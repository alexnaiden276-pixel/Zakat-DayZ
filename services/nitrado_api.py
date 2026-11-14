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

# Штатные пути миссии на FTP Nitrado
DEFAULT_SPAWN_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/cfgeventspawns.xml"
DEFAULT_EVENTS_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/db/events.xml"
DEFAULT_MESSAGES_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/db/messages.xml"

# Маркеры для безопасной вставки и очистки заказов внутри штатного cfgeventspawns.xml
SHOP_MARKER_START = "    <!-- BOT_SHOP_START -->"
SHOP_MARKER_END = "    <!-- BOT_SHOP_END -->"

# Nitrado именует .adm файлы в UTC; DayZ PS4 запускает таймер messages.xml ~через 6 мин после создания лога
NITRADO_LOG_TZ = timezone.utc
MSK_TZ = timezone(timedelta(hours=3))
RESTART_BOOT_OFFSET_MINUTES = 6

# Регулярное выражение для парсинга убийств из логов DayZ (дистанция опциональна для ближнего боя)
KILL_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)\s*\(id=.*?\)\s+killed\s+by\s+Player\s+"(?P<killer>[^"]+)"\s*\(id=.*?\)\s+with\s+(?P<weapon>.+?)(?:\s+from\s+(?P<distance>[0-9.]+)\s*meters?)?$',
    re.IGNORECASE,
)

# Регулярное выражение для проверки попадания в голову
HEADSHOT_PATTERN = re.compile(
    r'Player\s+"(?P<victim>[^"]+)"(?:\s*\(DEAD\))?\s*\(id=.*?\)(?:\[HP:\s*[0-9.]+\])?\s+hit\s+by\s+Player\s+"(?P<killer>[^"]+)"\s*\(id=.*?\)\s+into\s+(?:Head|Brain)\(',
    re.IGNORECASE,
)

# Паттерн для извлечения UTC-времени из имени файла DayZServer_PS4_x64_YYYY-MM-DD_HH-MM-SS.ADM
ADM_FILENAME_DT_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.adm$", re.IGNORECASE)
def _time_to_seconds(time_str: str) -> int | None:
    """Переводит HH:MM:SS в секунды с начала суток."""
    parts = time_str.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


def _is_within_seconds(hit_time: str, kill_time: str, max_diff: int = 3) -> bool:
    """Проверяет, что попадание в голову произошло не раньше чем за max_diff секунд до смерти."""
    t1 = _time_to_seconds(hit_time)
    t2 = _time_to_seconds(kill_time)
    if t1 is None or t2 is None:
        return hit_time == kill_time
    diff = (t2 - t1) % 86400
    return 0 <= diff <= max_diff


def _search_player_in_file(file_path: str, pattern: re.Pattern) -> str | None:
    """Синхронный поиск ID игрока в файле лога (выполняется в отдельном потоке)."""
    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            match = pattern.search(line)
            if match:
                return match.group(1)
    return None


