import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import random
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from typing import Callable, TypeVar

import aioftp
import aiohttp

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Штатные пути миссии на FTP Nitrado
DEFAULT_SHOP_EVENTS_PATH = "/dayzps_missions/dayzOffline.chernarusplus/custom/shop_events.xml"
DEFAULT_EVENT_SPAWNS_PATH = "/dayzps_missions/dayzOffline.chernarusplus/cfgeventspawns.xml"
DEFAULT_MESSAGES_XML_PATH = "/dayzps_missions/dayzOffline.chernarusplus/db/messages.xml"
DEFAULT_GAMEPLAY_JSON_PATH = "/dayzps_missions/dayzOffline.chernarusplus/cfggameplay.json"

MSK_TZ = timezone(timedelta(hours=3))

# Регулярное выражение для парсинга PvP-убийств из логов DayZ
KILL_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)\s*\(id=.*?\)\s+killed\s+by\s+Player\s+"(?P<killer>[^"]+)"\s*\(id=.*?\)\s+with\s+(?P<weapon>.+?)(?:\s+from\s+(?P<distance>[0-9.]+)\s*meters?)?$',
    re.IGNORECASE,
)

# Регулярное выражение для парсинга любой смерти игрока (зомби, голод, падение, суицид)
ANY_DEATH_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)',
    re.IGNORECASE,
)

# Регулярное выражение для проверки попадания в голову
HEADSHOT_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"(?:\s*\(DEAD\))?\s*\(id=.*?\)(?:\[HP:\s*[0-9.]+\])?\s+hit\s+by\s+Player\s+"(?P<killer>[^"]+)"\s*\(id=.*?\)\s+into\s+(?:Head|Brain)\(',
    re.IGNORECASE,
)

# Регулярное выражение для сброса хедшота при добивании в тело
BODY_HIT_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"(?:\s*\(DEAD\))?\s*\(id=.*?\)(?:\[HP:\s*[0-9.]+\])?\s+hit\s+by\s+Player\s+"(?P<killer>[^"]+)"',
    re.IGNORECASE,
)

# Регулярное выражение для поиска координат игроков в .ADM логе (в DayZ PS4/PS5 формат: pos=<X, Z, Y>)
PLAYER_POS_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<nick>[^"]+)"\s*\(id=.*?\s+pos=<(?P<x>-?[0-9.]+),\s*(?P<z>-?[0-9.]+),\s*-?[0-9.]+>',
    re.IGNORECASE,
)

# Регулярное выражение для отслеживания выхода игрока с сервера в .ADM логе
DISCONNECT_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<nick>[^"]+)"\s*\(id=.*?\)\s+has\s+been\s+disconnected',
    re.IGNORECASE,
)

# Если на сервере 0 игроков онлайн
EMPTY_PLAYERLIST_PATTERN = re.compile(
    r'PlayerList\s+log:\s*0\s+players',
    re.IGNORECASE,
)

# Фиксация попадания/уничтожения предмета игроком в .ADM логе (pos=<X, Z, Y>)
ADM_ITEM_HIT_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<nick>[^"]+)"\s*\(id=.*?\)\s+hit\s+(?P<classname>[A-Za-z0-9_]+)\s*\(pos=<(?P<x>-?[0-9.]+),\s*(?P<z>-?[0-9.]+),\s*-?[0-9.]+>',
    re.IGNORECASE,
)

# Поиск ника и полного ID игрока по строкам лога
PLAYER_BY_ID_PATTERN = re.compile(
    r'Player\s+"(?P<nick>[^"]+)"\s*\(id=(?P<full_id>[^)]+)\)',
    re.IGNORECASE,
)

# Убийство с координатами жертвы и убийцы в .ADM логе
KILL_COORDS_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)\s*\(id=(?P<victim_id>[^)\s]+)\s+pos=<(?P<vx>-?[0-9.]+),\s*(?P<vz>-?[0-9.]+),\s*-?[0-9.]+>\)\s+killed\s+by\s+Player\s+"(?P<killer>[^"]+)"\s*\(id=(?P<killer_id>[^)\s]+)\s+pos=<(?P<kx>-?[0-9.]+),\s*(?P<kz>-?[0-9.]+),\s*-?[0-9.]+>\)\s+with\s+(?P<weapon>.+?)(?:\s+from\s+(?P<distance>[0-9.]+)\s*meters?)?$',
    re.IGNORECASE,
)

