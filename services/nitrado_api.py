import asyncio
import contextlib
import hashlib
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import aioftp
import aiohttp

logger = logging.getLogger(__name__)

# Пути по умолчанию на FTP Nitrado
DEFAULT_SPAWN_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/custom/shop_eventspawns.xml"
DEFAULT_EVENTS_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/custom/shop_events.xml"
DEFAULT_MESSAGES_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/db/messages.xml"

# Регулярное выражение для парсинга убийств из логов DayZ
KILL_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)\s*\(id=.*?\)\s+killed\s+by\s+Player\s+"(?P<killer>[^"]+)"\s*\(id=.*?\)\s+with\s+(?P<weapon>.+?)\s+from\s+(?P<distance>[0-9.]+)\s*meters?',
    re.IGNORECASE,
)

# Регулярное выражение для проверки попадания в голову
HEADSHOT_PATTERN = re.compile(
    r'Player\s+"(?P<victim>[^"]+)"(?:\s*\(DEAD\))?\s*\(id=.*?\)(?:\[HP:\s*[0-9.]+\])?\s+hit\s+by\s+Player\s+"(?P<killer>[^"]+)"\s*\(id=.*?\)\s+into\s+(?:Head|Brain)\(',
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


def _parse_adm_file(
    file_path: str, last_line_hash: str | None, is_first_run: bool
) -> tuple[list[dict], str | None]:
    """Синхронный парсинг лога убийств с определением хедшотов (выполняется в отдельном потоке)."""
    kills_in_file = []
    last_found_hash = last_line_hash
    recent_headshots: dict[tuple[str, str], str] = {}

    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line_idx, line in enumerate(f, start=1):
            time_prefix = line[:8] if len(line) >= 8 else ""

            hit_match = HEADSHOT_PATTERN.search(line)
            if hit_match:
                pair = (hit_match.group("killer"), hit_match.group("victim"))
                recent_headshots[pair] = time_prefix
                continue

            if " hit by Player " in line and " into " in line:
                for pair, hit_time in list(recent_headshots.items()):
                    if hit_time != time_prefix:
                        del recent_headshots[pair]
                continue

            match = KILL_PATTERN.search(line)
            if match:
                clean_line = line.strip()
                unique_payload = f"{line_idx}:{clean_line}"
                line_hash = hashlib.sha256(unique_payload.encode("utf-8")).hexdigest()
                data = match.groupdict()
                data["hash"] = line_hash

                pair = (data["killer"], data["victim"])
                hit_time = recent_headshots.pop(pair, None)
                data["is_headshot"] = hit_time == data["time"]

                kills_in_file.append(data)

    if not kills_in_file:
        return [], last_found_hash

    if is_first_run:
        return [], kills_in_file[-1]["hash"]

    if last_line_hash:
        for idx, item in enumerate(kills_in_file):
            if item["hash"] == last_line_hash:
                new_items = kills_in_file[idx + 1 :]
                new_last_hash = new_items[-1]["hash"] if new_items else last_line_hash
                return new_items, new_last_hash

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

        self._spawn_xml_lock = asyncio.Lock()

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

    async def _download_ftp_text(self, client: aioftp.Client, remote_path: str) -> str:
        """Скачивает текстовый файл с FTP через поток, минуя команду MLST."""
        chunks: list[bytes] = []
        async with client.download_stream(remote_path) as stream:
            async for block in stream.iter_by_block():
                chunks.append(block)
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def _upload_ftp_text(self, client: aioftp.Client, remote_path: str, content: str) -> None:
        """Загружает текстовый файл на FTP через поток, минуя команду MLST."""
        async with client.upload_stream(remote_path) as stream:
            await stream.write(content.encode("utf-8"))

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/dayzps/config") -> str | None:
        """
        Подключается по FTP, сортирует логи по дате и имени и проверяет 3 самых свежих файла .adm,
        чтобы гарантированно найти ID игрока.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return None

        pattern = re.compile(rf'Player\s+"{re.escape(psn_nick)}"\s+\(id=([^)]+)\)', re.IGNORECASE)
        downloaded_files: list[str] = []
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

                    if os.path.isfile(real_local_path):
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
                if os.path.isfile(temp_file):
                    with contextlib.suppress(OSError):
                        os.remove(temp_file)
    async def get_new_kills(
        self,
        last_file_name: str | None,
        last_line_hash: str | None,
        folder: str = "/dayzps/config",
    ) -> tuple[list[dict], str | None, str | None]:
        """
        Скачивает свежий .adm лог во временную папку, парсит новые убийства по хэшу строки
        и корректно удаляет временный файл. При смене файла дочитывает остаток предыдущего.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return [], last_file_name, last_line_hash

        downloaded_paths: list[str] = []
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

                all_new_kills: list[dict] = []

                if not is_first_run and last_file_name and current_file_name != last_file_name:
                    prev_file = next((f for f in adm_files if f["name"] == last_file_name), None)
                    if prev_file:
                        prev_local_path = os.path.join(temp_dir, f"kills_prev_{prev_file['name']}")
                        downloaded_paths.append(prev_local_path)
                        await client.download(prev_file["path"], prev_local_path, write_into=True)
                        if os.path.isfile(prev_local_path):
                            prev_kills, _ = await asyncio.to_thread(
                                _parse_adm_file, prev_local_path, last_line_hash, False
                            )
                            all_new_kills.extend(prev_kills)

                effective_hash = last_line_hash if current_file_name == last_file_name else None
                real_local_path = os.path.join(temp_dir, f"kills_{current_file_name}")
                downloaded_paths.append(real_local_path)
                await client.download(latest_file["path"], real_local_path, write_into=True)

                if os.path.isfile(real_local_path):
                    new_kills, updated_hash = await asyncio.to_thread(
                        _parse_adm_file, real_local_path, effective_hash, is_first_run
                    )
                    all_new_kills.extend(new_kills)

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
                            len(all_new_kills),
                        )
                    return all_new_kills, current_file_name, updated_hash

        except Exception as e:
            logger.error("Ошибка при парсинге логов убийств через aioftp: %s", e, exc_info=True)
        finally:
            for tmp_path in downloaded_paths:
                if tmp_path and os.path.isfile(tmp_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)

        return [], last_file_name, last_line_hash
    async def get_next_restart_time(
        self,
        server_data: dict | None = None,
        messages_xml_path: str = DEFAULT_MESSAGES_XML_PATH,
        logs_folder: str = "/dayzps/config",
    ) -> datetime | None:
        """
        Читает <deadline> (в минутах) из /dayzps_missions/dayzOffline.chernarusplus/db/messages.xml
        и вычисляет точное время следующего рестарта по МСК.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.warning("[MONITOR] FTP-данные не заданы, невозможно прочитать messages.xml.")
            return None

        try:
            async with aioftp.Client.context(
                self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
            ) as client:
                xml_content = await self._download_ftp_text(client, messages_xml_path)
                root = ET.fromstring(xml_content)
                deadline_minutes: int | None = None

                for msg in root.findall("message"):
                    deadline_el = msg.find("deadline")
                    if deadline_el is not None and deadline_el.text and deadline_el.text.strip().isdigit():
                        val = int(deadline_el.text.strip())
                        if val > 0:
                            if msg.find("shutdown") is not None:
                                deadline_minutes = val
                                break
                            if deadline_minutes is None:
                                deadline_minutes = val

                if not deadline_minutes:
                    logger.warning("[MONITOR] В %s не найден активный <deadline> > 0.", messages_xml_path)
                    return None

                logger.info("[MONITOR] Найден таймер рестарта в messages.xml: %d мин.", deadline_minutes)

                start_dt_utc: datetime | None = None

                if server_data:
                    raw_started = (
                        server_data.get("last_status_change")
                        or server_data.get("started_at")
                    )
                    if raw_started:
                        with contextlib.suppress(ValueError):
                            parsed = datetime.fromisoformat(str(raw_started).replace("Z", "+00:00"))
                            start_dt_utc = parsed.astimezone(timezone.utc)
                            logger.info("[MONITOR] Время старта из Nitrado API: %s", start_dt_utc)

                if start_dt_utc is None:
                    await client.change_directory(logs_folder)
                    adm_files = []
                    async for path, stat in client.list():
                        if path.name.lower().endswith(".adm"):
                            modified_time = stat.get("modify", "0") or "0"
                            adm_files.append({"name": path.name, "modified": str(modified_time)})

                    if adm_files:
                        adm_files.sort(key=lambda x: (x["modified"], x["name"]), reverse=True)
                        latest_name = adm_files[0]["name"]
                        match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", latest_name)
                        if match:
                            dt_str = f"{match.group(1)} {match.group(2).replace('-', ':')}"
                            start_dt_utc = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(
                                tzinfo=timezone.utc
                            )
                            logger.info("[MONITOR] Время старта из лога %s: %s", latest_name, start_dt_utc)

                if start_dt_utc is None:
                    logger.warning("[MONITOR] Не удалось определить время запуска сервера.")
                    return None

                msk_tz = timezone(timedelta(hours=3))
                restart_dt_msk = (start_dt_utc + timedelta(minutes=deadline_minutes)).astimezone(msk_tz)
                logger.info(
                    "[MONITOR] Расчётное время рестарта: %s",
                    restart_dt_msk.strftime("%Y-%m-%d %H:%M:%S МСК"),
                )
                return restart_dt_msk

        except Exception as e:
            logger.error("[MONITOR] Ошибка расчёта рестарта из messages.xml: %s", e, exc_info=True)
            return None
    async def ensure_shop_event_exists(
        self,
        classname: str,
        remote_events_path: str = DEFAULT_EVENTS_XML_PATH,
    ) -> bool:
        """
        Проверяет наличие <event name="Item_{classname}"> в custom/shop_events.xml
        и при необходимости автоматически добавляет его на FTP.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            return False

        event_name = f"Item_{classname}"
        async with self._spawn_xml_lock:
            try:
                async with aioftp.Client.context(
                    self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
                ) as client:
                    try:
                        xml_content = await self._download_ftp_text(client, remote_events_path)
                    except Exception:
                        xml_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<events>\n</events>'

                    if f'name="{event_name}"' in xml_content:
                        return True

                    event_block = (
                        f'    <event name="{event_name}">\n'
                        f"        <nominal>20</nominal>\n"
                        f"        <min>0</min>\n"
                        f"        <max>20</max>\n"
                        f"        <lifetime>3600</lifetime>\n"
                        f"        <restock>0</restock>\n"
                        f"        <saferadius>0</saferadius>\n"
                        f"        <distanceradius>0</distanceradius>\n"
                        f"        <cleanupradius>0</cleanupradius>\n"
                        f'        <flags deletable="0" init_random="0" remove_damaged="0"/>\n'
                        f"        <position>fixed</position>\n"
                        f"        <limit>child</limit>\n"
                        f"        <active>1</active>\n"
                        f"        <children>\n"
                        f'            <child lootmax="0" lootmin="0" max="1" min="1" type="{classname}"/>\n'
                        f"        </children>\n"
                        f"    </event>\n"
                    )

                    if "</events>" in xml_content:
                        xml_content = xml_content.replace("</events>", f"{event_block}</events>")
                    else:
                        xml_content += f"\n<events>\n{event_block}</events>"

                    await self._upload_ftp_text(client, remote_events_path, xml_content)
                    logger.info("Событие %s автоматически добавлено в %s", event_name, remote_events_path)
                    return True
            except Exception as e:
                logger.error("Ошибка обновления %s для %s: %s", remote_events_path, classname, e)
                return False

    async def append_spawn_orders(
        self,
        items: list[dict],
        x: float,
        z: float,
        remote_xml_path: str = DEFAULT_SPAWN_XML_PATH,
    ) -> bool:
        """
        Скачивает XML спавна с FTP потоком, дописывает координаты iZurvive компактной сеткой
        и загружает обновленный файл обратно на сервер Nitrado.
        """
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            logger.error("Параметры FTP (host, user, pass) не переданы!")
            return False

        async with self._spawn_xml_lock:
            try:
                async with aioftp.Client.context(
                    self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
                ) as client:
                    try:
                        xml_content = await self._download_ftp_text(client, remote_xml_path)
                    except Exception:
                        xml_content = (
                            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                            "<eventposdef>\n"
                            "</eventposdef>"
                        )

                    new_entries = []
                    cols = 4
                    step = 0.35
                    for item in items:
                        classname = item["game_classname"]
                        qty = item.get("qty", 1)
                        for _ in range(qty):
                            idx = len(new_entries)
                            row, col = divmod(idx, cols)
                            offset_x = round(x + (col * step), 2)
                            offset_z = round(z + (row * step), 2)
                            new_entries.append(
                                f'    <event name="Item_{classname}">\n'
                                f'        <pos x="{offset_x}" z="{offset_z}" a="0" />\n'
                                f"    </event>"
                            )

                    entries_block = "\n".join(new_entries) + "\n"

                    if "</eventposdef>" in xml_content:
                        xml_content = xml_content.replace("</eventposdef>", f"{entries_block}</eventposdef>")
                    else:
                        xml_content += f"\n<eventposdef>\n{entries_block}</eventposdef>"

                    await self._upload_ftp_text(client, remote_xml_path, xml_content)
                    logger.info("Заказ успешно записан в %s на координаты X=%s Z=%s", remote_xml_path, x, z)
                    return True

            except Exception as e:
                logger.error("Ошибка записи заказа на FTP Nitrado: %s", e, exc_info=True)
                return False

    async def clear_spawn_orders(
        self,
        remote_xml_path: str = DEFAULT_SPAWN_XML_PATH,
    ) -> bool:
        """Очищает изолированный файл заказов на FTP после рестарта сервера."""
        if not all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            return False

        async with self._spawn_xml_lock:
            clean_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<eventposdef>\n</eventposdef>\n'
            try:
                async with aioftp.Client.context(
                    self.ftp_host, port=self.ftp_port, user=self.ftp_user, password=self.ftp_pass
                ) as client:
                    await self._upload_ftp_text(client, remote_xml_path, clean_xml)
                    logger.info("Файл заказов %s успешно очищен после рестарта сервера.", remote_xml_path)
                    return True
            except Exception as e:
                logger.error("Ошибка очистки файла заказов на FTP: %s", e)
                return False
