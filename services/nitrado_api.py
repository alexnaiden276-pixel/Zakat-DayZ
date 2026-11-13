import asyncio
import hashlib
import logging
import os
import re
import tempfile
import aiohttp
import aioftp

logger = logging.getLogger(__name__)

# Путь к XML-файлу спавна заказов на FTP Nitrado
DEFAULT_SPAWN_XML_PATH = "/dayzps/missions/dayzOffline.chernarusplus/cfgeventspawns.xml"

# Улучшенное регулярное выражение: корректно захватывает оружие даже с пробелами
KILL_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<killer>[^"]+)"\s*\(id=[^)]+\)\s+killed\s+Player\s+"(?P<victim>[^"]+)"\s*\(id=[^)]+\)\s+with\s+(?P<weapon>.+?)\s+from\s+(?P<distance>[0-9.]+)m',
    re.IGNORECASE,
)


def _search_player_in_file(file_path: str, pattern: re.Pattern) -> str | None:
    """Синхронный поиск ID игрока в файле лога (выполняется в отдельном потоке)."""
    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            match = pattern.search(line)
            if match:
                return match.group(1)
    return None


def _parse_adm_file(file_path: str, last_line_hash: str | None, is_first_run: bool) -> tuple[list[dict], str | None]:
    """Синхронный парсинг лога убийств (выполняется в отдельном потоке)."""
    kills_in_file = []
    last_found_hash = last_line_hash

    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            match = KILL_PATTERN.search(line)
            if match:
                clean_line = line.strip()
                line_hash = hashlib.sha256(clean_line.encode("utf-8")).hexdigest()
                data = match.groupdict()
                data["hash"] = line_hash
                kills_in_file.append(data)

    if not kills_in_file:
        return [], last_found_hash

    # При самом первом запуске не спамим старыми киллами, а фиксируем последний как точку отсчета
    if is_first_run:
        return [], kills_in_file[-1]["hash"]

    # Ищем строку, на которой остановились в прошлый раз
    if last_line_hash:
        for idx, item in enumerate(kills_in_file):
            if item["hash"] == last_line_hash:
                new_items = kills_in_file[idx + 1 :]
                new_last_hash = new_items[-1]["hash"] if new_items else last_line_hash
                return new_items, new_last_hash

    # Если хэш не найден (например, начался новый файл лога после рестарта) — возвращаем все убийства из файла
    return kills_in_file, kills_in_file[-1]["hash"]