# Стройка / размещение объектов игроком в .ADM логе
BUILD_ACTION_PATTERN = re.compile(
    r'(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<nick>[^"]+)"\s*\(id=(?P<player_id>[^)\s]+).*?\)\s+(?:placed\s+(?P<placed_item>.+?)\s+at\s+<(?P<px>-?[0-9.]+),\s*(?P<pz>-?[0-9.]+),\s*-?[0-9.]+>|built\s+(?P<built_item>.+?)\s+with\s+.+?\s+at\s+<(?P<bx>-?[0-9.]+),\s*(?P<bz>-?[0-9.]+),\s*-?[0-9.]+>)',
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
        h, m, s = map(int, parts)
        return h * 3600 + m * 60 + s
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


def _is_within_zone(x: float, z: float, center_x: float, center_z: float, radius: float) -> bool:
    """Проверяет попадание координат (x, z) в заданный радиус от центра зоны."""
    return math.hypot(x - center_x, z - center_z) <= radius


def _search_player_in_file(file_path: str, pattern: re.Pattern) -> str | None:
    """Синхронный поиск ID игрока в файле лога (выполняется в отдельном потоке)."""
    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            match = pattern.search(line)
            if match:
                return match.group(1)
    return None


def _search_player_by_short_id_in_file(file_path: str, short_id_lower: str) -> tuple[str, str] | None:
    """
    Синхронный поиск игрока по последним 8 символам ID в файле лога.
    Возвращает кортеж (game_nick, full_game_id) или None.
    """
    found: tuple[str, str] | None = None
    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            match = PLAYER_BY_ID_PATTERN.search(line)
            if match:
                full_id = match.group("full_id").strip()
                if len(full_id) >= 8 and full_id[-8:].lower() == short_id_lower:
                    found = (match.group("nick").strip(), full_id)
    return found
def _parse_adm_file(
    file_path: str,
    last_line_hash: str | None,
    is_first_run: bool,
    source_file_name: str | None = None,
) -> tuple[list[dict], str | None]:
    """Синхронный парсинг лога убийств и любых смертей с определением хедшотов."""
    kills_in_file = []
    last_found_hash = last_line_hash
    recent_headshots: dict[tuple[str, str], str] = {}
    file_prefix = source_file_name or os.path.basename(file_path)

    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line_idx, line in enumerate(f):
            hit_match = HEADSHOT_PATTERN.search(line)
            if hit_match:
                pair = (hit_match.group("killer"), hit_match.group("victim"))
                recent_headshots[pair] = hit_match.group("time")
                continue

            body_match = BODY_HIT_PATTERN.search(line)
            if body_match:
                pair = (body_match.group("killer"), body_match.group("victim"))
                recent_headshots.pop(pair, None)
                continue

            clean_line = line.strip()
            match = KILL_PATTERN.search(clean_line)
            if match:
                unique_payload = f"{file_prefix}:{line_idx}:{clean_line}"
                line_hash = hashlib.sha256(unique_payload.encode("utf-8")).hexdigest()
                data = match.groupdict()
                data["event_type"] = "pvp_kill"
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
                continue

            death_match = ANY_DEATH_PATTERN.search(clean_line)
            if death_match:
                unique_payload = f"{file_prefix}:{line_idx}:{clean_line}"
                line_hash = hashlib.sha256(unique_payload.encode("utf-8")).hexdigest()
                victim_nick = death_match.group("victim")
                kills_in_file.append({
                    "event_type": "pve_death",
                    "time": death_match.group("time"),
                    "victim": victim_nick,
                    "killer": victim_nick,
                    "weapon": "Окружение / Суицид",
                    "distance": "0",
                    "is_headshot": False,
                    "hash": line_hash,
                    **({"file_name": source_file_name} if source_file_name else {}),
                })

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
def _extract_latest_positions_from_adm(file_path: str) -> dict[str, tuple[float, float] | None]:
    """
    Читает .ADM лог сверху вниз и возвращает САМУЮ ПОСЛЕДНЮЮ известную позицию (x, z)
    для каждого игрока (lower_nick -> (x, z) или None, если игрок отключился).
    """
    latest_pos: dict[str, tuple[float, float] | None] = {}
    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if EMPTY_PLAYERLIST_PATTERN.search(line):
                for k in list(latest_pos.keys()):
                    latest_pos[k] = None
                continue

            m_pos = PLAYER_POS_PATTERN.search(line)
            if m_pos:
                nick_lower = m_pos.group("nick").strip().lower()
                latest_pos[nick_lower] = (float(m_pos.group("x")), float(m_pos.group("z")))
                continue

            m_disc = DISCONNECT_PATTERN.search(line)
            if m_disc:
                nick_lower = m_disc.group("nick").strip().lower()
                latest_pos[nick_lower] = None
    return latest_pos


def _extract_online_nicks_from_adm(file_path: str) -> list[str]:
    """
    Читает .ADM лог сверху вниз и возвращает список ников игроков,
    которые сейчас находятся на сервере (не отключились).
    """
    online_map: dict[str, str | None] = {}
    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if EMPTY_PLAYERLIST_PATTERN.search(line):
                for k in list(online_map.keys()):
                    online_map[k] = None
                continue

            m_pos = PLAYER_POS_PATTERN.search(line)
            if m_pos:
                raw_nick = m_pos.group("nick").strip()
                online_map[raw_nick.lower()] = raw_nick
                continue

            m_disc = DISCONNECT_PATTERN.search(line)
            if m_disc:
                nick_lower = m_disc.group("nick").strip().lower()
                online_map[nick_lower] = None

    return sorted([nick for nick in online_map.values() if nick is not None], key=str.lower)
def _parse_adm_zone_destructions(
    file_path: str,
    center_x: float,
    center_z: float,
    radius: float,
    allowed_classnames: set[str],
    last_line_hash: str | None,
    is_first_run: bool,
    source_file_name: str,
) -> tuple[list[dict], str | None]:
    """
    Ищет в .ADM логе попадания/уничтожение разрешённых предметов в радиусе зоны сдачи.
    """
    events = []
    seen_item_keys: set[str] = set()
    allowed_lower = {c.lower() for c in allowed_classnames}

    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line in f:
            clean_line = line.strip()
            if not clean_line:
                continue
            m = ADM_ITEM_HIT_PATTERN.search(clean_line)
            if m:
                classname = m.group("classname")
                if classname.lower() not in allowed_lower:
                    continue

                x = float(m.group("x"))
                z = float(m.group("z"))
                if not _is_within_zone(x, z, center_x, center_z, radius):
                    continue

                nick = m.group("nick").strip()
                time_str = m.group("time")
                dedup_key = (
                    f"{source_file_name}:{time_str[:5]}:{nick.lower()}:"
                    f"{classname.lower()}:{round(x * 2) / 2}:{round(z * 2) / 2}"
                )
                if dedup_key in seen_item_keys:
                    continue
                seen_item_keys.add(dedup_key)

                line_hash = hashlib.sha256(dedup_key.encode("utf-8")).hexdigest()
                events.append({
                    "time": time_str,
                    "nick": nick,
                    "classname": classname,
                    "x": x,
                    "z": z,
                    "hash": line_hash,
                    "file_name": source_file_name,
                })

    if not events:
        return [], last_line_hash

    if is_first_run:
        return [], events[-1]["hash"]

    if last_line_hash:
        for idx, item in enumerate(events):
            if item["hash"] == last_line_hash:
                new_items = events[idx + 1 :]
                new_last_hash = new_items[-1]["hash"] if new_items else last_line_hash
                return new_items, new_last_hash

    return events, events[-1]["hash"]


def _parse_adm_autoban_events(
    file_path: str,
    last_line_hash: str | None,
    is_first_run: bool,
    source_file_name: str,
) -> tuple[list[dict], str | None]:
    """Парсит события стройки и убийств с координатами из .ADM лога для проверки зон автобана."""
    events: list[dict] = []

    with open(file_path, mode="r", encoding="utf-8", errors="replace") as f:
        for line_idx, line in enumerate(f):
            clean = line.strip()
            if not clean:
                continue

            m_kill = KILL_COORDS_PATTERN.search(clean)
            if m_kill:
                line_hash = hashlib.sha256(f"{source_file_name}:{line_idx}:{clean}".encode("utf-8")).hexdigest()
                events.append({
                    "type": "kill",
                    "time": m_kill.group("time"),
                    "killer": m_kill.group("killer").strip(),
                    "killer_id": m_kill.group("killer_id").strip(),
                    "kx": float(m_kill.group("kx")),
                    "kz": float(m_kill.group("kz")),
                    "victim": m_kill.group("victim").strip(),
                    "vx": float(m_kill.group("vx")),
                    "vz": float(m_kill.group("vz")),
                    "weapon": (m_kill.group("weapon") or "").strip(),
                    "hash": line_hash,
                    "file_name": source_file_name,
                })
                continue

            m_build = BUILD_ACTION_PATTERN.search(clean)
            if m_build:
                line_hash = hashlib.sha256(f"{source_file_name}:{line_idx}:{clean}".encode("utf-8")).hexdigest()
                if m_build.group("px") is not None:
                    item_name = (m_build.group("placed_item") or "Постройка").strip()
                    bx = float(m_build.group("px"))
                    bz = float(m_build.group("pz"))
                else:
                    item_name = (m_build.group("built_item") or "Постройка").strip()
                    bx = float(m_build.group("bx"))
                    bz = float(m_build.group("bz"))

                events.append({
                    "type": "build",
                    "time": m_build.group("time"),
                    "nick": m_build.group("nick").strip(),
                    "player_id": m_build.group("player_id").strip(),
                    "item": item_name,
                    "x": bx,
                    "z": bz,
                    "hash": line_hash,
                    "file_name": source_file_name,
                })

    if not events:
        return [], last_line_hash

    if is_first_run:
        return [], events[-1]["hash"]

    if last_line_hash:
        for idx, ev in enumerate(events):
            if ev["hash"] == last_line_hash:
                new_items = events[idx + 1 :]
                new_hash = new_items[-1]["hash"] if new_items else last_line_hash
                return new_items, new_hash

    return events, events[-1]["hash"]


def _build_shop_event_element(root: ET.Element, classname: str) -> ET.Element:
    """Создаёт и добавляет стандартный узел <event name="Item_Shop_..."> в корень XML."""
    event_name = f"Item_Shop_{classname}"
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
    return ev
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
        """Получает актуальную информацию о сервере DayZ (GET https://nitrado.net{server_id}/gameservers)."""
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

    async def restart_server(self, message: str = "Плановый рестарт сервера через панель администратора.") -> bool:
        """Отправляет реальный запрос на перезагрузку игрового сервера через Nitrado API."""
        session = await self._get_session()
        restart_url = f"{self.api_url}/restart"
        payload = {"message": message, "restart_message": message}
        try:
            async with session.post(restart_url, json=payload) as response:
                if response.status in (200, 201, 202):
                    logger.info("[NITRADO] Запрос на рестарт сервера #%s успешно отправлен.", self.server_id)
                    return True
                resp_text = await response.text()
                logger.warning(
                    "[NITRADO] Ошибка при запросе рестарта (статус %s): %s",
                    response.status,
                    resp_text[:300],
                )
                return False
        except Exception as e:
            logger.error("[NITRADO] Исключение при запросе рестарта сервера: %s", e, exc_info=True)
            return False
    async def get_online_players(self, folder: str = "/dayzps/config") -> tuple[int, list[str]]:
        """
        Возвращает кортеж (количество_игроков_онлайн, список_ников)
        из API Nitrado и самого свежего .adm лога.
        """
        server_data = await self.get_server_status()
        query = (server_data or {}).get("query") or {}
        api_count = int(query.get("player_current") or 0)

        if not self._has_ftp_creds():
            return api_count, []

        downloaded_paths: list[str] = []
        temp_dir = tempfile.gettempdir()
        call_uid = uuid.uuid4().hex[:8]

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                first_adm = next(iter(adm_files), None)
                if not first_adm:
                    return api_count, []

                adm_local = os.path.join(temp_dir, f"adm_online_{call_uid}_{first_adm['name']}")
                downloaded_paths.append(adm_local)
                await self._download_ftp_file(client, first_adm["name"], adm_local)

                if not os.path.isfile(adm_local):
                    return api_count, []

                nicks = await asyncio.to_thread(_extract_online_nicks_from_adm, adm_local)
                actual_count = max(api_count, len(nicks)) if nicks else api_count
                return actual_count, nicks
        except Exception as e:
            logger.warning("[NITRADO] Не удалось получить список ников онлайн из .adm: %s", e)
            return api_count, []
        finally:
            for tmp_path in downloaded_paths:
                if os.path.isfile(tmp_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)

    async def close(self) -> None:
        """Закрывает HTTP-сессию при остановке бота."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_ban_list(self) -> list[str]:
        """
        Получает актуальный список забаненных игроков из настроек Nitrado
        (GET https://nitrado.net{server_id}/gameservers -> settings.general.bans).
        """
        server_data = await self.get_server_status()
        if not server_data:
            return []

        settings = server_data.get("settings") or {}
        general = settings.get("general") or {}
        raw_bans = general.get("bans") or ""

        if isinstance(raw_bans, list):
            return [str(x).strip() for x in raw_bans if str(x).strip()]

        bans = []
        for line in str(raw_bans).splitlines():
            clean = line.strip()
            if clean and not clean.startswith("//") and not clean.startswith("#"):
                bans.append(clean)
        return bans

    async def _save_ban_list(self, bans: list[str]) -> bool:
        """
        Сохраняет обновлённый список банов в веб-интерфейс Nitrado
        (POST https://nitrado.net{server_id}/gameservers/settings).
        """
        session = await self._get_session()
        settings_url = f"{self.api_url}/settings"
        unique_bans = list(dict.fromkeys(b.strip() for b in bans if b.strip()))
        payload = {
            "category": "general",
            "key": "bans",
            "value": "\n".join(unique_bans),
        }
        try:
            async with session.post(settings_url, json=payload) as response:
                if response.status in (200, 201, 202):
                    logger.info("[NITRADO] Список банов обновлён (всего записей: %d).", len(unique_bans))
                    return True
                resp_text = await response.text()
                logger.warning(
                    "[NITRADO] Ошибка сохранения бан-листа (статус %s): %s",
                    response.status,
                    resp_text[:300],
                )
                return False
        except Exception as e:
            logger.error("[NITRADO] Исключение при сохранении бан-листа: %s", e, exc_info=True)
            return False
    async def ban_player(self, identifier: str) -> bool:
        """Добавляет ник или ID игрока в бан-лист Nitrado."""
        clean_id = identifier.strip()
        if not clean_id:
            return False
        async with self._shop_file_lock:
            current_bans = await self.get_ban_list()
            if any(b.lower() == clean_id.lower() for b in current_bans):
                return True
            current_bans.append(clean_id)
            return await self._save_ban_list(current_bans)

    async def unban_player(self, identifier: str) -> bool:
        """Удаляет ник или ID игрока из бан-листа Nitrado."""
        clean_id = identifier.strip()
        if not clean_id:
            return False
        async with self._shop_file_lock:
            current_bans = await self.get_ban_list()
            new_bans = [b for b in current_bans if b.lower() != clean_id.lower()]
            if len(new_bans) == len(current_bans):
                return True
            return await self._save_ban_list(new_bans)

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

    async def _search_in_recent_adm_logs(
        self,
        folder: str,
        prefix: str,
        search_fn: Callable[[str], T | None],
        limit: int = 3,
    ) -> T | None:
        """Скачивает до `limit` свежих .adm файлов и применяет к ним синхронную функцию поиска."""
        downloaded_files: list[str] = []
        temp_dir = tempfile.gettempdir()
        call_uid = uuid.uuid4().hex[:8]

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                if not adm_files:
                    logger.warning("Файлы логов .adm не найдены в папке %s", folder)
                    return None

                for idx, file_info in enumerate(adm_files[:limit]):
                    file_name = file_info["name"]
                    local_temp_path = os.path.join(temp_dir, f"{prefix}_{call_uid}_{idx}_{file_name}")
                    downloaded_files.append(local_temp_path)

                    await self._download_ftp_file(client, file_name, local_temp_path)
                    if os.path.isfile(local_temp_path):
                        result = await asyncio.to_thread(search_fn, local_temp_path)
                        if result is not None:
                            return result
            return None
        finally:
            for temp_file in downloaded_files:
                if os.path.isfile(temp_file):
                    with contextlib.suppress(OSError):
                        os.remove(temp_file)

    async def get_latest_adm_filename(self, logs_folder: str = "/dayzps/config") -> str | None:
        """Возвращает имя самого свежего .adm лога на FTP для отслеживания рестартов."""
        if not self._has_ftp_creds():
            return None
        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, logs_folder)
                first_adm = next(iter(adm_files), None)
                return first_adm["name"] if first_adm else None
        except Exception as e:
            logger.warning("[MONITOR] Не удалось получить имя свежего .adm файла: %s", e)
            return None
    async def find_player_id_in_logs(self, psn_nick: str, folder: str = "/dayzps/config") -> str | None:
        """
        Подключается по FTP, сортирует логи и проверяет 3 самых свежих файла .adm,
        чтобы гарантированно найти ID игрока по нику.
        """
        if not self._has_ftp_creds():
            return None

        escaped_nick = re.escape(psn_nick.strip())
        pattern = re.compile(rf'Player\s+"{escaped_nick}"\s*\(id=([^)]+)\)', re.IGNORECASE)

        try:
            return await self._search_in_recent_adm_logs(
                folder=folder,
                prefix="dayz_find",
                search_fn=lambda path: _search_player_in_file(path, pattern),
            )
        except Exception as e:
            logger.error("Ошибка при поиске игрока через aioftp: %s", e, exc_info=True)
            return None

    async def find_player_by_short_id(
        self,
        short_id: str,
        folder: str = "/dayzps/config",
    ) -> tuple[str, str] | None:
        """
        Подключается по FTP, сортирует логи и поочередно проверяет 3 самых свежих файла .adm,
        чтобы найти игрока по последним 8 символам его ID.
        Возвращает кортеж (game_nick, full_game_id) или None.
        """
        if not self._has_ftp_creds():
            return None

        short_id_lower = short_id.strip().lower()
        if len(short_id_lower) != 8:
            return None

        try:
            return await self._search_in_recent_adm_logs(
                folder=folder,
                prefix="dayz_find_id",
                search_fn=lambda path: _search_player_by_short_id_in_file(path, short_id_lower),
            )
        except Exception as e:
            logger.error("Ошибка при поиске игрока по ID '%s' через aioftp: %s", short_id, e, exc_info=True)
            raise

    async def get_new_kills(
        self,
        last_file_name: str | None,
        last_line_hash: str | None,
        folder: str = "/dayzps/config",
    ) -> tuple[list[dict], str | None, str | None]:
        """
        Скачивает свежий .adm лог во временную папку, парсит новые убийства и смерти по хэшу строки
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
                    return [], last_file_name, last_line_hash

                latest_file = next(iter(adm_files))
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
                            "[KILL_LOG] Проверка завершена: файл=%s, новых событий=%d",
                            current_file_name,
                            len(all_new_kills),
                        )
                    return all_new_kills, current_file_name, updated_hash

        except Exception as e:
            logger.error("Ошибка при парсинге логов убийств через aioftp: %s", e, exc_info=True)
        finally:
            for tmp_path in downloaded_paths:
                if os.path.isfile(tmp_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)

        return [], last_file_name, last_line_hash
    async def get_players_in_sell_zone(
        self,
        center_x: float,
        center_z: float,
        radius: float,
        folder: str = "/dayzps/config",
    ) -> set[str]:
        """Возвращает множество ников (в нижнем регистре), находящихся в зоне сдачи."""
        if not self._has_ftp_creds():
            return set()

        downloaded_paths: list[str] = []
        temp_dir = tempfile.gettempdir()
        call_uid = uuid.uuid4().hex[:8]

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                first_adm = next(iter(adm_files), None)
                if not first_adm:
                    return set()

                adm_local = os.path.join(temp_dir, f"adm_zone_{call_uid}_{first_adm['name']}")
                downloaded_paths.append(adm_local)
                await self._download_ftp_file(client, first_adm["name"], adm_local)

                if not os.path.isfile(adm_local):
                    return set()

                latest_positions = await asyncio.to_thread(_extract_latest_positions_from_adm, adm_local)
                inside_nicks: set[str] = set()
                for nick_lower, coords in latest_positions.items():
                    if coords is not None:
                        px, pz = coords
                        if _is_within_zone(px, pz, center_x, center_z, radius):
                            inside_nicks.add(nick_lower)
                return inside_nicks

        except Exception as e:
            logger.error("[LOOT_DROP] Ошибка проверки позиций игроков в торговой зоне: %s", e)
            return set()
        finally:
            for tmp_path in downloaded_paths:
                if os.path.isfile(tmp_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)

    async def is_player_at_sell_zone(
        self,
        game_nick: str,
        center_x: float,
        center_z: float,
        radius: float,
        folder: str = "/dayzps/config",
    ) -> bool:
        """Проверяет по самой последней записи в свежем .ADM логе, находится ли игрок в зоне сдачи."""
        inside_nicks = await self.get_players_in_sell_zone(center_x, center_z, radius, folder=folder)
        return game_nick.strip().lower() in inside_nicks

    async def get_new_autoban_events(
        self,
        last_file_name: str | None,
        last_line_hash: str | None,
        folder: str = "/dayzps/config",
    ) -> tuple[list[dict], str | None, str | None]:
        """Скачивает свежий .ADM лог и возвращает новые события стройки и убийств с координатами."""
        if not self._has_ftp_creds():
            return [], last_file_name, last_line_hash

        downloaded_paths: list[str] = []
        temp_dir = tempfile.gettempdir()
        call_uid = uuid.uuid4().hex[:8]
        is_first_run = not last_file_name and not last_line_hash

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                if not adm_files:
                    return [], last_file_name, last_line_hash

                latest_file = next(iter(adm_files))
                current_file_name = latest_file["name"]
                all_events: list[dict] = []

                if not is_first_run and last_file_name and current_file_name != last_file_name:
                    prev_file = next((f for f in adm_files if f["name"] == last_file_name), None)
                    if prev_file:
                        prev_local = os.path.join(temp_dir, f"aban_prev_{call_uid}_{prev_file['name']}")
                        downloaded_paths.append(prev_local)
                        await self._download_ftp_file(client, prev_file["name"], prev_local)
                        if os.path.isfile(prev_local):
                            prev_evs, _ = await asyncio.to_thread(
                                _parse_adm_autoban_events,
                                prev_local,
                                last_line_hash,
                                False,
                                prev_file["name"],
                            )
                            all_events.extend(prev_evs)

                effective_hash = last_line_hash if current_file_name == last_file_name else None
                curr_local = os.path.join(temp_dir, f"aban_{call_uid}_{current_file_name}")
                downloaded_paths.append(curr_local)
                await self._download_ftp_file(client, current_file_name, curr_local)

                updated_hash = effective_hash
                if os.path.isfile(curr_local):
                    new_evs, updated_hash = await asyncio.to_thread(
                        _parse_adm_autoban_events,
                        curr_local,
                        effective_hash,
                        is_first_run,
                        current_file_name,
                    )
                    all_events.extend(new_evs)

                return all_events, current_file_name, updated_hash
        except Exception as e:
            logger.error("[AUTOBAN] Ошибка парсинга .ADM логов: %s", e, exc_info=True)
            return [], last_file_name, last_line_hash
        finally:
            for tmp_path in downloaded_paths:
                if os.path.isfile(tmp_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)
    async def get_new_sell_zone_cleanups(
        self,
        center_x: float,
        center_z: float,
        radius: float,
        allowed_classnames: set[str],
        last_rpt_file: str | None,
        last_rpt_hash: str | None,
        folder: str = "/dayzps/config",
    ) -> tuple[list[dict], str | None, str | None]:
        """Скачивает .ADM лог и ищет уничтожение предметов в радиусе зоны сдачи."""
        if not self._has_ftp_creds() or not allowed_classnames:
            return [], last_rpt_file, last_rpt_hash

        downloaded_paths: list[str] = []
        temp_dir = tempfile.gettempdir()
        call_uid = uuid.uuid4().hex[:8]
        is_first_run = not last_rpt_file and not last_rpt_hash

        try:
            async with self._ftp_client() as client:
                adm_files = await self._list_adm_files(client, folder)
                if not adm_files:
                    return [], last_rpt_file, last_rpt_hash

                latest_adm = next(iter(adm_files))
                current_adm_name = latest_adm["name"]
                all_cleanups: list[dict] = []

                if not is_first_run and last_rpt_file and current_adm_name != last_rpt_file:
                    prev_adm = next((f for f in adm_files if f["name"] == last_rpt_file), None)
                    if prev_adm:
                        prev_local = os.path.join(temp_dir, f"adm_sell_prev_{call_uid}_{prev_adm['name']}")
                        downloaded_paths.append(prev_local)
                        await self._download_ftp_file(client, prev_adm["name"], prev_local)
                        if os.path.isfile(prev_local):
                            prev_items, _ = await asyncio.to_thread(
                                _parse_adm_zone_destructions,
                                prev_local,
                                center_x,
                                center_z,
                                radius,
                                allowed_classnames,
                                last_rpt_hash,
                                False,
                                prev_adm["name"],
                            )
                            all_cleanups.extend(prev_items)

                effective_hash = last_rpt_hash if current_adm_name == last_rpt_file else None
                curr_local = os.path.join(temp_dir, f"adm_sell_{call_uid}_{current_adm_name}")
                downloaded_paths.append(curr_local)
                await self._download_ftp_file(client, current_adm_name, curr_local)

                updated_hash = effective_hash
                if os.path.isfile(curr_local):
                    new_items, updated_hash = await asyncio.to_thread(
                        _parse_adm_zone_destructions,
                        curr_local,
                        center_x,
                        center_z,
                        radius,
                        allowed_classnames,
                        effective_hash,
                        is_first_run,
                        current_adm_name,
                    )
                    all_cleanups.extend(new_items)

                return all_cleanups, current_adm_name, updated_hash

        except Exception as e:
            logger.error("[LOOT_DROP] Ошибка парсинга уничтожения предметов в .adm: %s", e, exc_info=True)
            return [], last_rpt_file, last_rpt_hash
        finally:
            for tmp_path in downloaded_paths:
                if os.path.isfile(tmp_path):
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)

    async def ensure_shop_event_exists(
        self,
        classname: str,
        remote_events_path: str = DEFAULT_SHOP_EVENTS_PATH,
    ) -> bool:
        """Создаёт запись <event name="Item_Shop_..."> в shop_events.xml, если её ещё нет."""
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

                    _build_shop_event_element(root, classname)

                    _indent_xml(root)
                    xml_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
                        root, encoding="unicode"
                    )
                    await self._upload_ftp_text(client, remote_events_path, xml_out)
                    logger.info("Эвент %s зарегистрирован в %s.", event_name, remote_events_path)
                    return True
            except Exception as e:
                logger.error("Ошибка регистрации эвента %s в XML: %s", event_name, e, exc_info=True)
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
        """Увеличивает nominal/max в shop_events.xml и добавляет точки спавна в cfgeventspawns.xml."""
        if not self._has_ftp_creds():
            return False
        if not items:
            return True

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
                    spawns_root = ET.fromstring(raw_spawns) if raw_spawns.strip() else ET.Element("eventposdef")

                    for item in items:
                        classname = item["game_classname"]
                        qty = int(item.get("qty", 1))
                        event_name = f"Item_Shop_{classname}"

                        target_ev = next(
                            (e for e in events_root.findall("event") if e.get("name") == event_name),
                            None,
                        )
                        if target_ev is None:
                            target_ev = _build_shop_event_element(events_root, classname)

                        nom_tag = target_ev.find("nominal")
                        max_tag = target_ev.find("max")
                        current_nom = int(nom_tag.text or 0) if nom_tag is not None else 0
                        new_nom = current_nom + qty
                        if nom_tag is not None:
                            nom_tag.text = str(new_nom)
                        if max_tag is not None:
                            max_tag.text = str(new_nom)

                        spawn_ev = next(
                            (e for e in spawns_root.findall("event") if e.get("name") == event_name),
                            None,
                        )
                        if spawn_ev is None:
                            spawn_ev = ET.SubElement(spawns_root, "event", {"name": event_name})

                        zone = spawn_ev.find("zone")
                        if zone is None:
                            ET.SubElement(spawn_ev, "zone", {
                                "smin": "0",
                                "smax": "0",
                                "dmin": "0",
                                "dmax": "0",
                                "r": str(int(radius)),
                            })

                        for _ in range(qty):
                            angle = random.uniform(0, 2 * math.pi)
                            dist = radius * math.sqrt(random.random())
                            spawn_x = round(x + dist * math.cos(angle), 2)
                            spawn_z = round(z + dist * math.sin(angle), 2)
                            ET.SubElement(spawn_ev, "pos", {
                                "x": str(spawn_x),
                                "z": str(spawn_z),
                                "a": str(random.randint(0, 359)),
                            })

                    _indent_xml(events_root)
                    events_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
                        events_root, encoding="unicode"
                    )
                    await self._upload_ftp_text(client, remote_events_path, events_out)

                    _indent_xml(spawns_root)
                    spawns_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
                        spawns_root, encoding="unicode"
                    )
                    await self._upload_ftp_text(client, remote_spawns_path, spawns_out)

                    logger.info("Заказ магазина записан в XML: %d поз. в районе X=%s, Z=%s", len(items), x, z)
                    return True
            except Exception as e:
                logger.error("Ошибка записи заказа в XML на FTP: %s", e, exc_info=True)
                return False

    async def clear_event_spawn_orders(
        self,
        remote_events_path: str = DEFAULT_SHOP_EVENTS_PATH,
        remote_spawns_path: str = DEFAULT_EVENT_SPAWNS_PATH,
    ) -> bool:
        """Обнуляет nominal/max в shop_events.xml и удаляет точки спавна из cfgeventspawns.xml."""
        if not self._has_ftp_creds():
            return False

        async with self._shop_file_lock:
            try:
                async with self._ftp_client() as client:
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
                            events_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
                                events_root, encoding="unicode"
                            )
                            await self._upload_ftp_text(client, remote_events_path, events_out)
                    except Exception as ev_err:
                        logger.warning("Не удалось очистить shop_events.xml: %s", ev_err)

                    raw_spawns = await self._download_ftp_text(client, remote_spawns_path)
                    if raw_spawns.strip():
                        spawns_root = ET.fromstring(raw_spawns)
                        for ev in list(spawns_root.findall("event")):
                            if (ev.get("name") or "").startswith("Item_Shop_"):
                                spawns_root.remove(ev)
                        _indent_xml(spawns_root)
                        spawns_out = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(
                            spawns_root, encoding="unicode"
                        )
                        await self._upload_ftp_text(client, remote_spawns_path, spawns_out)

                    logger.info("Эвенты магазина успешно очищены после рестарта.")
                    return True
            except Exception as e:
                logger.error("Ошибка очистки XML эвентов: %s", e, exc_info=True)
                return False

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

    async def get_raid_mode(
        self,
        remote_gameplay_path: str = DEFAULT_GAMEPLAY_JSON_PATH,
    ) -> bool | None:
        """
        Читает cfggameplay.json и возвращает:
        - True, если рейды включены (disableBaseDamage == False)
        - False, если рейды выключены (disableBaseDamage == True)
        - None при ошибке чтения.
        """
        if not self._has_ftp_creds():
            return None
        try:
            async with self._ftp_client() as client:
                raw_json = await self._download_ftp_text(client, remote_gameplay_path)
                data = json.loads(raw_json)
                general = data.get("GeneralData") or {}
                disable_base = bool(general.get("disableBaseDamage", False))
                return not disable_base
        except Exception as e:
            logger.error("[RAID] Ошибка чтения cfggameplay.json: %s", e)
            return None

    async def set_raid_mode(
        self,
        raid_enabled: bool,
        remote_gameplay_path: str = DEFAULT_GAMEPLAY_JSON_PATH,
    ) -> bool:
        """
        Обновляет cfggameplay.json (версия 123):
        - raid_enabled=True  -> disableBaseDamage=False, disableContainerDamage=False (рейды ВКЛ)
        - raid_enabled=False -> disableBaseDamage=True,  disableContainerDamage=True  (рейды ВЫКЛ)
        Изменения вступят в силу на сервере после ближайшего рестарта.
        """
        if not self._has_ftp_creds():
            return False

        disable_flag = not raid_enabled

        async with self._shop_file_lock:
            try:
                async with self._ftp_client() as client:
                    raw_json = await self._download_ftp_text(client, remote_gameplay_path)
                    data = json.loads(raw_json)

                    general_data = data.setdefault("GeneralData", {})
                    general_data["disableBaseDamage"] = disable_flag
                    general_data["disableContainerDamage"] = disable_flag

                    updated_json = json.dumps(data, indent="\t", ensure_ascii=False)
                    await self._upload_ftp_text(client, remote_gameplay_path, updated_json)

                    logger.info(
                        "[RAID] Файл cfggameplay.json обновлён: рейды=%s (disableBaseDamage=%s, disableContainerDamage=%s)",
                        "ВКЛ" if raid_enabled else "ВЫКЛ",
                        disable_flag,
                        disable_flag,
                    )
                    return True
            except Exception as e:
                logger.error("[RAID] Ошибка обновления cfggameplay.json: %s", e, exc_info=True)
                return False

