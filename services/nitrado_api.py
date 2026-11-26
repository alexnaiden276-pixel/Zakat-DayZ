import asyncio
import hashlib
import logging
import math
import os
import re
import tempfile
from ftplib import FTP, error_perm
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

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

# Паттерн периодического дампа координат игроков в .ADM (формат DayZ: <X, Z_высота, Y_север_юг>)
PLAYER_POS_PATTERN = re.compile(
    r'Player\s+"(?P<nick>[^"]+)"\s*\(id=(?P<id>[A-Za-z0-9_-]+)'
    r'(?:\s*,\s*pos=<(?P<x>-?\d+(?:\.\d+)?)\s*,\s*(?P<z>-?\d+(?:\.\d+)?)\s*,\s*(?P<y>-?\d+(?:\.\d+)?)>)?\)',
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
    if diff > 43200:
        diff = 86400 - diff
    return diff <= max_diff


def _calc_distance_2d(
    kx: str | None,
    ky: str | None,
    vx: str | None,
    vy: str | None,
) -> float:
    """Считает 2D-дистанцию на карте DayZ (оси X и Y; Z — это высота)."""
    if None in (kx, ky, vx, vy):
        return 0.0
    try:
        dx = float(kx) - float(vx)
        dy = float(ky) - float(vy)
        return round(math.hypot(dx, dy), 1)
    except (TypeError, ValueError):
        return 0.0


def _extract_new_lines(lines: list[str], stop_at_hash: str | None) -> list[str]:
    """Возвращает только новые строки лога после строки с хэшем stop_at_hash."""
    if not stop_at_hash:
        return lines
    for idx in range(len(lines) - 1, -1, -1):
        if _line_hash(lines[idx]) == stop_at_hash:
            return lines[idx + 1:]
    return lines
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
    new_lines = _extract_new_lines(lines, stop_at_hash)

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
                    data.get("kx"), data.get("ky"), data.get("vx"), data.get("vy")
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
                "killer_id": data.get("killer_id"),
                "victim": data["victim"],
                "victim_id": data.get("victim_id"),
                "weapon": weapon_clean,
                "distance": dist_val,
                "is_headshot": is_headshot,
                "kx": float(data["kx"]) if data.get("kx") is not None else None,
                "ky": float(data["ky"]) if data.get("ky") is not None else None,
                "kz": float(data["kz"]) if data.get("kz") is not None else None,
                "vx": float(data["vx"]) if data.get("vx") is not None else None,
                "vy": float(data["vy"]) if data.get("vy") is not None else None,
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
    """Парсит события постройки объектов и PvP-убийств с 2D-координатами (X, Y) для гео-автобана."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return [], None

    newest_hash = _line_hash(lines[-1])
    new_lines = _extract_new_lines(lines, stop_at_hash)

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
                "y": float(d["y"]),
                "z": float(d["z"]),
                "item": d["item"].strip(),
            })
            continue

        kill_m = KILL_PATTERN.match(line)
        if kill_m:
            d = kill_m.groupdict()
            if None not in (d.get("kx"), d.get("ky"), d.get("vx"), d.get("vy")):
                events.append({
                    "type": "kill",
                    "file": file_name,
                    "time": d["time"],
                    "killer": d["killer"],
                    "killer_id": d.get("killer_id"),
                    "victim": d["victim"],
                    "kx": float(d["kx"]),
                    "ky": float(d["ky"]),
                    "kz": float(d["kz"]),
                    "vx": float(d["vx"]),
                    "vy": float(d["vy"]),
                    "vz": float(d["vz"]),
                })

    return events, newest_hash
class NitradoAPI:
    BASE_URL = "https://api.nitrado.net"
    SHOP_SPAWNS_START = "<!-- BOT_SHOP_SPAWNS_START -->"
    SHOP_SPAWNS_END = "<!-- BOT_SHOP_SPAWNS_END -->"
    SHOP_EVENTS_START = "<!-- BOT_SHOP_EVENTS_START -->"
    SHOP_EVENTS_END = "<!-- BOT_SHOP_EVENTS_END -->"

    def __init__(
        self,
        token: str,
        server_id: str | int | None = None,
        service_id: str | int | None = None,
        ftp_host: str | None = None,
        ftp_port: int | str | None = None,
        ftp_user: str | None = None,
        ftp_pass: str | None = None,
        backup_dir: str | Path = "data",
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
        self.backup_dir = Path(backup_dir)

        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None
        self._bans_lock = asyncio.Lock()
        self._xml_lock = asyncio.Lock()
        self._ftp_semaphore = asyncio.Semaphore(2)

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
            logger.error("Таймаут при запросе статуса к Nitrado API")
            return None
        except Exception as e:
            logger.error("Ошибка при запросе статуса к Nitrado API: %r", e)
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
        except asyncio.TimeoutError:
            logger.error("Таймаут при запросе рестарта Nitrado")
            return False
        except Exception as e:
            logger.error("Исключение при рестарте Nitrado: %r", e)
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
        except asyncio.TimeoutError:
            logger.error("Таймаут при запросе остановки Nitrado")
            return False
        except Exception as e:
            logger.error("Исключение при остановке Nitrado: %r", e)
            return False

    async def start_server(self) -> bool:
        """Запускает остановленный игровой сервер через Nitrado API с проверкой выхода из stopped."""
        ok = await self.restart_server()
        if not ok:
            return False
        await asyncio.sleep(5)
        gs = await self.get_server_status()
        if gs and str(gs.get("status", "")).lower() == "stopped":
            return await self.restart_server()
        return True
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
        except asyncio.TimeoutError:
            logger.error("Таймаут при обновлении настройки Nitrado %s.%s", category, key)
            return False
        except Exception as e:
            logger.error("Исключение при обновлении настройки Nitrado %s.%s: %r", category, key, e)
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
        async with self._bans_lock:
            current = await self.get_ban_list()
            if any(item.lower() == clean_id.lower() for item in current):
                return True
            current.append(clean_id)
            ok = await self.update_setting("general", "bans", "\n".join(current))
            if ok:
                await asyncio.sleep(0.5)
            return ok

    async def unban_player(self, identifier: str) -> bool:
        """Удаляет ник/ID игрока из бан-листа Nitrado (general.bans)."""
        clean_id = identifier.strip()
        if not clean_id:
            return False
        async with self._bans_lock:
            current = await self.get_ban_list()
            updated = [item for item in current if item.lower() != clean_id.lower()]
            if len(updated) == len(current):
                return True
            ok = await self.update_setting("general", "bans", "\n".join(updated))
            if ok:
                await asyncio.sleep(0.5)
            return ok
    async def _get_ftp_credentials(self) -> tuple[str, int, str, str] | None:
        if all([self.ftp_host, self.ftp_user, self.ftp_pass]):
            return self.ftp_host, self.ftp_port, self.ftp_user, self.ftp_pass  # type: ignore[return-value]

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
    def _open_ftp(host: str, port: int, username: str, password: str, timeout: int = 15) -> FTP:
        ftp = FTP()
        ftp.encoding = "utf-8"
        try:
            ftp.connect(host, port, timeout=timeout)
            ftp.login(username, password)
            ftp.set_pasv(True)
            return ftp
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _close_ftp(ftp: FTP | None) -> None:
        if ftp is None:
            return
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass

    @staticmethod
    def _atomic_ftp_write(ftp: FTP, remote_path: str, data: bytes) -> None:
        """Безопасно перезаписывает файл на FTP через временный файл (защита от обрыва связи)."""
        tmp_path = f"{remote_path}.tmp"
        ftp.storbinary(f"STOR {tmp_path}", BytesIO(data))
        try:
            ftp.delete(remote_path)
        except Exception:
            pass
        ftp.rename(tmp_path, remote_path)
    @staticmethod
    def _find_adm_files_on_ftp(ftp: FTP, limit: int = 3) -> tuple[str, list[str]]:
        candidate_dirs = [
            "/dayzps/config",
            "/dayzxb/config",
            "/dayz/config",
            "/config",
            "dayzps/config",
            "dayzxb/config",
            "dayz/config",
            "config",
            "/",
        ]
        target_dir = ""
        adm_files: list[str] = []

        for folder in candidate_dirs:
            try:
                ftp.cwd("/")
                ftp.cwd(folder)
                files = [f.rstrip("/").split("/")[-1] for f in ftp.nlst()]
                found = [f for f in files if f.lower().endswith(".adm")]
                if found:
                    target_dir = ftp.pwd()
                    adm_files = found
                    break
            except error_perm:
                continue

        if not adm_files:
            return "", []

        adm_files.sort(reverse=True)
        return target_dir, adm_files[:limit]

    async def get_latest_adm_filename(self) -> str | None:
        """Возвращает имя самого свежего .ADM лог-файла на сервере."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return None

        def _worker() -> str | None:
            ftp = self._open_ftp(*creds)
            try:
                _, files = self._find_adm_files_on_ftp(ftp, limit=1)
                return files if files else None
            finally:
                self._close_ftp(ftp)

        try:
            async with self._ftp_semaphore:
                return await asyncio.to_thread(_worker)
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
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            target_dir, recent_files = NitradoAPI._find_adm_files_on_ftp(ftp, limit=3)
            if not recent_files:
                return None

            ftp.cwd("/")
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
            return None
        finally:
            NitradoAPI._close_ftp(ftp)

    async def find_player_by_short_id(self, short_id_suffix: str) -> tuple[str, str] | None:
        creds = await self._get_ftp_credentials()
        if not creds:
            return None
        try:
            async with self._ftp_semaphore:
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
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            target_dir, recent_files = NitradoAPI._find_adm_files_on_ftp(ftp, limit=4)
            if not recent_files:
                return None

            ftp.cwd("/")
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
            return None
        finally:
            NitradoAPI._close_ftp(ftp)

    async def find_player_id_in_logs(self, nick: str) -> str | None:
        """Ищет уникальный ID игрока по его нику в последних .ADM логах."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return None
        try:
            async with self._ftp_semaphore:
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
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            target_dir, recent_files = NitradoAPI._find_adm_files_on_ftp(ftp, limit=1)
            if not recent_files:
                return []

            ftp.cwd("/")
            ftp.cwd(target_dir)
            buf = BytesIO()
            ftp.retrbinary(f"RETR {recent_files}", buf.write)
            text = buf.getvalue().decode("utf-8", errors="ignore")

            online_map: dict[str, str] = {}
            for line in text.splitlines():
                low = line.lower()
                disc = DISCONNECT_PATTERN.search(line)
                if disc:
                    nick_key = disc.group("nick").strip().lower()
                    online_map.pop(nick_key, None)
                    continue

                if "(dead)" in low:
                    continue

                connected_nick: str | None = None
                for pattern in CONNECT_PATTERNS:
                    m = pattern.search(line)
                    if m:
                        connected_nick = m.group("nick").strip()
                        break

                if not connected_nick and "disconnected" not in low:
                    pos_m = PLAYER_POS_PATTERN.search(line)
                    if pos_m:
                        connected_nick = pos_m.group("nick").strip()

                if connected_nick:
                    online_map[connected_nick.lower()] = connected_nick

            return list(online_map.values())
        finally:
            NitradoAPI._close_ftp(ftp)

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
            async with self._ftp_semaphore:
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
        """Возвращает словарь {game_nick: (x, y) или None} для игроков на сервере (2D-координаты карты)."""
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            target_dir, recent_files = NitradoAPI._find_adm_files_on_ftp(ftp, limit=1)
            if not recent_files:
                return {}

            ftp.cwd("/")
            ftp.cwd(target_dir)
            buf = BytesIO()
            ftp.retrbinary(f"RETR {recent_files}", buf.write)
            text = buf.getvalue().decode("utf-8", errors="ignore")

            online_pos: dict[str, tuple[str, tuple[float, float] | None]] = {}
            for line in text.splitlines():
                low = line.lower()
                disc = DISCONNECT_PATTERN.search(line)
                if disc:
                    nick_key = disc.group("nick").strip().lower()
                    online_pos.pop(nick_key, None)
                    continue

                if "(dead)" in low:
                    continue

                pos_m = PLAYER_POS_PATTERN.search(line)
                if pos_m and "disconnected" not in low:
                    nick = pos_m.group("nick").strip()
                    x_str, y_str = pos_m.group("x"), pos_m.group("y")
                    coords = (float(x_str), float(y_str)) if (x_str is not None and y_str is not None) else None
                    prev = online_pos.get(nick.lower())
                    prev_coords = prev if prev else None
                    online_pos[nick.lower()] = (nick, coords if coords is not None else prev_coords)
                    continue

                for pattern in CONNECT_PATTERNS:
                    m = pattern.search(line)
                    if m:
                        nick = m.group("nick").strip()
                        prev = online_pos.get(nick.lower())
                        prev_coords = prev if prev else None
                        online_pos[nick.lower()] = (nick, prev_coords)
                        break

            return {nick: coords for nick, coords in online_pos.values()}
        finally:
            NitradoAPI._close_ftp(ftp)

    async def get_online_players_with_positions(self) -> tuple[int, dict[str, tuple[float, float] | None]]:
        """Возвращает (количество_онлайн, {nick: (x, y) | None}) для расчёта зарплаты с учётом Опасной зоны."""
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
            async with self._ftp_semaphore:
                pos_map = await asyncio.to_thread(self._parse_online_positions_from_adm_sync, *creds)
            return count, pos_map
        except Exception as e:
            logger.error("Ошибка при получении координат игроков онлайн из логов: %s", e)
            return count, {}
    @staticmethod
    def _fetch_adm_events_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        last_file: str | None,
        last_hash: str | None,
        parser_fn: Callable[[str, str, str | None], tuple[list[dict[str, Any]], str | None]],
    ) -> tuple[list[dict[str, Any]], str | None, str | None, bool]:
        """Универсальный инкрементальный парсер новых событий из последних .ADM файлов."""
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            target_dir, recent_files = NitradoAPI._find_adm_files_on_ftp(ftp, limit=5)
            if not recent_files:
                return [], last_file, last_hash, True

            newest_file = recent_files
            ftp.cwd("/")
            ftp.cwd(target_dir)

            if not last_file:
                buf = BytesIO()
                ftp.retrbinary(f"RETR {newest_file}", buf.write)
                text = buf.getvalue().decode("utf-8", errors="ignore")
                _, newest_hash = parser_fn(newest_file, text, None)
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
                file_events, f_newest_hash = parser_fn(fname, text, stop_hash)

                all_events.extend(file_events)
                final_file = fname
                if f_newest_hash:
                    final_hash = f_newest_hash

            return all_events, final_file, final_hash, True
        finally:
            NitradoAPI._close_ftp(ftp)

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
            async with self._ftp_semaphore:
                events, final_file, final_hash, _ = await asyncio.to_thread(
                    self._fetch_adm_events_sync,
                    *creds,
                    file_arg,
                    hash_arg,
                    _parse_adm_file,
                )
            return events, final_file, final_hash
        except Exception as e:
            logger.error("Ошибка при парсинге убийств из FTP логов Nitrado: %s", e)
            return [], file_arg, hash_arg

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
            async with self._ftp_semaphore:
                events, final_file, final_hash, _ = await asyncio.to_thread(
                    self._fetch_adm_events_sync,
                    *creds,
                    file_arg,
                    hash_arg,
                    _parse_adm_autoban_events,
                )
            return events, final_file, final_hash
        except Exception as e:
            logger.error("Ошибка при парсинге событий гео-автобана из FTP логов: %s", e)
            return [], file_arg, hash_arg
    # ==================== УПРАВЛЕНИЕ XML-СПАВНАМИ МАГАЗИНА ====================

    @staticmethod
    def _find_mission_dir_sync(ftp: FTP) -> str | None:
        """Возвращает путь к папке активной миссии DayZ (например, /dayzps_missions/dayzOffline.chernarusplus)."""
        mission_roots = [
            "/dayzps_missions",
            "dayzps_missions",
            "/dayzps/mpmissions",
            "/dayzxb_missions",
            "dayzxb_missions",
            "/dayzxb/mpmissions",
            "/dayz/mpmissions",
            "/mpmissions",
        ]
        for root in mission_roots:
            try:
                ftp.cwd("/")
                ftp.cwd(root)
                root_pwd = ftp.pwd().rstrip("/")
                entries = ftp.nlst()
                for entry in entries:
                    clean_entry = entry.rstrip("/").split("/")[-1]
                    if clean_entry in (".", "..", ""):
                        continue
                    candidate = f"{root_pwd}/{clean_entry}"
                    try:
                        ftp.cwd("/")
                        ftp.cwd(candidate)
                        files = {f.rstrip("/").split("/")[-1].lower() for f in ftp.nlst()}
                        if "cfgeventspawns.xml" in files or "cfggameplay.json" in files:
                            return candidate
                    except error_perm:
                        continue
            except error_perm:
                continue

        logger.error("Не найдена папка миссии на FTP Nitrado")
        return None

    @staticmethod
    def _resolve_events_xml_path(ftp: FTP, mission_dir: str) -> str:
        """Определяет точный путь к штатному events.xml."""
        for candidate in (f"{mission_dir}/db/events.xml", f"{mission_dir}/events.xml"):
            try:
                ftp.size(candidate)
                return candidate
            except Exception:
                continue
        return f"{mission_dir}/db/events.xml"

    @classmethod
    def _extract_marked_zone(
        cls,
        xml_text: str,
        start_marker: str,
        end_marker: str,
        closing_tags: tuple[str, ...],
    ) -> tuple[str, str, str] | None:
        """
        Разделяет XML-файл на 3 части: (до_маркера, содержимое_зоны, после_маркера).
        Если маркеров ещё нет, автоматически создаёт пустую секцию перед закрывающим тегом файла.
        """
        start_idx = xml_text.find(start_marker)
        end_idx = xml_text.find(end_marker)

        if start_idx != -1 and end_idx != -1 and start_idx < end_idx:
            before = xml_text[: start_idx + len(start_marker)]
            zone = xml_text[start_idx + len(start_marker) : end_idx]
            after = xml_text[end_idx:]
            return before, zone, after

        for closing_tag in closing_tags:
            close_idx = xml_text.rfind(closing_tag)
            if close_idx != -1:
                before = f"{xml_text[:close_idx]}\t{start_marker}\n"
                zone = ""
                after = f"\t{end_marker}\n{xml_text[close_idx:]}"
                return before, zone, after

        return None

    @staticmethod
    def _build_shop_event_block(event_name: str, safe_cls: str, count: int = 0) -> str:
        safe_count = max(0, count)
        active_flag = 1 if safe_count > 0 else 0
        return (
            f'\t<event name="{event_name}">\n'
            f'\t\t<nominal>{safe_count}</nominal>\n'
            f'\t\t<min>{safe_count}</min>\n'
            f'\t\t<max>{safe_count}</max>\n'
            f'\t\t<lifetime>3600</lifetime>\n'
            f'\t\t<restock>0</restock>\n'
            f'\t\t<saferadius>0</saferadius>\n'
            f'\t\t<distanceradius>0</distanceradius>\n'
            f'\t\t<cleanupradius>0</cleanupradius>\n'
            f'\t\t<flags deletable="0" init_random="0" remove_damaged="0"/>\n'
            f'\t\t<position>fixed</position>\n'
            f'\t\t<limit>child</limit>\n'
            f'\t\t<active>{active_flag}</active>\n'
            f'\t\t<children>\n'
            f'\t\t\t<child lootmax="0" lootmin="0" max="{safe_count}" min="{safe_count}" type="{safe_cls}"/>\n'
            f'\t\t</children>\n'
            f'\t</event>\n'
        )
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

        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                logger.error("Не найдена папка миссии на FTP Nitrado")
                return False

            events_path = NitradoAPI._resolve_events_xml_path(ftp, mission_dir)
            buf = BytesIO()
            ftp.retrbinary(f"RETR {events_path}", buf.write)
            xml_text = buf.getvalue().decode("utf-8", errors="ignore")

            parts = NitradoAPI._extract_marked_zone(
                xml_text,
                NitradoAPI.SHOP_EVENTS_START,
                NitradoAPI.SHOP_EVENTS_END,
                ("</events>",),
            )
            if not parts:
                logger.error("В %s не найден закрывающий тег </events>", events_path)
                return False

            before_ev, events_zone, after_ev = parts
            if f'name="{event_name}"' in events_zone:
                return True

            if events_zone and not events_zone.endswith("\n"):
                events_zone += "\n"
            elif not events_zone:
                events_zone = "\n"

            events_zone += NitradoAPI._build_shop_event_block(event_name, safe_cls, count=0)
            updated_xml = f"{before_ev}{events_zone}{after_ev}"
            NitradoAPI._atomic_ftp_write(ftp, events_path, updated_xml.encode("utf-8"))
            logger.info("Добавлен XML-эвент %s в секцию магазина %s", event_name, events_path)
            return True
        finally:
            NitradoAPI._close_ftp(ftp)

    async def ensure_shop_event_exists(self, game_classname: str) -> bool:
        """Гарантирует наличие <event name="ItemShop_<classname>"> в секции магазина db/events.xml."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            async with self._xml_lock, self._ftp_semaphore:
                return await asyncio.to_thread(self._ensure_shop_event_sync, *creds, game_classname)
        except Exception as e:
            logger.error("Ошибка при обновлении db/events.xml для %s: %s", game_classname, e)
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
        y: float | None = None,
    ) -> bool:
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                logger.error("Не найдена папка миссии (cfgeventspawns.xml) на FTP Nitrado")
                return False

            # 1. Записываем 2D-координаты (x, z) без высоты y, чтобы DayZ ставил предмет ровно на землю
            ftp.cwd("/")
            ftp.cwd(mission_dir)
            buf = BytesIO()
            ftp.retrbinary("RETR cfgeventspawns.xml", buf.write)
            spawns_xml = buf.getvalue().decode("utf-8", errors="ignore")

            spawn_parts = NitradoAPI._extract_marked_zone(
                spawns_xml,
                NitradoAPI.SHOP_SPAWNS_START,
                NitradoAPI.SHOP_SPAWNS_END,
                ("</events>", "</eventposdef>"),
            )
            if not spawn_parts:
                logger.error("В cfgeventspawns.xml не найден корневой закрывающий тег")
                return False

            before_sp, spawns_zone, after_sp = spawn_parts

            grouped_pos: dict[str, list[str]] = {}
            event_class_map: dict[str, str] = {}
            idx = 0

            for item in items:
                safe_cls = re.sub(r"[^A-Za-z0-9_]", "", str(item["game_classname"]).strip())
                if not safe_cls:
                    continue
                qty = max(1, int(item.get("qty", 1)))
                event_name = f"ItemShop_{safe_cls}"
                event_class_map[event_name] = safe_cls
                pos_list = grouped_pos.setdefault(event_name, [])

                for _ in range(qty):
                    angle = idx * 2.399963229728653
                    offset = min(radius, 0.45 * math.sqrt(idx)) if idx > 0 else 0.0
                    px = round(x + math.cos(angle) * offset, 3)
                    pz = round(z + math.sin(angle) * offset, 3)
                    pos_list.append(f'\t\t<pos x="{px}" z="{pz}" a="0"/>\n')
                    idx += 1

            updated_spawns_zone = spawns_zone
            new_spawn_blocks: list[str] = []

            for event_name, pos_lines in grouped_pos.items():
                pos_payload = "".join(pos_lines)
                pattern = re.compile(
                    rf'(<event\s+name="{re.escape(event_name)}"\s*>)(.*?)(</event>)',
                    flags=re.DOTALL,
                )
                if pattern.search(updated_spawns_zone):
                    updated_spawns_zone = pattern.sub(
                        lambda m: f"{m.group(1)}{m.group(2)}{pos_payload}\t{m.group(3)}",
                        updated_spawns_zone,
                        count=1,
                    )
                else:
                    new_spawn_blocks.append(
                        f'\t<event name="{event_name}">\n'
                        f'{pos_payload}'
                        f'\t</event>\n'
                    )

            if new_spawn_blocks:
                if updated_spawns_zone and not updated_spawns_zone.endswith("\n"):
                    updated_spawns_zone += "\n"
                elif not updated_spawns_zone:
                    updated_spawns_zone = "\n"
                updated_spawns_zone += "".join(new_spawn_blocks)

            NitradoAPI._atomic_ftp_write(
                ftp,
                "cfgeventspawns.xml",
                f"{before_sp}{updated_spawns_zone}{after_sp}".encode("utf-8"),
            )
            # 2. Активируем события в изолированной секции db/events.xml с точным количеством total_pos
            events_path = NitradoAPI._resolve_events_xml_path(ftp, mission_dir)
            ev_buf = BytesIO()
            ftp.retrbinary(f"RETR {events_path}", ev_buf.write)
            events_xml = ev_buf.getvalue().decode("utf-8", errors="ignore")

            ev_parts = NitradoAPI._extract_marked_zone(
                events_xml,
                NitradoAPI.SHOP_EVENTS_START,
                NitradoAPI.SHOP_EVENTS_END,
                ("</events>",),
            )
            if not ev_parts:
                logger.error("В %s не найден тег </events>", events_path)
                return False

            before_ev, events_zone, after_ev = ev_parts
            updated_events_zone = events_zone

            for event_name, safe_cls in event_class_map.items():
                ev_match = re.search(
                    rf'<event\s+name="{re.escape(event_name)}"\s*>.*?</event>',
                    updated_spawns_zone,
                    flags=re.DOTALL,
                )
                total_pos = len(re.findall(r"<pos\b", ev_match.group(0))) if ev_match else 1
                total_pos = max(1, total_pos)

                if f'name="{event_name}"' not in updated_events_zone:
                    if updated_events_zone and not updated_events_zone.endswith("\n"):
                        updated_events_zone += "\n"
                    elif not updated_events_zone:
                        updated_events_zone = "\n"
                    updated_events_zone += NitradoAPI._build_shop_event_block(event_name, safe_cls, count=total_pos)
                else:
                    def _update_event_counts(m: re.Match) -> str:
                        block = m.group(0)
                        block = re.sub(r"<nominal>\d+</nominal>", f"<nominal>{total_pos}</nominal>", block)
                        block = re.sub(r"<min>\d+</min>", f"<min>{total_pos}</min>", block)
                        block = re.sub(r"<max>\d+</max>", f"<max>{total_pos}</max>", block)
                        block = re.sub(r"<cleanupradius>\d+</cleanupradius>", "<cleanupradius>0</cleanupradius>", block)
                        block = re.sub(r"<active>\d+</active>", "<active>1</active>", block)
                        block = re.sub(
                            r'(<child\b[^>]*?)\bmax="\d+"\s+min="\d+"',
                            rf'\1max="{total_pos}" min="{total_pos}"',
                            block,
                        )
                        return block

                    updated_events_zone = re.sub(
                        rf'<event\s+name="{re.escape(event_name)}"\s*>.*?</event>',
                        _update_event_counts,
                        updated_events_zone,
                        flags=re.DOTALL,
                    )

            NitradoAPI._atomic_ftp_write(
                ftp,
                events_path,
                f"{before_ev}{updated_events_zone}{after_ev}".encode("utf-8"),
            )
            return True
        finally:
            NitradoAPI._close_ftp(ftp)

    async def append_event_spawn_orders(
        self,
        items: list[dict[str, Any]],
        x: float,
        z: float,
        radius: float = 3.0,
        y: float | None = None,
    ) -> bool:
        """Добавляет координаты заказанных товаров в cfgeventspawns.xml и активирует их в db/events.xml."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            async with self._xml_lock, self._ftp_semaphore:
                return await asyncio.to_thread(
                    self._append_event_spawns_sync, *creds, items, x, z, radius, y
                )
        except Exception as e:
            logger.error("Ошибка при записи позиций в cfgeventspawns.xml: %s", e)
            return False
    @staticmethod
    def _clear_event_spawns_sync(
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> bool:
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                logger.error("clear_event_spawns: папка миссии не найдена")
                return False

            # 1. Очищаем секцию магазина в cfgeventspawns.xml
            ftp.cwd("/")
            ftp.cwd(mission_dir)
            buf = BytesIO()
            ftp.retrbinary("RETR cfgeventspawns.xml", buf.write)
            spawns_xml = buf.getvalue().decode("utf-8", errors="ignore")

            spawn_parts = NitradoAPI._extract_marked_zone(
                spawns_xml,
                NitradoAPI.SHOP_SPAWNS_START,
                NitradoAPI.SHOP_SPAWNS_END,
                ("</events>", "</eventposdef>"),
            )
            if spawn_parts:
                before_sp, spawns_zone, after_sp = spawn_parts
                if spawns_zone.strip():
                    NitradoAPI._atomic_ftp_write(
                        ftp,
                        "cfgeventspawns.xml",
                        f"{before_sp}\n{after_sp}".encode("utf-8"),
                    )
                    logger.info("Секция спавнов магазина в cfgeventspawns.xml очищена")

            # 2. Обнуляем события ТОЛЬКО внутри секции магазина в db/events.xml
            events_path = NitradoAPI._resolve_events_xml_path(ftp, mission_dir)
            ev_buf = BytesIO()
            ftp.retrbinary(f"RETR {events_path}", ev_buf.write)
            events_xml = ev_buf.getvalue().decode("utf-8", errors="ignore")

            ev_parts = NitradoAPI._extract_marked_zone(
                events_xml,
                NitradoAPI.SHOP_EVENTS_START,
                NitradoAPI.SHOP_EVENTS_END,
                ("</events>",),
            )
            if ev_parts:
                before_ev, events_zone, after_ev = ev_parts
                if events_zone.strip():
                    def _deactivate_shop_event(m: re.Match) -> str:
                        block = m.group(0)
                        block = re.sub(r"<nominal>\d+</nominal>", "<nominal>0</nominal>", block)
                        block = re.sub(r"<min>\d+</min>", "<min>0</min>", block)
                        block = re.sub(r"<max>\d+</max>", "<max>0</max>", block)
                        block = re.sub(r"<active>\d+</active>", "<active>0</active>", block)
                        return block

                    updated_ev_zone, count = re.subn(
                        r'<event\s+name="ItemShop_[^"]+"\s*>.*?</event>',
                        _deactivate_shop_event,
                        events_zone,
                        flags=re.DOTALL,
                    )
                    if count > 0:
                        NitradoAPI._atomic_ftp_write(
                            ftp,
                            events_path,
                            f"{before_ev}{updated_ev_zone}{after_ev}".encode("utf-8"),
                        )

            return True
        finally:
            NitradoAPI._close_ftp(ftp)

    async def clear_event_spawn_orders(self) -> bool:
        """Очищает секцию магазина в cfgeventspawns.xml и обнуляет события в секции db/events.xml."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            async with self._xml_lock, self._ftp_semaphore:
                return await asyncio.to_thread(self._clear_event_spawns_sync, *creds)
        except Exception as e:
            logger.error("Ошибка при очистке спавнов магазина: %s", e)
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
        ftp = NitradoAPI._open_ftp(host, port, username, password)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                logger.error("Не найдена папка миссии для изменения cfggameplay.json")
                return False

            ftp.cwd("/")
            ftp.cwd(mission_dir)
            buf = BytesIO()
            ftp.retrbinary("RETR cfggameplay.json", buf.write)
            raw_json = buf.getvalue().decode("utf-8", errors="ignore")

            flag_val = "false" if raid_enabled else "true"
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

            NitradoAPI._atomic_ftp_write(ftp, "cfggameplay.json", updated_json.encode("utf-8"))
            logger.info(
                "Обновлён cfggameplay.json: raid_enabled=%s (disableBaseDamage=%s)",
                raid_enabled,
                flag_val,
            )
            return True
        finally:
            NitradoAPI._close_ftp(ftp)

    async def set_raid_mode(self, raid_enabled: bool) -> bool:
        """Включает или отключает урон по постройкам и палаткам в cfggameplay.json."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False
        try:
            async with self._xml_lock, self._ftp_semaphore:
                return await asyncio.to_thread(self._set_raid_mode_sync, *creds, raid_enabled)
        except Exception as e:
            logger.error("Ошибка при обновлении cfggameplay.json: %s", e)
            return False
    # ==================== УПРАВЛЕНИЕ ВАЙПАМИ (ПОЛНЫЙ И АВТО) ====================

    def _get_events_backup_path(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        return self.backup_dir / f"events_backup_{self.service_id}.xml"

    def _save_backup_to_disk(self, xml_content: str) -> None:
        path = self._get_events_backup_path()
        fd, tmp_path = tempfile.mkstemp(dir=str(self.backup_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(xml_content)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def _load_backup_from_disk(self) -> str | None:
        path = self._get_events_backup_path()
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def _remove_backup_from_disk(self) -> None:
        path = self._get_events_backup_path()
        if path.exists():
            try:
                path.unlink()
            except Exception as err:
                logger.warning("Не удалось удалить временный бэкап %s: %s", path, err)

    @staticmethod
    def _wipe_storage_via_ftp_sync(
        host: str,
        port: int,
        username: str,
        password: str,
    ) -> tuple[bool, str]:
        """Удаляет файлы базы данных и хранилища миссии DayZ (storage_1) для полного вайпа."""
        ftp = NitradoAPI._open_ftp(host, port, username, password, timeout=20)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                return False, "Не найдена папка миссии на FTP Nitrado."

            storage_dir = f"{mission_dir}/storage_1"
            try:
                ftp.cwd("/")
                ftp.cwd(storage_dir)
            except error_perm:
                return False, f"Папка хранилища {storage_dir} не найдена."

            def _clean_dir_recursive(path: str) -> int:
                deleted = 0
                ftp.cwd("/")
                ftp.cwd(path)
                entries = ftp.nlst()
                for entry in entries:
                    clean_entry = entry.rstrip("/").split("/")[-1]
                    if clean_entry in (".", "..", ""):
                        continue
                    full_path = f"{path}/{clean_entry}"
                    try:
                        ftp.delete(full_path)
                        deleted += 1
                    except error_perm:
                        deleted += _clean_dir_recursive(full_path)
                        try:
                            ftp.cwd("/")
                            ftp.cwd(path)
                            ftp.rmd(full_path)
                        except error_perm:
                            pass
                return deleted

            removed_files = _clean_dir_recursive(storage_dir)
            logger.info("Полный вайп: очищено %d файлов в %s", removed_files, storage_dir)
            return True, f"Очищено файлов сохранения: {removed_files}."
        finally:
            NitradoAPI._close_ftp(ftp)
    @staticmethod
    def _set_vehicles_nominal_sync(
        host: str,
        port: int,
        username: str,
        password: str,
        zero_mode: bool,
        backup_xml: str | None = None,
    ) -> tuple[bool, str | None]:
        """Временно обнуляет nominal/min/max у Vehicle* событий в events.xml без удаления XML-комментариев."""
        ftp = NitradoAPI._open_ftp(host, port, username, password, timeout=20)
        try:
            mission_dir = NitradoAPI._find_mission_dir_sync(ftp)
            if not mission_dir:
                return False, None

            events_path = NitradoAPI._resolve_events_xml_path(ftp, mission_dir)
            if not zero_mode and backup_xml:
                NitradoAPI._atomic_ftp_write(ftp, events_path, backup_xml.encode("utf-8"))
                return True, None

            buf = BytesIO()
            ftp.retrbinary(f"RETR {events_path}", buf.write)
            original_xml = buf.getvalue().decode("utf-8", errors="ignore")

            changed = 0

            def _zero_vehicle_block(m: re.Match) -> str:
                nonlocal changed
                block = m.group(0)
                block = re.sub(r"<nominal>\d+</nominal>", "<nominal>0</nominal>", block)
                block = re.sub(r"<min>\d+</min>", "<min>0</min>", block)
                block = re.sub(r"<max>\d+</max>", "<max>0</max>", block)
                changed += 1
                return block

            modified_xml = re.sub(
                r'<event\s+name="Vehicle[^"]*"\s*>.*?</event>',
                _zero_vehicle_block,
                original_xml,
                flags=re.DOTALL,
            )

            if changed == 0:
                return False, None

            NitradoAPI._atomic_ftp_write(ftp, events_path, modified_xml.encode("utf-8"))
            return True, original_xml
        finally:
            NitradoAPI._close_ftp(ftp)

    async def restore_pending_events_backup(self) -> bool:
        """
        Проверяет при старте бота, не остался ли незавершённый бэкап events.xml после вайпа авто.
        Если бэкап найден на диске, восстанавливает его на FTP и удаляет локальный файл.
        """
        backup_xml = self._load_backup_from_disk()
        if not backup_xml:
            return False

        creds = await self._get_ftp_credentials()
        if not creds:
            logger.error("Найден незавершённый бэкап events.xml, но не удалось получить FTP-реквизиты")
            return False

        try:
            async with self._xml_lock, self._ftp_semaphore:
                ok_restore, _ = await asyncio.to_thread(
                    self._set_vehicles_nominal_sync, *creds, False, backup_xml
                )
            if ok_restore:
                self._remove_backup_from_disk()
                await self.restart_server()
                logger.info("Незавершённый бэкап events.xml успешно восстановлен после перезапуска бота")
                return True
        except Exception as e:
            logger.error("Ошибка при восстановлении бэкапа events.xml при старте: %s", e)
        return False

    async def _wipe_cars_via_events_xml(self) -> tuple[bool, str]:
        """Выполняет вайп автомобилей: обнуляет Vehicle-эвенты, перезапускает сервер и возвращает штатный events.xml."""
        creds = await self._get_ftp_credentials()
        if not creds:
            return False, "Не удалось получить FTP-реквизиты Nitrado."

        async with self._xml_lock:
            async with self._ftp_semaphore:
                ok_zero, backup_xml = await asyncio.to_thread(
                    self._set_vehicles_nominal_sync, *creds, True, None
                )
            if not ok_zero or not backup_xml:
                return False, "Не удалось обновить Vehicle-события в events.xml."

            self._save_backup_to_disk(backup_xml)

            if not await self.restart_server():
                async with self._ftp_semaphore:
                    ok_rollback, _ = await asyncio.to_thread(
                        self._set_vehicles_nominal_sync, *creds, False, backup_xml
                    )
                if ok_rollback:
                    self._remove_backup_from_disk()
                return False, "Не удалось отправить команду рестарта в Nitrado."

            # Ждём прогрузки сервера, чтобы DayZ удалил старые автомобили с карты
            await asyncio.sleep(150)

            creds_after = await self._get_ftp_credentials() or creds
            async with self._ftp_semaphore:
                ok_restore, _ = await asyncio.to_thread(
                    self._set_vehicles_nominal_sync, *creds_after, False, backup_xml
                )
            if not ok_restore:
                return False, "Старые авто удалены, но не удалось восстановить исходный events.xml (бэкап сохранён на диске)!"

            self._remove_backup_from_disk()
            await self.restart_server()
            return True, "Транспорт очищен, штатный спавн восстановлен, сервер перезапускается."

    async def _wait_until_stopped(self, max_wait: int = 90) -> bool:
        """Ожидает перехода сервера в статус stopped перед очисткой файлов сохранения."""
        elapsed = 0
        while elapsed < max_wait:
            await asyncio.sleep(5)
            elapsed += 5
            gs = await self.get_server_status()
            if gs and str(gs.get("status", "")).lower() == "stopped":
                return True
        return False

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

        is_stopped = await self._wait_until_stopped(max_wait=90)
        if not is_stopped:
            logger.error("Сервер не перешёл в статус stopped за 90 секунд; полный вайп отменён для защиты файлов.")
            return False, "Сервер не успел остановиться за 90 секунд. Вайп отменён во избежание повреждения базы."

        creds = await self._get_ftp_credentials()
        if not creds:
            await self.start_server()
            return False, "Не удалось получить FTP-реквизиты Nitrado после остановки сервера."

        try:
            async with self._xml_lock, self._ftp_semaphore:
                ok_clean, msg = await asyncio.to_thread(self._wipe_storage_via_ftp_sync, *creds)
        except Exception as e:
            logger.error("Ошибка очистки storage_1 при полном вайпе: %s", e, exc_info=True)
            ok_clean, msg = False, f"Ошибка FTP при очистке хранилища: {e}"

        started = await self.start_server()
        if not started:
            msg += " Внимание: не удалось автоматически запустить сервер после вайпа!"

        return ok_clean, msg