class NitradoAPI:
    def __init__(
        self,
        token: str,
        server_id: int | str,
        ftp_host: str | None = None,
        ftp_user: str | None = None,
        ftp_pass: str | None = None,
        ftp_port: int = 21,
    ) -> None:
        self.token = token
        self.server_id = server_id
        self.headers = {"Authorization": f"Bearer {token}"}
        self.api_url = f"https://api.nitrado.net/services/{server_id}/gameservers"
        self._timeout = aiohttp.ClientTimeout(total=15)
        self._session: aiohttp.ClientSession | None = None

        # FTP настройки
        self.ftp_host = ftp_host
        self.ftp_user = ftp_user
        self.ftp_pass = ftp_pass
        self.ftp_port = ftp_port

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=self._timeout,
            )
        return self._session

    async def get_server_status(self) -> dict | None:
        """Получает актуальную информацию о сервере DayZ через API Nitrado."""
        session = await self._get_session()
        try:
            async with session.get(self.api_url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("data", {}).get("gameserver", {})
                logger.warning("Ошибка Nitrado API (status): статус %s", response.status)
                return None
        except Exception as e:
            logger.error("Ошибка при запросе статуса сервера: %s", e)
            return None

    async def close(self) -> None:
        """Закрывает HTTP-сессию при остановке бота."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/dayzps/config") -> str | None:
        """
        Подключается по FTP, сортирует логи по дате и имени и проверяет 3 самых свежих файла .adm,
        чтобы гарантированно найти ID игрока.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return None

        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)', re.IGNORECASE)
        downloaded_files = []
        temp_dir = tempfile.gettempdir()

        try:
            logger.info("Асинхронное подключение к FTP Nitrado: %s:%s", self.ftp_host, self.ftp_port)

            async with aioftp.Client.context(
                self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
            ) as client:
                try:
                    await client.change_directory(folder)
                except Exception as e:
                    logger.error("Не удалось перейти в папку логов '%s' через FTP: %s", folder, e)
                    return None

                adm_files = []
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        modified_time = stat.get("modify", "0") or "0"
                        adm_files.append({"path": path, "name": path.name, "modified": str(modified_time)})

                if not adm_files:
                    logger.warning("Файлы логов .adm не обнаружены на FTP в папке %s", folder)
                    return None

                adm_files.sort(key=lambda x: (x["modified"], x["name"]), reverse=True)
                files_to_check = adm_files[:3]

                for file_data in files_to_check:
                    real_local_path = os.path.join(temp_dir, f"find_{file_data['name']}")
                    await client.download(file_data["path"], real_local_path, write_into=True)
                    downloaded_files.append(real_local_path)

                    if os.path.exists(real_local_path):
                        player_id = await asyncio.to_thread(_search_player_in_file, real_local_path, pattern)
                        if player_id:
                            logger.info("Игрок %s успешно найден! ID: %s", psn_nick, player_id)
                            return player_id
                return None

        except Exception as e:
            logger.error("Ошибка при поиске игрока через aioftp: %s", e, exc_info=True)
            return None
        finally:
            for temp_file in downloaded_files:
                if os.path.exists(temp_file) and os.path.isfile(temp_file):
                    os.remove(temp_file)
    
    async def get_new_kills(
        self,
        last_file_name: str | None,
        last_line_hash: str | None,
        folder: str = "/dayzps/config",
    ) -> tuple[list[dict], str | None, str | None]:
        """
        Скачивает свежий .adm лог во временную папку, парсит новые убийства по хэшу строки
        и корректно удаляет временный файл.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return [], last_file_name, last_line_hash

        real_local_path = None
        temp_dir = tempfile.gettempdir()
        is_first_run = last_file_name is None and last_line_hash is None

        try:
            async with aioftp.Client.context(
                self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
            ) as client:
                try:
                    await client.change_directory(folder)
                except Exception as e:
                    logger.error("Не удалось перейти в папку логов через FTP для поиска киллов: %s", e)
                    return [], last_file_name, last_line_hash

                adm_files = []
                async for path, stat in client.list():
                    if path.name.lower().endswith(".adm"):
                        modified_time = stat.get("modify", "0") or "0"
                        adm_files.append({"path": path, "name": path.name, "modified": str(modified_time)})

                if not adm_files:
                    logger.warning("[KILL_LOG] Файлы .adm не найдены в папке %s", folder)
                    return [], last_file_name, last_line_hash

                adm_files.sort(key=lambda x: (x["modified"], x["name"]), reverse=True)
                latest_file = adm_files[0]
                current_file_name = latest_file["name"]

                # Если файл сменился (произошел рестарт сервера) — сбрасываем старый хэш строки
                effective_hash = last_line_hash if current_file_name == last_file_name else None

                real_local_path = os.path.join(temp_dir, f"kills_{current_file_name}")
                await client.download(latest_file["path"], real_local_path, write_into=True)

                if os.path.exists(real_local_path) and os.path.isfile(real_local_path):
                    new_kills, updated_hash = await asyncio.to_thread(
                        _parse_adm_file, real_local_path, effective_hash, is_first_run
                    )
                    if is_first_run:
                        logger.info(
                            "[KILL_LOG] Точка отсчета установлена: файл=%s, hash=%s",
                            current_file_name,
                            updated_hash,
                        )
                    else:
                        logger.info(
                            "[KILL_LOG] Проверка завершена: файл=%s, новых убийств=%d",
                            current_file_name,
                            len(new_kills),
                        )
                    return new_kills, current_file_name, updated_hash

        except Exception as e:
            logger.error("Ошибка при парсинге логов убийств через aioftp: %s", e, exc_info=True)
        finally:
            if real_local_path and os.path.exists(real_local_path) and os.path.isfile(real_local_path):
                os.remove(real_local_path)

        return [], last_file_name, last_line_hash

    async def append_spawn_orders(
        self,
        items: list[dict],
        x: float,
        z: float,
        remote_xml_path: str = DEFAULT_SPAWN_XML_PATH,
    ) -> bool:
        """
        Скачивает XML спавна с FTP, дописывает координаты iZurvive для каждого купленного предмета
        и загружает обновленный файл обратно на сервер Nitrado.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы!")
            return False

        local_tmp = os.path.join(tempfile.gettempdir(), "shop_orders_spawn.xml")
        try:
            async with aioftp.Client.context(
                self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
            ) as client:
                try:
                    await client.download(remote_xml_path, local_tmp, write_into=True)
                    with open(local_tmp, mode="r", encoding="utf-8") as f:
                        xml_content = f.read()
                except Exception:
                    xml_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<eventposdef>\n</eventposdef>'

                new_entries = []
                for item in items:
                    classname = item["game_classname"]
                    qty = item.get("qty", 1)
                    for _ in range(qty):
                        offset_x = round(x + (len(new_entries) * 0.3), 2)
                        offset_z = round(z, 2)
                        new_entries.append(
                            f'    <event name="Item_{classname}">\n'
                            f'        <pos x="{offset_x}" z="{offset_z}" a="0" />\n'
                            f'    </event>'
                        )

                entries_block = "\n".join(new_entries) + "\n"

                if "</eventposdef>" in xml_content:
                    xml_content = xml_content.replace("</eventposdef>", f"{entries_block}</eventposdef>")
                else:
                    xml_content += f"\n<eventposdef>\n{entries_block}</eventposdef>"

                with open(local_tmp, mode="w", encoding="utf-8") as f:
                    f.write(xml_content)

                await client.upload(local_tmp, remote_xml_path, write_into=True)
                logger.info("Заказ успешно записан в %s на координаты X=%s Z=%s", remote_xml_path, x, z)
                return True

        except Exception as e:
            logger.error("Ошибка записи заказа на FTP Nitrado: %s", e, exc_info=True)
            return False
        finally:
            if os.path.exists(local_tmp) and os.path.isfile(local_tmp):
                os.remove(local_tmp)

    async def clear_spawn_orders(
        self,
        remote_xml_path: str = DEFAULT_SPAWN_XML_PATH,
    ) -> bool:
        """Очищает файл заказов на FTP после рестарта сервера."""
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            return False

        local_tmp = os.path.join(tempfile.gettempdir(), "shop_orders_clean.xml")
        clean_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<eventposdef>\n</eventposdef>\n'
        try:
            with open(local_tmp, mode="w", encoding="utf-8") as f:
                f.write(clean_xml)

            async with aioftp.Client.context(
                self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
            ) as client:
                await client.upload(local_tmp, remote_xml_path, write_into=True)
                logger.info("Файл заказов %s успешно очищен после рестарта сервера.", remote_xml_path)
                return True
        except Exception as e:
            logger.error("Ошибка очистки файла заказов на FTP: %s", e)
            return False
        finally:
            if os.path.exists(local_tmp) and os.path.isfile(local_tmp):
                os.remove(local_tmp)


