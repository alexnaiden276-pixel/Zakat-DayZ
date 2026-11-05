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


def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("В .env не задан BOT_TOKEN")

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
    )