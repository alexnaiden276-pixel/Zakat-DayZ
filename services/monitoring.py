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

    while True:
        try:
            server_data = await nitrado.get_server_status()
            text = format_server_info(server_data)

            if not message_id:
                # Отправляем НОВОЕ сообщение, если старого нет в базе
                msg = await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode="HTML",
                )
                message_id = msg.message_id
                await db.set_setting("dayz_monitor_message_id", str(message_id))
            else:
                try:
                    await bot.edit_message_text(
                        text=text,
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode="HTML",
                    )
                except TelegramRetryAfter as e:
                    logger.warning("Флуд-контроль Telegram, пауза %s сек.", e.retry_after)
                    await asyncio.sleep(e.retry_after)
                    continue  # Сразу повторяем попытку
                except TelegramAPIError as e:
                    err_text = str(e).lower()
                    
                    if "message is not modified" in err_text:
                        # Текст не изменился (например, сервер всё еще выключен). 
                        # Просто логируем и идем дальше спать на 5 минут.
                        logger.debug("Текст мониторинга не изменился.")
                        
                    elif (
                        "message to edit not found" in err_text
                        or "message can't be edited" in err_text
                        or "chat not found" in err_text
                    ):
                        # Сообщение было удалено из чата или у бота забрали права
                        logger.info("Старое сообщение удалено или недоступно. Создаём новое.")
                        message_id = None
                        await db.set_setting("dayz_monitor_message_id", "")
                        continue  # МГНОВЕННО переходим к отправке нового сообщения
                    else:
                        logger.error("Ошибка Telegram при редактировании: %s", e)

        except asyncio.CancelledError:
            logger.info("Задача мониторинга сервера остановлена")
            raise
        except Exception as e:
            logger.exception("Непредвиденная ошибка в цикле мониторинга: %s", e)

        # Сон между успешными проверками
        await asyncio.sleep(300)
