import logging
import os
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


def _parse_chat_target(
    chat_env_key: str,
    thread_env_key: str,
    default_chat_id: int,
    default_thread_id: int | None = None,
    inherit_thread_on_same_chat: bool = False,
) -> tuple[int, int | None]:
    """Парсит ID чата и темы, безопасно наследуя thread_id только при совпадении чата."""
    chat_raw = os.getenv(chat_env_key, "").strip()
    chat_id = _safe_int(chat_raw, default=default_chat_id) if chat_raw else default_chat_id
    if chat_id is None:
        chat_id = default_chat_id

    thread_raw = os.getenv(thread_env_key, "").strip()
    if thread_raw:
        thread_id = _safe_int(thread_raw)
    elif inherit_thread_on_same_chat and chat_id == default_chat_id:
        thread_id = default_thread_id
    else:
        thread_id = None

    return chat_id, thread_id


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: str
    admin_ids: set[int]
    admin_chat_id: int | None
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
    # Поля для публикации анонсов Опасных зон (Кровавый час) в отдельную тему
    hotzone_chat_id: int
    hotzone_thread_id: int | None
    # Поля для публикации кнопки личного кабинета в отдельную тему
    cabinet_chat_id: int
    cabinet_thread_id: int | None
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

    target_chat_id = _safe_int(os.getenv("TARGET_CHAT_ID"))
    if target_chat_id is None:
        raise RuntimeError("В .env не задан или некорректно указан TARGET_CHAT_ID")

    target_thread_id = _safe_int(os.getenv("TARGET_THREAD_ID"))
    admin_chat_id = _safe_int(os.getenv("ADMIN_CHAT_ID"))

    kill_chat_id, kill_thread_id = _parse_chat_target(
        "KILL_CHAT_ID", "KILL_THREAD_ID", target_chat_id
    )
    leaderboard_chat_id, leaderboard_thread_id = _parse_chat_target(
        "LEADERBOARD_CHAT_ID", "LEADERBOARD_THREAD_ID", target_chat_id
    )
    groups_chat_id, groups_thread_id = _parse_chat_target(
        "GROUPS_CHAT_ID", "GROUPS_THREAD_ID", target_chat_id
    )
    raid_chat_id, raid_thread_id = _parse_chat_target(
        "RAID_CHAT_ID", "RAID_THREAD_ID", target_chat_id, target_thread_id, inherit_thread_on_same_chat=True
    )
    hotzone_chat_id, hotzone_thread_id = _parse_chat_target(
        "HOTZONE_CHAT_ID", "HOTZONE_THREAD_ID", target_chat_id, target_thread_id, inherit_thread_on_same_chat=True
    )
    cabinet_chat_id, cabinet_thread_id = _parse_chat_target(
        "CABINET_CHAT_ID", "CABINET_THREAD_ID", target_chat_id
    )

    server_display_name = os.getenv(
        "SERVER_DISPLAY_NAME",
        "! Zakat Dayz | RUSSIA RU | 3PP | LOOT+ | RAID SAT-SUN | SHOP | TG",
    ).strip()

    return Settings(
        bot_token=token,
        db_path=os.getenv("DB_PATH", "data/dayz.sqlite3"),
        admin_ids=_parse_ids(os.getenv("ADMIN_IDS", "")),
        admin_chat_id=admin_chat_id,
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
        hotzone_chat_id=hotzone_chat_id,
        hotzone_thread_id=hotzone_thread_id,
        cabinet_chat_id=cabinet_chat_id,
        cabinet_thread_id=cabinet_thread_id,
        server_display_name=server_display_name,
    )
