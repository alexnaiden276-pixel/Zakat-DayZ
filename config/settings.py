import os
from dataclasses import dataclass

from dotenv import load_dotenv

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
    if not target_chat_raw:
        raise RuntimeError("В .env не задан TARGET_CHAT_ID")

    target_thread_raw = os.getenv("TARGET_THREAD_ID", "").strip()
    target_thread_id = int(target_thread_raw) if target_thread_raw else None

    admin_chat_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
    admin_chat_id = int(admin_chat_raw) if admin_chat_raw else None

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
        target_chat_id=int(target_chat_raw),
        target_thread_id=target_thread_id,
    )
