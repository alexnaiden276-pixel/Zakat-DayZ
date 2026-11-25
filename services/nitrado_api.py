import asyncio
import hashlib
import logging
import math
import re
import xml.etree.ElementTree as ET
from ftplib import FTP, error_perm
from io import BytesIO
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Паттерны для поиска ID и ника игрока в логах подключения (.ADM)
CONNECT_PATTERNS = [
    re.compile(r'Player\s+"(?P<nick>[^"]+)"\s*\(id=(?P<id>[A-Za-z0-9_-]{8,})'),
    re.compile(r'Player\s+"(?P<nick>[^"]+)"\s+is\s+connected\s*\(id=(?P<id>[A-Za-z0-9_-]{8,})'),
    re.compile(r'Player\s+"(?P<nick>[^"]+)"\s*\[id=(?P<id>[A-Za-z0-9_-]{8,})'),
]

# Паттерн отключения игрока от сервера
DISCONNECT_PATTERN = re.compile(
    r'Player\s+"(?P<nick>[^"]+)"(?:\s*\(id=(?P<id>[A-Za-z0-9_-]{8,})\))?\s+has\s+been\s+disconnected',
    re.IGNORECASE,
)

# Паттерн периодического дампа координат игроков в .ADM
PLAYER_POS_PATTERN = re.compile(
    r'Player\s+"(?P<nick>[^"]+)"\s*\(id=(?P<id>[A-Za-z0-9_-]+)(?:\s*,\s*pos=<(?P<x>-?\d+(?:\.\d+)?)\s*,\s*(?P<z>-?\d+(?:\.\d+)?)\s*,\s*(?P<y>-?\d+(?:\.\d+)?)>)?\)',
    re.IGNORECASE,
)

# Паттерны для парсинга PvP-убийств в DayZ .ADM логах
KILL_PATTERN = re.compile(
    r'^(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)'
    r'(?:\s*\(id=(?P<victim_id>[A-Za-z0-9_-]+)'
    r'(?:\s*,\s*pos=<(?P<vx>-?\d+(?:\.\d+)?)\s*,\s*(?P<vz>-?\d+(?:\.\d+)?)\s*,\s*(?P<vy>-?\d+(?:\.\d+)?)>)?\))?'
    r'.*?killed\s+by\s+Player\s+"(?P<killer>[^"]+)"'
    r'(?:\s*\(id=(?P<killer_id>[A-Za-z0-9_-]+)'
    r'(?:\s*,\s*pos=<(?P<kx>-?\d+(?:\.\d+)?)\s*,\s*(?P<kz>-?\d+(?:\.\d+)?)\s*,\s*(?P<ky>-?\d+(?:\.\d+)?)>)?\))?'
    r'(?:\s+with\s+(?P<weapon>.*?))?(?:\s+from\s+(?P<distance>\d+(?:\.\d+)?)\s*meters)?\s*$',
    re.IGNORECASE,
)

# Паттерн любой смерти игрока (зомби, падение, голод, суицид и т.д.)
ANY_DEATH_PATTERN = re.compile(
    r'^(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)',
    re.IGNORECASE,
)

# Паттерны попаданий для определения хедшотов
HIT_PATTERN = re.compile(
    r'^(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(id=[^)]+\)\s*'
    r'hit\s+by\s+Player\s+"(?P<attacker>[^"]+)"\s*\(id=[^)]+\)\s*'
    r'into\s+(?P<zone>[^(]+)\(',
    re.IGNORECASE,
)

# Паттерн постройки/установки объектов игроком (для гео-автобана)
BUILD_ACTION_PATTERN = re.compile(
    r'^(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<nick>[^"]+)"'
    r'\s*\(id=(?P<id>[A-Za-z0-9_-]+)\s*,\s*pos=<(?P<x>-?\d+(?:\.\d+)?)\s*,\s*(?P<z>-?\d+(?:\.\d+)?)\s*,\s*(?P<y>-?\d+(?:\.\d+)?)>\)'
    r'\s*(?:placed|built)\s+(?P<item>.+?)\s*$',
    re.IGNORECASE,
)


def _line_hash(line: str) -> str:
    return hashlib.sha1(line.strip().encode("utf-8", errors="ignore")).hexdigest()


