import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))


def format_server_info(server_data: dict | None) -> str:
    now_str = datetime.now(MSK).strftime("%H:%M:%S МСК")

    if not server_data:
        return (
            "❌ <b>Сервер DayZ временно недоступен (ошибка API)</b>\n\n"
            f"🕒 <i>Обновлено: {now_str}</i>"
        )

    settings = server_data.get("settings") or {}
    config = settings.get("config") or {}
    query = server_data.get("query") or {}

    raw_name = (
        query.get("server_name")
        or config.get("hostname")
        or "DayZ PlayStation Server"
    )
    server_name = html.escape(str(raw_name))

    status = server_data.get("status")
    slots = server_data.get("slots") or query.get("player_max", 0)
    players_online = query.get("player_current", 0)
    map_name = html.escape(str(query.get("map", "Chernarus")))

    status_emoji = "🟢 Онлайн" if status == "started" else "🔴 Выключен"

    return (
        f"🎮 <b>Мониторинг сервера DayZ (PlayStation)</b>\n\n"
        f"📛 <b>Название:</b> {server_name}\n"
        f"📊 <b>Статус:</b> {status_emoji}\n"
        f"🗺️ <b>Карта:</b> {map_name}\n"
        f"👥 <b>Игроки:</b> <code>{players_online}/{slots}</code>\n\n"
        f"🔄 <i>Обновлено: {now_str} (каждые 5 мин.)</i>"
    )


async def start_server_monitoring(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    chat_id: int,
    thread_id: int | None = None,
) -> None:
    """Фоновая задача для создания и обновления сообщения в теме группы."""
    raw_msg_id = await db.get_setting("dayz_monitor_message_id")
    try:
        message_id: int | None = int(raw_msg_id) if raw_msg_id else None
    except (ValueError, TypeError):
        message_id = None

 print("\n[MONITOR] >>> ФОНОВАЯ ЗАДАЧА ЗАПУЩЕНА <<<")
    message_id = None  # Принудительный сброс для теста

    while True:
        try:
            print("[MONITOR] Шаг 1: Запрашиваем данные от Nitrado API...")
            server_data = await nitrado.get_server_status()
            
            print(f"[MONITOR] Шаг 2: Данные получены! Результат: {str(server_data)[:100]}...")
            text = format_server_info(server_data)

            print(f"[MONITOR] Шаг 3: Пытаемся отправить сообщение в Chat_ID: {chat_id}, Thread_ID: {thread_id}")
            
            if not message_id:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode="HTML",
                )
                message_id = msg.message_id
                print(f"[MONITOR] УСПЕХ! Новое сообщение отправлено, ID: {message_id}")
                await db.set_setting("dayz_monitor_message_id", str(message_id))
            else:
                await bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode="HTML",
                )
                print(f"[MONITOR] УСПЕХ! Сообщение {message_id} успешно обновлено")

        except asyncio.CancelledError:
            print("[MONITOR] Задача мониторинга была отменена!")
            raise
        except Exception as e:
            print(f"\n[MONITOR] !!! КРИТИЧЕСКАЯ ОШИБКА В ЦИКЛЕ: {e} !!!\n")
            logger.exception("Ошибка: %s", e)

        print("[MONITOR] Шаг 4: Засыпаем на 5 минут...\n")
        await asyncio.sleep(300)
