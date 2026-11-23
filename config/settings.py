import os
import logging
from dataclasses import dataclass

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


def _parse_ids(value: str) -> set[int]:
    result = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                pass
    return result


def _safe_int(value: str | None, default: int | None = None) -> int | None:
    """Безопасно преобразует строку в int, предотвращая падение по ValueError."""
    if not value or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        logger.error("Ошибка конфигурации: Не удалось привести '%s' к типу int", value)
        return default


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: str
    admin_ids: set[int]
    admin_chat_id: int | None
    sell_location: str
    sell_coordinates: str
    restart_at: str
    rules_text: str
    nitrado_token: str
    nitrado_server_id: str
    target_chat_id: int
    target_thread_id: int | None
    # Поля для логирования убийств в отдельную тему
    kill_chat_id: int
    kill_thread_id: int | None
    # Поля для публикации таблицы лидеров в отдельную тему
    leaderboard_chat_id: int
    leaderboard_thread_id: int | None
    # Поля для публикации карточек группировок в отдельную тему
    groups_chat_id: int
    groups_thread_id: int | None
    # Поля для публикации анонсов рейд-тайма в отдельную тему
    raid_chat_id: int
    raid_thread_id: int | None
    server_display_name: str


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("В .env не задан BOT_TOKEN")

    nitrado_token = os.getenv("NITRADO_TOKEN", "").strip()
    if not nitrado_token:
        raise RuntimeError("В .env не задан NITRADO_TOKEN")

    nitrado_server_id = os.getenv("NITRADO_SERVER_ID", "").strip()
    if not nitrado_server_id:
        raise RuntimeError("В .env не задан NITRADO_SERVER_ID")

    target_chat_raw = os.getenv("TARGET_CHAT_ID", "").strip()
    target_chat_id = _safe_int(target_chat_raw)
    if target_chat_id is None:
        raise RuntimeError("В .env не задан или некорректно указан TARGET_CHAT_ID")

    # Безопасный парсинг данных для логов убийств
    kill_chat_raw = os.getenv("KILL_CHAT_ID", "").strip()
    kill_chat_id = _safe_int(kill_chat_raw) if kill_chat_raw else target_chat_id
    if kill_chat_id is None:
        raise RuntimeError("Не удалось определить ID чата для вывода убийств")

    # Безопасный парсинг чата для таблицы лидеров
    leaderboard_chat_raw = os.getenv("LEADERBOARD_CHAT_ID", "").strip()
    leaderboard_chat_id = _safe_int(leaderboard_chat_raw) if leaderboard_chat_raw else target_chat_id
    if leaderboard_chat_id is None:
        raise RuntimeError("Не удалось определить ID чата для таблицы лидеров")

    # Безопасный парсинг чата для карточек группировок
    groups_chat_raw = os.getenv("GROUPS_CHAT_ID", "").strip()
    groups_chat_id = _safe_int(groups_chat_raw) if groups_chat_raw else target_chat_id
    if groups_chat_id is None:
        raise RuntimeError("Не удалось определить ID чата для карточек группировок")

    target_thread_id = _safe_int(os.getenv("TARGET_THREAD_ID"))
    admin_chat_id = _safe_int(os.getenv("ADMIN_CHAT_ID"))
    kill_thread_id = _safe_int(os.getenv("KILL_THREAD_ID"))
    leaderboard_thread_id = _safe_int(os.getenv("LEADERBOARD_THREAD_ID"))
    groups_thread_id = _safe_int(os.getenv("GROUPS_THREAD_ID"))

    # Безопасный парсинг чата и темы для анонсов рейд-тайма
    raid_chat_raw = os.getenv("RAID_CHAT_ID", "").strip()
    raid_chat_id = _safe_int(raid_chat_raw) if raid_chat_raw else target_chat_id
    if raid_chat_id is None:
        raise RuntimeError("Не удалось определить ID чата для анонсов рейд-тайма")

    raid_thread_raw = os.getenv("RAID_THREAD_ID", "").strip()
    raid_thread_id = _safe_int(raid_thread_raw) if raid_thread_raw else target_thread_id

    server_display_name = os.getenv(
        "SERVER_DISPLAY_NAME",
        "! Zakat Dayz | RUSSIA RU | 3PP | LOOT+ | RAID SAT-SUN | SHOP | TG",
    ).strip()

    return Settings(
        bot_token=token,
        db_path=os.getenv("DB_PATH", "data/dayz.sqlite3"),
        admin_ids=_parse_ids(os.getenv("ADMIN_IDS", "")),
        admin_chat_id=admin_chat_id,
        sell_location=os.getenv("SELL_LOCATION", "Точка сдачи"),
        sell_coordinates=os.getenv("SELL_COORDINATES", "не заданы"),
        restart_at=os.getenv("RESTART_AT", "время не задано"),
        rules_text=os.getenv(
            "RULES_TEXT",
            "Правила сервера пока не настроены.",
        ).replace("\\n", "\n"),
        nitrado_token=nitrado_token,
        nitrado_server_id=nitrado_server_id,
        target_chat_id=target_chat_id,
        target_thread_id=target_thread_id,
        kill_chat_id=kill_chat_id,
        kill_thread_id=kill_thread_id,
        leaderboard_chat_id=leaderboard_chat_id,
        leaderboard_thread_id=leaderboard_thread_id,
        groups_chat_id=groups_chat_id,
        groups_thread_id=groups_thread_id,
        raid_chat_id=raid_chat_id,
        raid_thread_id=raid_thread_id,
        server_display_name=server_display_name,
    )
