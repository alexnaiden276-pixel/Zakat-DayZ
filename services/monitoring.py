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

# Понятное отображение всех возможных статусов Nitrado API
STATUS_MAP = {
    "started": "🟢 Онлайн",
    "restarting": "🟡 Перезагрузка",
    "stopping": "🟠 Выключается",
    "stopped": "🔴 Выключен",
    "installing": "🔵 Установка / Обновление",
    "suspended": "⛔ Приостановлен",
}


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

    status_raw = str(server_data.get("status", "unknown")).lower()
    status_display = STATUS_MAP.get(status_raw, f"⚪ Неизвестно ({html.escape(status_raw)})")

    slots = server_data.get("slots") or query.get("player_max", 0)
    players_online = query.get("player_current", 0)
    map_name = html.escape(str(query.get("map", "Chernarus")))

    return (
        f"🎮 <b>Мониторинг сервера DayZ (PlayStation)</b>\n\n"
        f"📛 <b>Название:</b> {server_name}\n"
        f"📊 <b>Статус:</b> {status_display}\n"
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
    initial_message_id: int | None = None,
) -> None:
    """Фоновая задача для создания и обновления сообщения в теме группы."""
    logger.info("[MONITOR] Фоновая задача мониторинга запущена.")
    message_id = initial_message_id

    while True:
        try:
            server_data = await nitrado.get_server_status()
            text = format_server_info(server_data)

            # Пытаемся отредактировать существующее сообщение
            if message_id:
                try:
                    await bot.edit_message_text(
                        text=text,
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode="HTML",
                    )
                    logger.debug("[MONITOR] Сообщение %s успешно обновлено.", message_id)
                except TelegramRetryAfter as e:
                    logger.warning("[MONITOR] Флуд-контроль Telegram, пауза %s сек.", e.retry_after)
                    await asyncio.sleep(e.retry_after)
                    continue
                except TelegramAPIError as e:
                    err_text = str(e).lower()
                    if "message is not modified" in err_text:
                        logger.debug("[MONITOR] Текст сообщения не изменился.")
                    elif (
                        "message to edit not found" in err_text
                        or "message can't be edited" in err_text
                        or "chat not found" in err_text
                    ):
                        logger.warning("[MONITOR] Старое сообщение потеряно. Будет создано новое.")
                        message_id = None
                    else:
                        logger.error("[MONITOR] Ошибка Telegram API при редактировании: %s", e)

            # Если сообщения нет (первый запуск или оно было удалено выше) — отправляем новое
            if not message_id:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode="HTML",
                )
                message_id = msg.message_id
                logger.info("[MONITOR] Создано новое сообщение мониторинга с ID: %s", message_id)

                try:
                    await db.set_setting("dayz_monitor_message_id", str(message_id))
                except Exception as db_err:
                    logger.error("[MONITOR] Ошибка сохранения ID в базу: %s", db_err)

        except asyncio.CancelledError:
            logger.info("[MONITOR] Задача мониторинга остановлена.")
            raise
        except Exception as e:
            logger.error("[MONITOR] Критическая ошибка в цикле мониторинга: %s", e, exc_info=True)

        await asyncio.sleep(300)