def _parse_adm_seconds(time_str: str) -> int | None:
    try:
        h, m, s = map(int, time_str.split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def _is_within_seconds(t1: str, t2: str, max_diff: int = 3) -> bool:
    s1 = _parse_adm_seconds(t1)
    s2 = _parse_adm_seconds(t2)
    if s1 is None or s2 is None:
        return False
    diff = abs(s2 - s1)
    if diff > 43200:  # переход через полночь (23:59:59 -> 00:00:01)
        diff = 86400 - diff
    return diff <= max_diff


def _calc_distance_2d(
    kx: str | None,
    kz: str | None,
    vx: str | None,
    vz: str | None,
) -> float:
    if None in (kx, kz, vx, vz):
        return 0.0
    try:
        dx = float(kx) - float(vx)
        dz = float(kz) - float(vz)
        return round(math.hypot(dx, dz), 1)
    except (TypeError, ValueError):
        return 0.0
def _parse_adm_file(
    file_name: str,
    content: str,
    stop_at_hash: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Парсит один .ADM файл, возвращает список событий (pvp_kill и pve_death) в хронологическом порядке
    и хэш самой последней (нижней) непустой строки файла.
    """
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return [], None

    newest_hash = _line_hash(lines[-1])
    new_lines: list[str] = []

    if stop_at_hash:
        found_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if _line_hash(lines[idx]) == stop_at_hash:
                found_idx = idx
                break
        if found_idx is not None:
            new_lines = lines[found_idx + 1:]
        else:
            new_lines = lines
    else:
        new_lines = lines

    events: list[dict[str, Any]] = []
    last_headshots: dict[tuple[str, str], str] = {}
    recent_pvp_deaths: dict[str, str] = {}

    for line in new_lines:
        hit_match = HIT_PATTERN.match(line)
        if hit_match:
            zone = hit_match.group("zone").strip().lower()
            if "head" in zone or "brain" in zone:
                key = (
                    hit_match.group("attacker").strip().lower(),
                    hit_match.group("victim").strip().lower(),
                )
                last_headshots[key] = hit_match.group("time")
            continue

        match = KILL_PATTERN.match(line)
        if match:
            data = match.groupdict()
            weapon_raw = (data.get("weapon") or "").strip()
            weapon_clean = re.sub(r'\s*\([^)]*\)\s*$', '', weapon_raw).strip()
            if not weapon_clean:
                weapon_clean = "Неизвестно"

            dist_val = float(data["distance"]) if data.get("distance") else 0.0
            if dist_val <= 0:
                dist_val = _calc_distance_2d(
                    data.get("kx"), data.get("kz"), data.get("vx"), data.get("vz")
                )

            pair_key = (data["killer"].strip().lower(), data["victim"].strip().lower())
            hs_time = last_headshots.pop(pair_key, None)
            is_headshot = bool(hs_time and _is_within_seconds(hs_time, data["time"], max_diff=3))
            recent_pvp_deaths[data["victim"].strip().lower()] = data["time"]

            events.append({
                "type": "pvp_kill",
                "file": file_name,
                "time": data["time"],
                "killer": data["killer"],
                "victim": data["victim"],
                "weapon": weapon_clean,
                "distance": dist_val,
                "is_headshot": is_headshot,
                "kx": float(data["kx"]) if data.get("kx") is not None else None,
                "kz": float(data["kz"]) if data.get("kz") is not None else None,
                "vx": float(data["vx"]) if data.get("vx") is not None else None,
                "vz": float(data["vz"]) if data.get("vz") is not None else None,
            })
            continue

        death_match = ANY_DEATH_PATTERN.match(line)
        if death_match:
            victim_nick = death_match.group("victim")
            prev_pvp_time = recent_pvp_deaths.get(victim_nick.strip().lower())
            if prev_pvp_time and _is_within_seconds(prev_pvp_time, death_match.group("time"), max_diff=3):
                continue

            events.append({
                "type": "pve_death",
                "file": file_name,
                "time": death_match.group("time"),
                "victim": victim_nick,
            })

    return events, newest_hash


def _parse_adm_autoban_events(
    file_name: str,
    content: str,
    stop_at_hash: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Парсит события постройки объектов и PvP-убийств с координатами для службы гео-автобана."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return [], None

    newest_hash = _line_hash(lines[-1])
    if stop_at_hash:
        found_idx = None
        for idx in range(len(lines) - 1, -1, -1):
            if _line_hash(lines[idx]) == stop_at_hash:
                found_idx = idx
                break
        new_lines = lines[found_idx + 1:] if found_idx is not None else lines
    else:
        new_lines = lines

    events: list[dict[str, Any]] = []
    for line in new_lines:
        build_m = BUILD_ACTION_PATTERN.match(line)
        if build_m:
            d = build_m.groupdict()
            events.append({
                "type": "build",
                "file": file_name,
                "time": d["time"],
                "nick": d["nick"],
                "player_id": d["id"],
                "x": float(d["x"]),
                "z": float(d["z"]),
                "item": d["item"].strip(),
            })
            continue

        kill_m = KILL_PATTERN.match(line)
        if kill_m:
            d = kill_m.groupdict()
            if None not in (d.get("kx"), d.get("kz"), d.get("vx"), d.get("vz")):
                events.append({
                    "type": "kill",
                    "file": file_name,
                    "time": d["time"],
                    "killer": d["killer"],
                    "killer_id": d.get("killer_id"),
                    "victim": d["victim"],
                    "kx": float(d["kx"]),
                    "kz": float(d["kz"]),
                    "vx": float(d["vx"]),
                    "vz": float(d["vz"]),
                })

    return events, newest_hash
class NitradoAPI:
    BASE_URL = "https://api.nitrado.net"

    def __init__(
        self,
        token: str,
        server_id: str | int | None = None,
        service_id: str | int | None = None,
        ftp_host: str | None = None,
        ftp_port: int | str | None = None,
        ftp_user: str | None = None,
        ftp_pass: str | None = None,
        **_kwargs: Any,
    ):
        resolved_id = server_id if server_id is not None else service_id
        if resolved_id is None:
            raise ValueError("Необходимо передать server_id или service_id")

        self.token = token
        self.service_id = str(resolved_id)
        self.ftp_host = (ftp_host or "").strip() or None
        self.ftp_port = int(ftp_port) if ftp_port else 21
        self.ftp_user = (ftp_user or "").strip() or None
        self.ftp_pass = (ftp_pass or "").strip() or None

        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def get_server_status(self) -> dict[str, Any] | None:
        url = f"{self.BASE_URL}/services/{self.service_id}/gameservers"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("Nitrado API error [%s]: %s", resp.status, text)
                    return None
                payload = await resp.json()
                return payload.get("data", {}).get("gameserver", {})
        except asyncio.TimeoutError:
            logger.error("Таймаут при запросе к Nitrado API")
            return None
        except Exception as e:
            logger.error("Ошибка при запросе к Nitrado API: %s", e)
            return None

    async def restart_server(self) -> bool:
        url = f"{self.BASE_URL}/services/{self.service_id}/gameservers/restart"
        session = await self._get_session()
        try:
            async with session.post(url, json={"message": "Server restart requested by Telegram Bot"}) as resp:
                if resp.status in (200, 201, 202):
                    return True
                text = await resp.text()
                logger.error("Ошибка рестарта Nitrado [%s]: %s", resp.status, text)
                return False
        except Exception as e:
            logger.error("Исключение при рестарте Nitrado: %s", e)
            return False

    async def stop_server(self) -> bool:
        """Останавливает игровой сервер через Nitrado API."""
        url = f"{self.BASE_URL}/services/{self.service_id}/gameservers/stop"
        session = await self._get_session()
        try:
            async with session.post(url, json={"message": "Server stop for scheduled wipe"}) as resp:
                if resp.status in (200, 201, 202):
                    return True
                text = await resp.text()
                logger.error("Ошибка остановки Nitrado [%s]: %s", resp.status, text)
                return False
        except Exception as e:
            logger.error("Исключение при остановке Nitrado: %s", e)
            return False

    async def start_server(self) -> bool:
        """Запускает игровой сервер через Nitrado API (использует endpoint restart)."""
        return await self.restart_server()
    async def get_settings(self) -> dict[str, Any] | None:
        """Возвращает словарь настроек Nitrado (category -> key -> value)."""
        gs = await self.get_server_status()
        if not gs:
            return None
        return gs.get("settings") or {}

    async def update_setting(self, category: str, key: str, value: str) -> bool:
        """Обновляет параметр конфигурации сервера в Nitrado Settings API."""
        url = f"{self.BASE_URL}/services/{self.service_id}/gameservers/settings"
        session = await self._get_session()
        try:
            async with session.post(url, json={"category": category, "key": key, "value": value}) as resp:
                if resp.status in (200, 201, 202):
                    return True
                text = await resp.text()
                logger.error("Ошибка обновления настройки Nitrado %s.%s [%s]: %s", category, key, resp.status, text)
                return False
        except Exception as e:
            logger.error("Исключение при обновлении настройки Nitrado %s.%s: %s", category, key, e)
            return False

    async def get_ban_list(self) -> list[str]:
        """Возвращает актуальный список забаненных игроков из настроек Nitrado (general.bans)."""
        settings_dict = await self.get_settings()
        if not settings_dict:
            return []
        raw_bans = (settings_dict.get("general") or {}).get("bans", "") or ""
        return [line.strip() for line in str(raw_bans).splitlines() if line.strip()]

    async def ban_player(self, identifier: str) -> bool:
        """Добавляет ник/ID игрока в бан-лист Nitrado (general.bans)."""
        clean_id = identifier.strip()
        if not clean_id:
            return False
        current = await self.get_ban_list()
        if any(item.lower() == clean_id.lower() for item in current):
            return True
        current.append(clean_id)
        return await self.update_setting("general", "bans", "\n".join(current))

    async def unban_player(self, identifier: str) -> bool:
        """Удаляет ник/ID игрока из бан-листа Nitrado (general.bans)."""
        clean_id = identifier.strip()
        if not clean_id:
            return False
        current = await self.get_ban_list()
        updated = [item for item in current if item.lower() != clean_id.lower()]
        if len(updated) == len(current):
            return True
        return await self.update_setting("general", "bans", "\n".join(updated))

    async def _get_ftp_credentials(self) -> tuple[str, int, str, str] | None:
        if self.ftp_host and self.ftp_user and self.ftp_pass:
            return self.ftp_host, self.ftp_port, self.ftp_user, self.ftp_pass

        gs = await self.get_server_status()
        if not gs:
            return None
        creds = gs.get("credentials", {}).get("ftp", {})
        host = creds.get("hostname")
        port = int(creds.get("port", 21))
        username = creds.get("username")
        password = creds.get("password")
        if not all([host, username, password]):
            logger.error("Nitrado не вернул FTP-реквизиты для сервера %s", self.service_id)
            return None
        return host, port, username, password
    @staticmethod
    def _find_adm_files_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        limit: int = 3,
    ) -> tuple[str, list[str]]:
        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            candidate_dirs = [
                "/dayzps/config",
                "/dayzxb/config",
                "/dayz/config",
                "/config",
                "/",
            ]
            target_dir = ""
            adm_files: list[str] = []

            for folder in candidate_dirs:
                try:
                    ftp.cwd(folder)
                    files = ftp.nlst()
                    found = [f for f in files if f.lower().endswith(".adm")]
                    if found:
                        target_dir = folder
                        adm_files = found
                        break
                except error_perm:
                    continue

            if not adm_files:
                return "", []

            adm_files.sort(reverse=True)
            return target_dir, adm_files[:limit]
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def get_latest_adm_filename(self) -> str | None:
        """Возвращает имя самого свежего .ADM лог-файла на сервере."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return None
        try:
            _, files = await asyncio.to_thread(self._find_adm_files_sync, *creds, 1)
            return files if files else None
        except Exception as e:
            logger.error("Ошибка при получении имени .ADM файла: %s", e)
            return None

    @staticmethod
    def _search_short_id_via_ftp_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        short_id_suffix: str,
    ) -> tuple[str, str] | None:
        target_suffix = short_id_suffix.strip().lower()
        target_dir, recent_files = NitradoAPI._find_adm_files_sync(
            host, port, username, password, limit=3
        )
        if not recent_files:
            return None

        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            ftp.cwd(target_dir)
            for fname in recent_files:
                buf = BytesIO()
                ftp.retrbinary(f"RETR {fname}", buf.write)
                text = buf.getvalue().decode("utf-8", errors="ignore")

                for line in reversed(text.splitlines()):
                    for pattern in CONNECT_PATTERNS:
                        match = pattern.search(line)
                        if match:
                            nick = match.group("nick").strip()
                            full_id = match.group("id").strip()
                            if full_id.lower().endswith(target_suffix):
                                return nick, full_id
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        return None

    async def find_player_by_short_id(self, short_id_suffix: str) -> tuple[str, str] | None:
        creds = await self._get_ftp_credentials()
        if not creds:
            return None
        try:
            return await asyncio.to_thread(
                self._search_short_id_via_ftp_sync,
                *creds,
                short_id_suffix,
            )
        except Exception as e:
            logger.error("Ошибка чтения FTP логов Nitrado: %s", e)
            return None
    @staticmethod
    def _search_id_by_nick_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        target_nick: str,
    ) -> str | None:
        clean_target = target_nick.strip().lower()
        target_dir, recent_files = NitradoAPI._find_adm_files_sync(
            host, port, username, password, limit=4
        )
        if not recent_files:
            return None

        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            ftp.cwd(target_dir)
            for fname in recent_files:
                buf = BytesIO()
                ftp.retrbinary(f"RETR {fname}", buf.write)
                text = buf.getvalue().decode("utf-8", errors="ignore")

                for line in reversed(text.splitlines()):
                    for pattern in CONNECT_PATTERNS:
                        match = pattern.search(line)
                        if match:
                            nick = match.group("nick").strip().lower()
                            full_id = match.group("id").strip()
                            if nick == clean_target:
                                return full_id
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()
        return None

    async def find_player_id_in_logs(self, nick: str) -> str | None:
        """Ищет уникальный ID игрока по его нику в последних .ADM логах."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return None
        try:
            return await asyncio.to_thread(self._search_id_by_nick_sync, *creds, nick)
        except Exception as e:
            logger.error("Ошибка при поиске ID игрока %s в логах: %s", nick, e)
            return None

    @staticmethod
    def _parse_online_from_adm_sync(
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> list[str]:
        target_dir, recent_files = NitradoAPI._find_adm_files_sync(
            host, port, username, password, limit=1
        )
        if not recent_files:
            return []

        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            ftp.cwd(target_dir)
            buf = BytesIO()
            ftp.retrbinary(f"RETR {recent_files}", buf.write)
            text = buf.getvalue().decode("utf-8", errors="ignore")

            online_map: dict[str, str] = {}
            for line in text.splitlines():
                disc = DISCONNECT_PATTERN.search(line)
                if disc:
                    nick_key = disc.group("nick").strip().lower()
                    online_map.pop(nick_key, None)
                    continue

                connected_nick: str | None = None
                for pattern in CONNECT_PATTERNS:
                    m = pattern.search(line)
                    if m:
                        connected_nick = m.group("nick").strip()
                        break

                if not connected_nick:
                    pos_m = PLAYER_POS_PATTERN.search(line)
                    if pos_m and "disconnected" not in line.lower():
                        connected_nick = pos_m.group("nick").strip()

                if connected_nick:
                    online_map[connected_nick.lower()] = connected_nick

            return list(online_map.values())
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def get_online_players(self) -> tuple[int, list[str]]:
        """Возвращает (количество_онлайн, список_ников_онлайн)."""
        gs = await self.get_server_status()
        if not gs:
            return 0, []

        query = gs.get("query") or {}
        count = int(query.get("player_current") or 0)
        if count <= 0:
            return 0, []

        creds = await self._get_ftp_credentials()
        if not creds:
            return count, []

        try:
            nicks = await asyncio.to_thread(self._parse_online_from_adm_sync, *creds)
            return count, nicks
        except Exception as e:
            logger.error("Ошибка при получении списка игроков онлайн из логов: %s", e)
            return count, []
    @staticmethod
    def _parse_online_positions_from_adm_sync(
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> dict[str, tuple[float, float] | None]:
        """Возвращает словарь {game_nick: (x, z) или None} для игроков, находящихся сейчас на сервере."""
        target_dir, recent_files = NitradoAPI._find_adm_files_sync(
            host, port, username, password, limit=1
        )
        if not recent_files:
            return {}

        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            ftp.cwd(target_dir)
            buf = BytesIO()
            ftp.retrbinary(f"RETR {recent_files}", buf.write)
            text = buf.getvalue().decode("utf-8", errors="ignore")

            online_pos: dict[str, tuple[str, tuple[float, float] | None]] = {}
            for line in text.splitlines():
                disc = DISCONNECT_PATTERN.search(line)
                if disc:
                    nick_key = disc.group("nick").strip().lower()
                    online_pos.pop(nick_key, None)
                    continue

                pos_m = PLAYER_POS_PATTERN.search(line)
                if pos_m and "disconnected" not in line.lower():
                    nick = pos_m.group("nick").strip()
                    x_str, z_str = pos_m.group("x"), pos_m.group("z")
                    coords = (float(x_str), float(z_str)) if (x_str is not None and z_str is not None) else None
                    prev = online_pos.get(nick.lower())
                    online_pos[nick.lower()] = (nick, coords if coords is not None else (prev if prev else None))
                    continue

                for pattern in CONNECT_PATTERNS:
                    m = pattern.search(line)
                    if m:
                        nick = m.group("nick").strip()
                        prev = online_pos.get(nick.lower())
                        online_pos[nick.lower()] = (nick, prev if prev else None)
                        break

            return {nick: coords for nick, coords in online_pos.values()}
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def get_online_players_with_positions(self) -> tuple[int, dict[str, tuple[float, float] | None]]:
        """Возвращает (количество_онлайн, {nick: (x, z) | None}) для расчёта зарплаты с учётом Опасной зоны."""
        gs = await self.get_server_status()
        if not gs:
            return 0, {}

        query = gs.get("query") or {}
        count = int(query.get("player_current") or 0)
        if count <= 0:
            return 0, {}

        creds = await self._get_ftp_credentials()
        if not creds:
            return count, {}

        try:
            pos_map = await asyncio.to_thread(self._parse_online_positions_from_adm_sync, *creds)
            return count, pos_map
        except Exception as e:
            logger.error("Ошибка при получении координат игроков онлайн из логов: %s", e)
            return count, {}

    @staticmethod
    def _fetch_new_kills_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        last_file: str | None,
        last_hash: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None, bool]:
        target_dir, recent_files = NitradoAPI._find_adm_files_sync(
            host, port, username, password, limit=5
        )
        if not recent_files:
            return [], last_file, last_hash, True

        newest_file = recent_files
        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)

        try:
            ftp.cwd(target_dir)

            if not last_file:
                buf = BytesIO()
                ftp.retrbinary(f"RETR {newest_file}", buf.write)
                text = buf.getvalue().decode("utf-8", errors="ignore")
                _, newest_hash = _parse_adm_file(newest_file, text, stop_at_hash=None)
                return [], newest_file, newest_hash, True

            files_to_read: list[str] = []
            for fname in recent_files:
                files_to_read.append(fname)
                if fname == last_file:
                    break

            files_to_read.reverse()

            all_events: list[dict[str, Any]] = []
            final_file = last_file
            final_hash = last_hash

            for fname in files_to_read:
                buf = BytesIO()
                ftp.retrbinary(f"RETR {fname}", buf.write)
                text = buf.getvalue().decode("utf-8", errors="ignore")

                stop_hash = last_hash if fname == last_file else None
                file_events, f_newest_hash = _parse_adm_file(fname, text, stop_at_hash=stop_hash)

                all_events.extend(file_events)
                final_file = fname
                if f_newest_hash:
                    final_hash = f_newest_hash

            return all_events, final_file, final_hash, True
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def get_new_kills(
        self,
        last_file_name: str | None = None,
        last_line_hash: str | None = None,
        last_file: str | None = None,
        last_hash: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        file_arg = last_file_name if last_file_name is not None else last_file
        hash_arg = last_line_hash if last_line_hash is not None else last_hash

        creds = await self._get_ftp_credentials()
        if not creds:
            return [], file_arg, hash_arg
        try:
            events, final_file, final_hash, _ = await asyncio.to_thread(
                self._fetch_new_kills_sync,
                *creds,
                file_arg,
                hash_arg,
            )
            return events, final_file, final_hash
        except Exception as e:
            logger.error("Ошибка при парсинге убийств из FTP логов Nitrado: %s", e)
            return [], file_arg, hash_arg
    @staticmethod
    def _fetch_autoban_events_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        last_file: str | None,
        last_hash: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None, bool]:
        target_dir, recent_files = NitradoAPI._find_adm_files_sync(
            host, port, username, password, limit=5
        )
        if not recent_files:
            return [], last_file, last_hash, True

        newest_file = recent_files
        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)

        try:
            ftp.cwd(target_dir)

            if not last_file:
                buf = BytesIO()
                ftp.retrbinary(f"RETR {newest_file}", buf.write)
                text = buf.getvalue().decode("utf-8", errors="ignore")
                _, newest_hash = _parse_adm_autoban_events(newest_file, text, stop_at_hash=None)
                return [], newest_file, newest_hash, True

            files_to_read: list[str] = []
            for fname in recent_files:
                files_to_read.append(fname)
                if fname == last_file:
                    break
            files_to_read.reverse()

            all_events: list[dict[str, Any]] = []
            final_file = last_file
            final_hash = last_hash

            for fname in files_to_read:
                buf = BytesIO()
                ftp.retrbinary(f"RETR {fname}", buf.write)
                text = buf.getvalue().decode("utf-8", errors="ignore")

                stop_hash = last_hash if fname == last_file else None
                file_events, f_newest_hash = _parse_adm_autoban_events(fname, text, stop_at_hash=stop_hash)

                all_events.extend(file_events)
                final_file = fname
                if f_newest_hash:
                    final_hash = f_newest_hash

            return all_events, final_file, final_hash, True
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def get_new_autoban_events(
        self,
        last_file_name: str | None = None,
        last_line_hash: str | None = None,
        last_file: str | None = None,
        last_hash: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """Возвращает новые события постройки и PvP-убийств из .ADM логов для проверки гео-автобана."""
        file_arg = last_file_name if last_file_name is not None else last_file
        hash_arg = last_line_hash if last_line_hash is not None else last_hash

        creds = await self._get_ftp_credentials()
        if not creds:
            return [], file_arg, hash_arg
        try:
            events, final_file, final_hash, _ = await asyncio.to_thread(
                self._fetch_autoban_events_sync,
                *creds,
                file_arg,
                hash_arg,
            )
            return events, final_file, final_hash
        except Exception as e:
            logger.error("Ошибка при парсинге событий гео-автобана из FTP логов: %s", e)
            return [], file_arg, hash_arg

    # ==================== УПРАВЛЕНИЕ XML-СПАВНАМИ МАГАЗИНА ====================

    @staticmethod
    def _find_mission_dir_sync(ftp: FTP) -> str | None:
        """Ищет папку активной миссии DayZ (где лежат events.xml, cfgeventspawns.xml и cfggameplay.json)."""
        mission_roots = [
            "/dayzps/mpmissions",
            "/dayzxb/mpmissions",
            "/dayz/mpmissions",
            "/mpmissions",
        ]
        for root in mission_roots:
            try:
                ftp.cwd(root)
                entries = ftp.nlst()
                for entry in entries:
                    if entry in (".", ".."):
                        continue
                    candidate = f"{root}/{entry}"
                    try:
                        ftp.cwd(candidate)
                        files = [f.lower() for f in ftp.nlst()]
                        if "events.xml" in files and "cfgeventspawns.xml" in files:
                            return candidate
                    except error_perm:
                        continue
            except error_perm:
                continue
        return None
    @staticmethod
    def _ensure_shop_event_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        game_classname: str,
    ) -> bool:
        safe_cls = re.sub(r"[^A-Za-z0-9_]", "", game_classname.strip())
        if not safe_cls:
            return False
        event_name = f"ItemShop_{safe_cls}"

        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                logger.error("Не найдена папка миссии (events.xml) на FTP Nitrado")
                return False

            ftp.cwd(mission_dir)
            buf = BytesIO()
            ftp.retrbinary("RETR events.xml", buf.write)
            xml_text = buf.getvalue().decode("utf-8", errors="ignore")

            if f'name="{event_name}"' in xml_text:
                return True

            event_block = (
                f'    <event name="{event_name}">\n'
                f'        <nominal>0</nominal>\n'
                f'        <min>0</min>\n'
                f'        <max>0</max>\n'
                f'        <lifetime>3888000</lifetime>\n'
                f'        <restock>0</restock>\n'
                f'        <saferadius>0</saferadius>\n'
                f'        <distanceradius>0</distanceradius>\n'
                f'        <cleanupradius>200</cleanupradius>\n'
                f'        <flags deletable="0" init_random="0" remove_damaged="0"/>\n'
                f'        <position>fixed</position>\n'
                f'        <limit>child</limit>\n'
                f'        <active>1</active>\n'
                f'        <children>\n'
                f'            <child lootmax="0" lootmin="0" max="1" min="1" type="{safe_cls}"/>\n'
                f'        </children>\n'
                f'    </event>\n'
            )

            if "</events>" not in xml_text:
                logger.error("В файле events.xml не найден закрывающий тег </events>")
                return False

            updated_xml = xml_text.replace("</events>", f"{event_block}</events>", 1)
            ftp.storbinary("STOR events.xml", BytesIO(updated_xml.encode("utf-8")))
            logger.info("Добавлен XML-эвент %s в events.xml", event_name)
            return True
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def ensure_shop_event_exists(self, game_classname: str) -> bool:
        """Гарантирует наличие <event name="ItemShop_<classname>"> в events.xml."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            return await asyncio.to_thread(self._ensure_shop_event_sync, *creds, game_classname)
        except Exception as e:
            logger.error("Ошибка при обновлении events.xml для %s: %s", game_classname, e)
            return False

    @staticmethod
    def _append_event_spawns_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        items: list[dict[str, Any]],
        x: float,
        z: float,
        radius: float = 3.0,
    ) -> bool:
        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                logger.error("Не найдена папка миссии (cfgeventspawns.xml) на FTP Nitrado")
                return False

            ftp.cwd(mission_dir)
            buf = BytesIO()
            ftp.retrbinary("RETR cfgeventspawns.xml", buf.write)
            xml_text = buf.getvalue().decode("utf-8", errors="ignore")

            if "</eventposdef>" not in xml_text:
                logger.error("В файле cfgeventspawns.xml не найден закрывающий тег </eventposdef>")
                return False

            blocks: list[str] = []
            idx = 0
            for item in items:
                safe_cls = re.sub(r"[^A-Za-z0-9_]", "", str(item["game_classname"]).strip())
                qty = max(1, int(item.get("qty", 1)))
                event_name = f"ItemShop_{safe_cls}"
                for _ in range(qty):
                    angle = (2.0 * math.pi * idx) / max(1, idx + 1)
                    offset = min(radius, 0.5 * idx) if idx > 0 else 0.0
                    px = round(x + math.cos(angle) * offset, 2)
                    pz = round(z + math.sin(angle) * offset, 2)
                    blocks.append(
                        f'    <event name="{event_name}">\n'
                        f'        <pos x="{px}" z="{pz}" a="0" />\n'
                        f'    </event>\n'
                    )
                    idx += 1

            payload = "".join(blocks)
            updated_xml = xml_text.replace("</eventposdef>", f"{payload}</eventposdef>", 1)
            ftp.storbinary("STOR cfgeventspawns.xml", BytesIO(updated_xml.encode("utf-8")))
            logger.info("Записано %d позиций спавна магазина в cfgeventspawns.xml (X=%s, Z=%s)", idx, x, z)
            return True
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def append_event_spawn_orders(
        self,
        items: list[dict[str, Any]],
        x: float,
        z: float,
        radius: float = 3.0,
    ) -> bool:
        """Добавляет координаты заказанных товаров в cfgeventspawns.xml."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            return await asyncio.to_thread(
                self._append_event_spawns_sync,
                *creds,
                items,
                x,
                z,
                radius,
            )
        except Exception as e:
            logger.error("Ошибка при записи заказа в cfgeventspawns.xml: %s", e)
            return False

    @staticmethod
    def _clear_event_spawns_sync(
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> bool:
        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                return False

            ftp.cwd(mission_dir)
            buf = BytesIO()
            ftp.retrbinary("RETR cfgeventspawns.xml", buf.write)
            xml_text = buf.getvalue().decode("utf-8", errors="ignore")

            cleaned_xml, removed_count = re.subn(
                r'\s*<event\s+name="ItemShop_[^"]+">.*?</event>\s*',
                "\n",
                xml_text,
                flags=re.DOTALL,
            )
            if removed_count > 0:
                ftp.storbinary("STOR cfgeventspawns.xml", BytesIO(cleaned_xml.encode("utf-8")))
                logger.info("Очищено %d разовых спавнов магазина из cfgeventspawns.xml", removed_count)
            return True
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def clear_event_spawn_orders(self) -> bool:
        """Удаляет все одноразовые события ItemShop_* из cfgeventspawns.xml после рестарта."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            return await asyncio.to_thread(self._clear_event_spawns_sync, *creds)
        except Exception as e:
            logger.error("Ошибка при очистке cfgeventspawns.xml: %s", e)
            return False
    # ==================== УПРАВЛЕНИЕ РЕЙД-ТАЙМОМ (cfggameplay.json) ====================

    @staticmethod
    def _set_raid_mode_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        raid_enabled: bool,
    ) -> bool:
        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=15)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                logger.error("Не найдена папка миссии для изменения cfggameplay.json")
                return False

            ftp.cwd(mission_dir)
            buf = BytesIO()
            ftp.retrbinary("RETR cfggameplay.json", buf.write)
            raw_json = buf.getvalue().decode("utf-8", errors="ignore")

            flag_val = "0" if raid_enabled else "1"
            updated_json, count1 = re.subn(
                r'("disableBaseDamage"\s*:\s*)(?:true|false|0|1)',
                rf'\g<1>{flag_val}',
                raw_json,
                flags=re.IGNORECASE,
            )
            updated_json, count2 = re.subn(
                r'("disableContainerDamage"\s*:\s*)(?:true|false|0|1)',
                rf'\g<1>{flag_val}',
                updated_json,
                flags=re.IGNORECASE,
            )

            if count1 == 0 and count2 == 0:
                logger.warning("В cfggameplay.json не найдены ключи disableBaseDamage / disableContainerDamage")
                return False

            ftp.storbinary("STOR cfggameplay.json", BytesIO(updated_json.encode("utf-8")))
            logger.info(
                "Обновлён cfggameplay.json: raid_enabled=%s (disableBaseDamage=%s)",
                raid_enabled,
                flag_val,
            )
            return True
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def set_raid_mode(self, raid_enabled: bool) -> bool:
        """Включает или отключает урон по постройкам и палаткам в cfggameplay.json."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            return await asyncio.to_thread(self._set_raid_mode_sync, *creds, raid_enabled)
        except Exception as e:
            logger.error("Ошибка при обновлении cfggameplay.json: %s", e)
            return False

    # ==================== УПРАВЛЕНИЕ ВАЙПАМИ (ПОЛНЫЙ И АВТО) ====================

    @staticmethod
    def _wipe_storage_via_ftp_sync(
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> tuple[bool, str]:
        """Удаляет файлы базы данных и хранилища миссии DayZ (storage_1) для полного вайпа."""
        ftp = FTP()
        ftp.encoding = "utf-8"
        ftp.connect(host, port, timeout=20)
        ftp.login(username, password)
        ftp.set_pasv(True)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                return False, "Не найдена папка миссии на FTP Nitrado."

            storage_dir = f"{mission_dir}/storage_1"
            try:
                ftp.cwd(storage_dir)
            except error_perm:
                return False, f"Папка хранилища {storage_dir} не найдена."

            def _clean_dir_recursive(path: str) -> int:
                deleted = 0
                ftp.cwd(path)
                entries = [e for e in ftp.nlst() if e not in (".", "..")]
                for entry in entries:
                    full_path = f"{path}/{entry}"
                    try:
                        ftp.delete(full_path)
                        deleted += 1
                    except error_perm:
                        deleted += _clean_dir_recursive(full_path)
                        try:
                            ftp.cwd(path)
                            ftp.rmd(full_path)
                        except error_perm:
                            pass
                return deleted

            removed_files = _clean_dir_recursive(storage_dir)
            logger.info("Полный вайп: очищено %d файлов в %s", removed_files, storage_dir)
            return True, f"Очищено файлов сохранения: {removed_files}."
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

        @staticmethod
    def _find_mission_dir_sync(ftp: FTP) -> str | None:
        """Ищет папку активной миссии DayZ (где лежат events.xml, cfgeventspawns.xml и cfggameplay.json)."""
        mission_roots = [
            "/dayzps_missions",
            "dayzps_missions",
            "/dayzps/mpmissions",
            "/dayzxb_missions",
            "/dayzxb/mpmissions",
            "/dayz/mpmissions",
            "/mpmissions",
        ]
        for root in mission_roots:
            try:
                ftp.cwd("/")
                ftp.cwd(root)
                entries = ftp.nlst()
                for entry in entries:
                    clean_entry = entry.rstrip("/").split("/")[-1]
                    if clean_entry in (".", ".."):
                        continue
                    candidate = f"{root.rstrip('/')}/{clean_entry}"
                    try:
                        ftp.cwd("/")
                        ftp.cwd(candidate)
                        files = [f.rstrip("/").split("/")[-1].lower() for f in ftp.nlst()]
                        if "events.xml" in files and "cfgeventspawns.xml" in files:
                            return candidate
                    except error_perm:
                        continue
            except error_perm:
                continue

        logger.error("Не найдена папка миссии на FTP Nitrado")
        return None


            ftp.cwd(mission_dir)
            if not zero_mode and backup_xml:
                ftp.storbinary("STOR events.xml", BytesIO(backup_xml.encode("utf-8")))
                return True, None

            buf = BytesIO()
            ftp.retrbinary("RETR events.xml", buf.write)
            original_xml = buf.getvalue().decode("utf-8", errors="ignore")

            root = ET.fromstring(original_xml)
            changed = 0
            for ev in root.findall("event"):
                ev_name = (ev.get("name") or "").strip()
                if ev_name.startswith("Vehicle"):
                    for tag_name in ("nominal", "min", "max"):
                        el = ev.find(tag_name)
                        if el is not None:
                            el.text = "0"
                    changed += 1

            if changed == 0:
                return False, None

            modified_xml = ET.tostring(root, encoding="unicode")
            if not modified_xml.lstrip().startswith("<?xml"):
                modified_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + modified_xml

            ftp.storbinary("STOR events.xml", BytesIO(modified_xml.encode("utf-8")))
            return True, original_xml
        finally:
            try:
                ftp.quit()
            except Exception:
                ftp.close()

    async def _wipe_cars_via_events_xml(self) -> tuple[bool, str]:
        """Выполняет вайп автомобилей: обнуляет Vehicle-эвенты, перезапускает сервер и возвращает штатный events.xml."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False, "Не удалось получить FTP-реквизиты Nitrado."

        ok_zero, backup_xml = await asyncio.to_thread(
            self._set_vehicles_nominal_sync, *creds, True, None
        )
        if not ok_zero or not backup_xml:
            return False, "Не удалось обновить Vehicle-события в events.xml."

        if not await self.restart_server():
            await asyncio.to_thread(self._set_vehicles_nominal_sync, *creds, False, backup_xml)
            return False, "Не удалось отправить команду рестарта в Nitrado."

        # Ждём прогрузки сервера, чтобы DayZ удалил старые автомобили с карты
        await asyncio.sleep(150)

        creds_after = await self._get_ftp_credentials() or creds
        ok_restore, _ = await asyncio.to_thread(
            self._set_vehicles_nominal_sync, *creds_after, False, backup_xml
        )
        if not ok_restore:
            return False, "Старые авто удалены, но не удалось восстановить исходный events.xml!"

        await self.restart_server()
        return True, "Транспорт очищен, штатный спавн восстановлен, сервер перезапускается."

    async def wipe_server(self, wipe_type: str = "full") -> tuple[bool, str]:
        """Выполняет полный вайп ('full') или вайп автомобилей ('cars')."""
        if wipe_type == "cars":
            try:
                return await self._wipe_cars_via_events_xml()
            except Exception as e:
                logger.error("Ошибка при вайпе автомобилей: %s", e, exc_info=True)
                return False, f"Исключение при вайпе авто: {e}"

        # Полный вайп сервера
        if not await self.stop_server():
            return False, "Не удалось остановить сервер перед полным вайпом."

        await asyncio.sleep(25)
        creds = await self._get_ftp_credentials()
        if not creds:
            await self.start_server()
            return False, "Не удалось получить FTP-реквизиты после остановки сервера."

        try:
            ok_clean, msg = await asyncio.to_thread(self._wipe_storage_via_ftp_sync, *creds)
        except Exception as e:
            logger.error("Ошибка очистки storage_1 при полном вайпе: %s", e, exc_info=True)
            ok_clean, msg = False, f"Ошибка FTP при очистке хранилища: {e}"

        await self.start_server()
        return ok_clean, msg
