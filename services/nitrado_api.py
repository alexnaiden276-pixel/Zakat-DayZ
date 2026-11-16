import asyncio
import contextlib
import hashlib
import logging
import os
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import aioftp
import aiohttp

logger = logging.getLogger(__name__)

# Штатные пути миссии на FTP Nitrado
DEFAULT_SHOP_EVENTS_PATH = "/dayzps_missions/dayzOffline.chernarusplus/custom/shop_events.xml"
DEFAULT_EVENT_SPAWNS_PATH = "/dayzps_missions/dayzOffline.chernarusplus/cfgeventspawns.xml"
DEFAULT_MESSAGES_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/db/messages.xml"

MSK_TZ = timezone(timedelta(hours=3))

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

# Регулярное выражение для сброса хедшота при добивании в тело
BODY_HIT_PATTERN = re.compile(
    r'Player\s+"(?P<victim>[^"]+)"(?:\s*\(DEAD\))?\s*\(id=.*?\)(?:\[HP:\s*[0-9.]+\])?\s+hit\s+by\s+Player\s+"(?P<killer>[^"]+)"',
    re.IGNORECASE,
)
def _indent_xml(elem: ET.Element, level: int = 0) -> None:
    """Аккуратно форматирует XML с отступами перед сохранением."""
    indent = "\n" + level * "    "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = indent + "    "
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
        for child in elem:
            _indent_xml(child, level + 1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = indent
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = indent


def _time_to_seconds(time_str: str) -> int | None:
    """Переводит HH:MM:SS в секунды с начала суток."""
    parts = time_str.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts) * 3600 + int(parts) * 60 + int(parts)
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
    file_prefix = source_file_name or os.path.basename(file_path)

    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            time_prefix = line[:8] if len(line) >= 8 else ""

            hit_match = HEADSHOT_PATTERN.search(line)
            if hit_match:
                pair = (hit_match.group("killer"), hit_match.group("victim"))
                recent_headshots[pair] = time_prefix
                continue

            body_match = BODY_HIT_PATTERN.search(line)
            if body_match:
                pair = (body_match.group("killer"), body_match.group("victim"))
                recent_headshots.pop(pair, None)
                continue

            match = KILL_PATTERN.search(line.strip())
            if match:
                clean_line = line.strip()
                unique_payload = f"{file_prefix}:{clean_line}"
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
        self.api_url = f"https://api.nitrado.net/services/{server_id}/gameservers"
        self._timeout = aiohttp.ClientTimeout(total=15)
        self._session: aiohttp.ClientSession | None = None

        self._shop_file_lock = asyncio.Lock()
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

    async def _download_ftp_file(self, client: aioftp.Client, remote_path: str, local_path: str) -> None:
        """Скачивает файл с FTP на диск через поток, минуя команду MLST."""
        async with client.download_stream(remote_path) as stream:
            with open(local_path, mode="wb") as f:
                async for block in stream.iter_by_block():
                    f.write(block)

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
        call_uid = uuid.uuid4().hex[:8]

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                if not adm_files:
                    logger.warning("Файлы логов .adm не обнаружены на FTP в папке %s", folder)
                    return None

                for file_data in adm_files[:3]:
                    real_local_path = os.path.join(temp_dir, f"find_{call_uid}_{file_data['name']}")
                    await self._download_ftp_file(client, file_data["name"], real_local_path)
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
        call_uid = uuid.uuid4().hex[:8]
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
                        prev_local_path = os.path.join(temp_dir, f"kills_prev_{call_uid}_{prev_file['name']}")
                        downloaded_paths.append(prev_local_path)
                        await self._download_ftp_file(client, prev_file["name"], prev_local_path)
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
                real_local_path = os.path.join(temp_dir, f"kills_{call_uid}_{current_file_name}")
                downloaded_paths.append(real_local_path)
                await self._download_ftp_file(client, current_file_name, real_local_path)

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
    async def ensure_shop_event_exists(
        self,
        classname: str,
        remote_events_path: str = DEFAULT_SHOP_EVENTS_PATH,
    ) -> bool:
        """Создаёт заготовку эвента Item_Shop_<Classname> с nominal=0 в shop_events.xml."""
        if not self._has_ftp_creds():
            return False

        event_name = f"Item_Shop_{classname}"
        async with self._shop_file_lock:
            try:
                async with self._ftp_client() as client:
                    try:
                        raw_xml = await self._download_ftp_text(client, remote_events_path)
                        root = ET.fromstring(raw_xml) if raw_xml.strip() else ET.Element("events")
                    except aioftp.StatusCodeError as ftp_err:
                        if "550" in str(ftp_err.received_codes):
                            root = ET.Element("events")
                        else:
                            raise

                    for ev in root.findall("event"):
                        if ev.get("name") == event_name:
                            return True

                    ev = ET.SubElement(root, "event", {"name": event_name})
                    ET.SubElement(ev, "nominal").text = "0"
                    ET.SubElement(ev, "min").text = "0"
                    ET.SubElement(ev, "max").text = "0"
                    ET.SubElement(ev, "lifetime").text = "3600"
                    ET.SubElement(ev, "restock").text = "0"
                    ET.SubElement(ev, "saferadius").text = "0"
                    ET.SubElement(ev, "distanceradius").text = "0"
                    ET.SubElement(ev, "cleanupradius").text = "50"
                    ET.SubElement(ev, "flags", {"deletable": "0", "init_random": "0", "remove_damaged": "0"})
                    ET.SubElement(ev, "position").text = "fixed"
                    ET.SubElement(ev, "limit").text = "child"
                    ET.SubElement(ev, "active").text = "1"
                    children = ET.SubElement(ev, "children")
                    ET.SubElement(children, "child", {
                        "lootmax": "0",
                        "lootmin": "0",
                        "max": "1",
                        "min": "1",
                        "type": classname,
                    })

                    _indent_xml(root)
                    xml_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding="unicode")
                    await self._upload_ftp_text(client, remote_events_path, xml_out)
                    logger.info("Эвент %s зарегистрирован в %s", event_name, remote_events_path)
                    return True
            except Exception as e:
                logger.error("Ошибка регистрации эвента %s: %s", event_name, e, exc_info=True)
                return False

    async def append_event_spawn_orders(
        self,
        items: list[dict],
        x: float,
        z: float,
        radius: float = 3.0,
        remote_events_path: str = DEFAULT_SHOP_EVENTS_PATH,
        remote_spawns_path: str = DEFAULT_EVENT_SPAWNS_PATH,
    ) -> bool:
        """
        Увеличивает nominal/max в shop_events.xml и прописывает координаты точки выдачи
        в cfgeventspawns.xml с радиусом разброса (без сетки координат).
        """
        if not self._has_ftp_creds():
            return False

        async with self._shop_file_lock:
            try:
                async with self._ftp_client() as client:
                    try:
                        raw_events = await self._download_ftp_text(client, remote_events_path)
                        events_root = ET.fromstring(raw_events) if raw_events.strip() else ET.Element("events")
                    except aioftp.StatusCodeError as ftp_err:
                        if "550" in str(ftp_err.received_codes):
                            events_root = ET.Element("events")
                        else:
                            raise

                    raw_spawns = await self._download_ftp_text(client, remote_spawns_path)
                    spawns_root = ET.fromstring(raw_spawns)

                    for item in items:
                        classname = item["game_classname"]
                        qty = int(item.get("qty", 1))
                        event_name = f"Item_Shop_{classname}"

                        target_ev = next((e for e in events_root.findall("event") if e.get("name") == event_name), None)
                        if target_ev is None:
                            target_ev = ET.SubElement(events_root, "event", {"name": event_name})
                            ET.SubElement(target_ev, "nominal").text = "0"
                            ET.SubElement(target_ev, "min").text = "0"
                            ET.SubElement(target_ev, "max").text = "0"
                            ET.SubElement(target_ev, "lifetime").text = "3600"
                            ET.SubElement(target_ev, "restock").text = "0"
                            ET.SubElement(target_ev, "saferadius").text = "0"
                            ET.SubElement(target_ev, "distanceradius").text = "0"
                            ET.SubElement(target_ev, "cleanupradius").text = "50"
                            ET.SubElement(target_ev, "flags", {"deletable": "0", "init_random": "0", "remove_damaged": "0"})
                            ET.SubElement(target_ev, "position").text = "fixed"
                            ET.SubElement(target_ev, "limit").text = "child"
                            ET.SubElement(target_ev, "active").text = "1"
                            children = ET.SubElement(target_ev, "children")
                            ET.SubElement(children, "child", {
                                "lootmax": "0",
                                "lootmin": "0",
                                "max": "1",
                                "min": "1",
                                "type": classname,
                            })

                        nom_tag = target_ev.find("nominal")
                        max_tag = target_ev.find("max")
                        current_nom = int(nom_tag.text or 0) if nom_tag is not None else 0
                        new_nom = current_nom + qty
                        if nom_tag is not None:
                            nom_tag.text = str(new_nom)
                        if max_tag is not None:
                            max_tag.text = str(new_nom)

                        spawn_ev = next((e for e in spawns_root.findall("event") if e.get("name") == event_name), None)
                        if spawn_ev is None:
                            spawn_ev = ET.SubElement(spawns_root, "event", {"name": event_name})

                        zone = spawn_ev.find("zone")
                        if zone is None:
                            ET.SubElement(spawn_ev, "zone", {
                                "smin": "0",
                                "smax": "0",
                                "dmin": "0",
                                "dmax": "0",
                                "r": str(radius),
                            })
                        for _ in range(qty):
                            ET.SubElement(spawn_ev, "pos", {
                                "x": str(round(float(x), 2)),
                                "z": str(round(float(z), 2)),
                                "a": "0",
                            })

                    _indent_xml(events_root)
                    _indent_xml(spawns_root)

                    events_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(events_root, encoding="unicode")
                    spawns_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(spawns_root, encoding="unicode")

                    await self._upload_ftp_text(client, remote_events_path, events_out)
                    await self._upload_ftp_text(client, remote_spawns_path, spawns_out)
                    logger.info("Заказ через эвенты записан на X=%s Z=%s (радиус %s м)", x, z, radius)
                    return True
            except Exception as e:
                logger.error("Ошибка записи заказа в XML эвентов: %s", e, exc_info=True)
                return False
    async def clear_event_spawn_orders(
        self,
        remote_events_path: str = DEFAULT_SHOP_EVENTS_PATH,
        remote_spawns_path: str = DEFAULT_EVENT_SPAWNS_PATH,
    ) -> bool:
        """После рестарта обнуляет nominal/max в shop_events.xml и удаляет точки Item_Shop_* из cfgeventspawns.xml."""
        if not self._has_ftp_creds():
            return False

        async with self._shop_file_lock:
            try:
                async with self._ftp_client() as client:
                    # 1. Обнуляем nominal и max в shop_events.xml
                    try:
                        raw_events = await self._download_ftp_text(client, remote_events_path)
                        if raw_events.strip():
                            events_root = ET.fromstring(raw_events)
                            for ev in events_root.findall("event"):
                                if (ev.get("name") or "").startswith("Item_Shop_"):
                                    nom = ev.find("nominal")
                                    mx = ev.find("max")
                                    if nom is not None:
                                        nom.text = "0"
                                    if mx is not None:
                                        mx.text = "0"
                            _indent_xml(events_root)
                            events_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(events_root, encoding="unicode")
                            await self._upload_ftp_text(client, remote_events_path, events_out)
                    except Exception as ev_err:
                        logger.warning("Не удалось очистить shop_events.xml: %s", ev_err)

                    # 2. Удаляем все <event name="Item_Shop_..."> из cfgeventspawns.xml
                    raw_spawns = await self._download_ftp_text(client, remote_spawns_path)
                    if raw_spawns.strip():
                        spawns_root = ET.fromstring(raw_spawns)
                        for ev in list(spawns_root.findall("event")):
                            if (ev.get("name") or "").startswith("Item_Shop_"):
                                spawns_root.remove(ev)
                        _indent_xml(spawns_root)
                        spawns_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(spawns_root, encoding="unicode")
                        await self._upload_ftp_text(client, remote_spawns_path, spawns_out)

                    logger.info("Эвенты магазина успешно очищены после рестарта.")
                    return True
            except Exception as e:
                logger.error("Ошибка очистки XML эвентов: %s", e, exc_info=True)
                return False

    # Алиасы для обратной совместимости со старыми вызовами
    async def append_spawn_orders(
        self,
        items: list[dict],
        x: float,
        z: float,
        y: float = 0.0,
        remote_xml_path: str = DEFAULT_SHOP_EVENTS_PATH,
    ) -> bool:
        return await self.append_event_spawn_orders(
            items=items,
            x=x,
            z=z,
            radius=3.0,
            remote_events_path=remote_xml_path,
        )

    async def clear_spawn_orders(
        self,
        remote_xml_path: str = DEFAULT_SHOP_EVENTS_PATH,
    ) -> bool:
        return await self.clear_event_spawn_orders(remote_events_path=remote_xml_path)
