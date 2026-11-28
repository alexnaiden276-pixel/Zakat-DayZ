import hashlib
import math
import re
from typing import Any

# Паттерны для поиска ID и ника игрока в логах подключения (.ADM)
CONNECT_PATTERNS = [
    re.compile(r'Player\s+"(?P<nick>[^"]+)"\s*\(id=(?P<id>[A-Za-z0-9_=-]{8,})'),
    re.compile(r'Player\s+"(?P<nick>[^"]+)"\s+is\s+connected\s*\(id=(?P<id>[A-Za-z0-9_=-]{8,})'),
    re.compile(r'Player\s+"(?P<nick>[^"]+)"\s*\[id=(?P<id>[A-Za-z0-9_=-]{8,})'),
]

# Паттерн отключения игрока от сервера
DISCONNECT_PATTERN = re.compile(
    r'Player\s+"(?P<nick>[^"]+)"(?:\s*\(id=(?P<id>[A-Za-z0-9_=-]{8,})[^)]*\))?\s+has\s+been\s+disconnected',
    re.IGNORECASE,
)

# Паттерн координат игрока в .ADM (формат DayZ PS4: (id=XXXX= pos=<X, Z_север_юг, Y_высота>))
PLAYER_POS_PATTERN = re.compile(
    r'Player\s+"(?P<nick>[^"]+)"\s*\(id=(?P<id>[A-Za-z0-9_=-]+)'
    r'(?:\s*,?\s*pos=<(?P<x>-?\d+(?:\.\d+)?)\s*,\s*(?P<z>-?\d+(?:\.\d+)?)\s*,\s*(?P<y>-?\d+(?:\.\d+)?)>)?\)',
    re.IGNORECASE,
)

# Паттерны для парсинга PvP-убийств в DayZ .ADM логах
KILL_PATTERN = re.compile(
    r'^(?P<time>\d{2}:\d{2}:\d{2})\s*\|\s*Player\s+"(?P<victim>[^"]+)"\s*\(DEAD\)'
    r'(?:\s*\(id=(?P<victim_id>[A-Za-z0-9_=-]+)'
    r'(?:\s*,?\s*pos=<(?P<vx>-?\d+(?:\.\d+)?)\s*,\s*(?P<vz>-?\d+(?:\.\d+)?)\s*,\s*(?P<vy>-?\d+(?:\.\d+)?)>)?\))?'
    r'.*?killed\s+by\s+Player\s+"(?P<killer>[^"]+)"'
    r'(?:\s*\(id=(?P<killer_id>[A-Za-z0-9_=-]+)'
    r'(?:\s*,?\s*pos=<(?P<kx>-?\d+(?:\.\d+)?)\s*,\s*(?P<kz>-?\d+(?:\.\d+)?)\s*,\s*(?P<ky>-?\d+(?:\.\d+)?)>)?\))?'
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
    r'\s*\(id=(?P<id>[A-Za-z0-9_=-]+)\s*,?\s*pos=<(?P<x>-?\d+(?:\.\d+)?)\s*,\s*(?P<z>-?\d+(?:\.\d+)?)\s*,\s*(?P<y>-?\d+(?:\.\d+)?)>\)'
    r'\s*(?:placed|built)\s+(?P<item>.+?)\s*$',
    re.IGNORECASE,
)


def line_hash(line: str) -> str:
    return hashlib.sha1(line.strip().encode("utf-8", errors="ignore")).hexdigest()


def parse_adm_seconds(time_str: str) -> int | None:
    try:
        h, m, s = map(int, time_str.split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def adm_elapsed_seconds(older_time: str, newer_time: str) -> int | None:
    """Возвращает разницу в секундах от older_time до newer_time с учётом перехода через полночь."""
    s_old = parse_adm_seconds(older_time)
    s_new = parse_adm_seconds(newer_time)
    if s_old is None or s_new is None:
        return None
    diff = s_new - s_old
    if diff < -43200:
        diff += 86400
    return max(0, diff)


def is_within_seconds(t1: str, t2: str, max_diff: int = 3) -> bool:
    s1 = parse_adm_seconds(t1)
    s2 = parse_adm_seconds(t2)
    if s1 is None or s2 is None:
        return False
    diff = abs(s2 - s1)
    if diff > 43200:
        diff = 86400 - diff
    return diff <= max_diff


def calc_distance_2d(
    kx: str | None,
    kz: str | None,
    vx: str | None,
    vz: str | None,
) -> float:
    """Считает 2D-дистанцию на карте DayZ (горизонтальные оси X и Z; Y — это высота)."""
    if None in (kx, kz, vx, vz):
        return 0.0
    try:
        dx = float(kx) - float(vx)
        dz = float(kz) - float(vz)
        return round(math.hypot(dx, dz), 1)
    except (TypeError, ValueError):
        return 0.0


def extract_new_lines(lines: list[str], stop_at_hash: str | None) -> list[str]:
    """Возвращает только новые строки лога после строки с хэшем stop_at_hash."""
    if not stop_at_hash:
        return lines
    for idx in range(len(lines) - 1, -1, -1):
        if line_hash(lines[idx]) == stop_at_hash:
            return lines[idx + 1:]
    return lines


def parse_adm_file(
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

    newest_hash = line_hash(lines[-1])
    new_lines = extract_new_lines(lines, stop_at_hash)

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
                dist_val = calc_distance_2d(
                    data.get("kx"), data.get("kz"), data.get("vx"), data.get("vz")
                )

            pair_key = (data["killer"].strip().lower(), data["victim"].strip().lower())
            hs_time = last_headshots.pop(pair_key, None)
            is_headshot = bool(hs_time and is_within_seconds(hs_time, data["time"], max_diff=3))
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
            if prev_pvp_time and is_within_seconds(prev_pvp_time, death_match.group("time"), max_diff=3):
                continue
            events.append({
                "type": "pve_death",
                "file": file_name,
                "time": death_match.group("time"),
                "victim": victim_nick,
            })

    return events, newest_hash


def parse_adm_autoban_events(
    file_name: str,
    content: str,
    stop_at_hash: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Парсит события постройки объектов и PvP-убийств с 2D-координатами (X, Z) для гео-автобана."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return [], None

    newest_hash = line_hash(lines[-1])
    new_lines = extract_new_lines(lines, stop_at_hash)

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
            if None not in (d.get("kx"), d.get("kz"), d.get("vx"), d.get("vz")):
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