def _parse_adm_file(
    file_path: str,
    last_line_hash: str | None,
    is_first_run: bool,
    source_file_name: str | None = None,
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
                    if not _is_within_seconds(hit_time, time_prefix, max_diff=3):
                        del recent_headshots[pair]
                continue

            match = KILL_PATTERN.search(line.strip())
            if match:
                clean_line = line.strip()
                unique_payload = f"{line_idx}:{clean_line}"
                line_hash = hashlib.sha256(unique_payload.encode("utf-8")).hexdigest()
                data = match.groupdict()
                data["distance"] = data.get("distance") or "0"
                data["hash"] = line_hash
                if source_file_name:
                    data["file_name"] = source_file_name

                pair = (data["killer"], data["victim"])
                hit_time = recent_headshots.pop(pair, None)
                data["is_headshot"] = bool(
                    hit_time and _is_within_seconds(hit_time, data["time"], max_diff=3)
                )

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
        self.api_url = f"https://nitrado.net{server_id}/gameservers"
        self._timeout = aiohttp.ClientTimeout(total=15)
        self._session: aiohttp.ClientSession | None = None

        self._spawn_xml_lock = asyncio.Lock()
        self._ftp_semaphore = asyncio.Semaphore(2)

        self.ftp_host = ftp_host
        self.ftp_user = ftp_user
        self.ftp_pass = ftp_pass
        self.ftp_port = ftp_port

    def _has_ftp_creds(self) -> bool:
        return all([self.ftp_host, self.ftp_user, self.ftp_pass])

    @contextlib.asynccontextmanager
    async def _ftp_client(self):
        """Контекстный менеджер для FTP с ограничением одновременных сессий и таймаутом."""
        async with self._ftp_semaphore:
            async with aioftp.Client.context(
                self.ftp_host,
                port=self.ftp_port,
                user=self.ftp_user,
                password=self.ftp_pass,
                socket_timeout=20,
            ) as client:
                yield client

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

    async def _list_adm_files(self, client: aioftp.Client, folder: str) -> list[dict]:
        """Возвращает отсортированный от свежих к старым список .adm файлов в папке."""
        await client.change_directory(folder)
        adm_files = []
        async for path, stat in client.list():
            if path.name.lower().endswith(".adm"):
                modified_time = stat.get("modify", "0") or "0"
                adm_files.append({"path": path, "name": path.name, "modified": str(modified_time)})
        adm_files.sort(key=lambda x: (x["name"], x["modified"]), reverse=True)
        return adm_files
    async def get_latest_adm_filename(self, logs_folder: str = "/dayzps/config") -> str | None:
        """Возвращает имя самого свежего .adm лога на FTP для отслеживания рестартов."""
        if not self._has_ftp_creds():
            return None
        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, logs_folder)
                return adm_files[0]["name"] if adm_files else None
        except Exception as e:
            logger.warning("[MONITOR] Не удалось получить имя свежего .adm файла: %s", e)
            return None

    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/dayzps/config") -> str | None:
        """
        Подключается по FTP, сортирует логи и проверяет 3 самых свежих файла .adm,
        чтобы гарантированно найти ID игрока.
        """
        if not self._has_ftp_creds():
            return None

        escaped_nick = re.escape(psn_nick.strip())
        pattern = re.compile(rf'Player\s+"{escaped_nick}"\s*\(id=([^)]+)\)', re.IGNORECASE)
        downloaded_files: list[str] = []
        temp_dir = tempfile.gettempdir()

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                if not adm_files:
                    logger.warning("Файлы логов .adm не обнаружены на FTP в папке %s", folder)
                    return None

                for file_data in adm_files[:3]:
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
        if not self._has_ftp_creds():
            logger.error("Параметры FTP (host, user, pass) не переданы при инициализации класса!")
            return [], last_file_name, last_line_hash

        downloaded_paths: list[str] = []
        temp_dir = tempfile.gettempdir()
        is_first_run = not last_file_name and not last_line_hash

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                if not adm_files:
                    logger.warning("[KILL_LOG] Файлы .adm не найдены в папке %s", folder)
                    return [], last_file_name, last_line_hash

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
                                _parse_adm_file,
                                prev_local_path,
                                last_line_hash,
                                False,
                                prev_file["name"],
                            )
                            all_new_kills.extend(prev_kills)

                effective_hash = last_line_hash if current_file_name == last_file_name else None
                real_local_path = os.path.join(temp_dir, f"kills_{current_file_name}")
                downloaded_paths.append(real_local_path)
                await client.download(latest_file["path"], real_local_path, write_into=True)

                if os.path.isfile(real_local_path):
                    new_kills, updated_hash = await asyncio.to_thread(
                        _parse_adm_file,
                        real_local_path,
                        effective_hash,
                        is_first_run,
                        current_file_name,
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
        Читает <deadline> (в минутах) из messages.xml и вычисляет точное время
        следующего рестарта по МСК от момента запуска миссии DayZ.
        """
        if not self._has_ftp_creds():
            logger.warning("[MONITOR] FTP-данные не заданы, невозможно прочитать messages.xml.")
            return None

        try:
            async with self._ftp_client() as client:
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
                    logger.warning("[MONITOR] В messages.xml не найден активный <deadline> для рестарта.")
                    return None

                start_dt_utc: datetime | None = None

                # Пробуем определить старт по имени свежего .adm файла
                adm_files = await self._list_adm_files(client, logs_folder)
                if adm_files:
                    match = ADM_FILENAME_DT_PATTERN.search(adm_files[0]["name"])
                    if match:
                        dt_str = f"{match.group(1)} {match.group(2).replace('-', ':')}"
                        with contextlib.suppress(ValueError):
                            parsed = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                            start_dt_utc = parsed.replace(tzinfo=NITRADO_LOG_TZ)

                # Резервный вариант — время запуска из Nitrado API (server_data)
                if start_dt_utc is None and server_data:
                    raw_started = (
                        server_data.get("started_at")
                        or server_data.get("last_started")
                        or server_data.get("status_since")
                    )
                    if raw_started:
                        with contextlib.suppress(ValueError):
                            parsed = datetime.fromisoformat(str(raw_started).replace("Z", "+00:00"))
                            start_dt_utc = parsed.astimezone(timezone.utc)

                if start_dt_utc is None:
                    logger.warning("[MONITOR] Не удалось определить время запуска сервера.")
                    return None

                now_msk = datetime.now(MSK_TZ)
                total_cycle_minutes = deadline_minutes + RESTART_BOOT_OFFSET_MINUTES
                restart_dt_msk = (start_dt_utc + timedelta(minutes=total_cycle_minutes)).astimezone(MSK_TZ)

                while restart_dt_msk <= now_msk:
                    restart_dt_msk += timedelta(minutes=total_cycle_minutes)

                logger.info(
                    "[MONITOR] Следующий рестарт (цикл %d+%d мин.): %s",
                    deadline_minutes,
                    RESTART_BOOT_OFFSET_MINUTES,
                    restart_dt_msk.strftime("%Y-%m-%d %H:%M:%S МСК"),
                )
                return restart_dt_msk

        except Exception as e:
            logger.error("[MONITOR] Ошибка расчёта рестарта из messages.xml: %s", e, exc_info=True)
            return None
    @staticmethod
    def _build_shop_event_xml(classname: str) -> str:
        """Формирует XML-блок события по точному класснейму предмета."""
        return (
            f'    <event name="{classname}">\n'
            f"        <nominal>20</nominal>\n"
            f"        <min>1</min>\n"
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

    async def ensure_shop_event_exists(
        self,
        classname: str,
        remote_events_path: str = DEFAULT_EVENTS_XML_PATH,
    ) -> bool:
        """Проверяет наличие <event name="{classname}"> в db/events.xml и добавляет при отсутствии."""
        if not self._has_ftp_creds():
            return False

        async with self._spawn_xml_lock:
            try:
                async with self._ftp_client() as client:
                    xml_content = await self._download_ftp_text(client, remote_events_path)
                    if f'name="{classname}"' in xml_content:
                        return True

                    if "</events>" not in xml_content:
                        logger.error("В %s не найден закрывающий тег </events>!", remote_events_path)
                        return False

                    event_block = self._build_shop_event_xml(classname)
                    updated_xml = xml_content.replace("</events>", f"{event_block}</events>")
                    ET.fromstring(updated_xml)  # Валидация XML перед загрузкой

                    await self._upload_ftp_text(client, remote_events_path, updated_xml)
                    logger.info("Событие %s добавлено в %s", classname, remote_events_path)
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
        remote_events_path: str = DEFAULT_EVENTS_XML_PATH,
    ) -> bool:
        """
        Добавляет события в db/events.xml и дописывает координаты заказа в cfgeventspawns.xml
        строго внутри маркеров BOT_SHOP_START / BOT_SHOP_END, не затрагивая ванильные точки.
        """
        if not self._has_ftp_creds():
            logger.error("Параметры FTP (host, user, pass) не переданы!")
            return False

        async with self._spawn_xml_lock:
            try:
                async with self._ftp_client() as client:
                    events_xml = await self._download_ftp_text(client, remote_events_path)
                    if "</events>" not in events_xml:
                        logger.error("В %s не найден закрывающий тег </events>!", remote_events_path)
                        return False

                    events_modified = False
                    for item in items:
                        classname = item["game_classname"]
                        if f'name="{classname}"' not in events_xml:
                            block = self._build_shop_event_xml(classname)
                            events_xml = events_xml.replace("</events>", f"{block}</events>")
                            events_modified = True

                    if events_modified:
                        ET.fromstring(events_xml)  # Валидация XML перед загрузкой
                        await self._upload_ftp_text(client, remote_events_path, events_xml)
                        logger.info("Файл %s обновлён новыми событиями перед записью заказа.", remote_events_path)

                    spawns_xml = await self._download_ftp_text(client, remote_xml_path)
                    if "</eventposdef>" not in spawns_xml:
                        logger.error("В %s не найден закрывающий тег </eventposdef>!", remote_xml_path)
                        return False

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
                                f'    <event name="{classname}">\n'
                                f'        <pos x="{offset_x}" z="{offset_z}" a="0" />\n'
                                f"    </event>"
                            )

                    entries_block = "\n".join(new_entries) + "\n"

                    if SHOP_MARKER_END in spawns_xml:
                        spawns_xml = spawns_xml.replace(
                            SHOP_MARKER_END, f"{entries_block}{SHOP_MARKER_END}"
                        )
                    else:
                        wrapped = f"{SHOP_MARKER_START}\n{entries_block}{SHOP_MARKER_END}\n"
                        spawns_xml = spawns_xml.replace("</eventposdef>", f"{wrapped}</eventposdef>")

                    ET.fromstring(spawns_xml)  # Валидация XML перед загрузкой
                    await self._upload_ftp_text(client, remote_xml_path, spawns_xml)
                    logger.info("Заказ успешно записан в %s на координаты X=%s Z=%s", remote_xml_path, x, z)
                    return True

            except Exception as e:
                logger.error("Ошибка записи заказа на FTP Nitrado: %s", e, exc_info=True)
                return False

    async def clear_spawn_orders(
        self,
        remote_xml_path: str = DEFAULT_SPAWN_XML_PATH,
    ) -> bool:
        """Удаляет ТОЛЬКО блок заказов между маркерами BOT_SHOP_START и BOT_SHOP_END в cfgeventspawns.xml."""
        if not self._has_ftp_creds():
            return False

        async with self._spawn_xml_lock:
            try:
                async with self._ftp_client() as client:
                    spawns_xml = await self._download_ftp_text(client, remote_xml_path)
                    if SHOP_MARKER_START not in spawns_xml or SHOP_MARKER_END not in spawns_xml:
                        return True

                    pattern = re.compile(
                        rf"{re.escape(SHOP_MARKER_START)}.*?{re.escape(SHOP_MARKER_END)}\n?",
                        re.DOTALL,
                    )
                    cleaned_xml = pattern.sub("", spawns_xml)

                    if cleaned_xml != spawns_xml:
                        ET.fromstring(cleaned_xml)  # Валидация XML перед загрузкой
                        await self._upload_ftp_text(client, remote_xml_path, cleaned_xml)
                        logger.info("Блок заказов магазина очищен в %s", remote_xml_path)
                    return True
            except Exception as e:
                logger.error("Ошибка очистки заказов в %s: %s", remote_xml_path, e, exc_info=True)
                return False
