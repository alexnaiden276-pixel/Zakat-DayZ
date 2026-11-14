import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
POLL_INTERVAL_SECONDS = 300

STATUS_MAP = {
    "started": "🟢 Онлайн",
    "restarting": "🟡 Перезагрузка",
    "stopping": "🟠 Выключается",
    "stopped": "🔴 Выключен",
    "installing": "🔵 Установка / Обновление",
    "suspended": "⛔ Приостановлен",
}


def _ensure_msk(dt: datetime | None) -> datetime | None:
    """Гарантирует, что объект datetime имеет часовой пояс MSK."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=MSK)
    return dt.astimezone(MSK)


def format_restart_line(status_raw: str, next_restart_msk: datetime | None) -> str:
    """Формирует строку с обратным отсчётом и точным временем рестарта по МСК."""
    if status_raw == "restarting":
        return "⏳ <b>Рестарт:</b> <i>идёт прямо сейчас...</i>\n"
    if status_raw in {"stopped", "stopping"}:
        return "⏳ <b>До рестарта:</b> <i>сервер остановлен</i>\n"
    if not next_restart_msk:
        return "⏳ <b>До рестарта:</b> <i>уточняется...</i>\n"

    now_msk = datetime.now(MSK)
    delta = next_restart_msk - now_msk
    total_minutes = int(delta.total_seconds() // 60)
    exact_time_str = next_restart_msk.strftime("%H:%M МСК")

    if total_minutes <= 0:
        return f"⏳ <b>До рестарта:</b> <code>менее 1 мин.</code> (в <code>{exact_time_str}</code>)\n"

    hours, minutes = divmod(total_minutes, 60)
    if hours > 0 and minutes > 0:
        countdown_str = f"{hours} ч. {minutes} мин."
    elif hours > 0:
        countdown_str = f"{hours} ч."
    else:
        countdown_str = f"{minutes} мин."

    return f"⏳ <b>До рестарта:</b> <code>{countdown_str}</code> (в <code>{exact_time_str}</code>)\n"


def format_server_info(
    server_data: dict | None,
    next_restart_msk: datetime | None = None,
) -> str:
    now_str = datetime.now(MSK).strftime("%H:%M:%S МСК")
    interval_min = max(1, POLL_INTERVAL_SECONDS // 60)

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

    restart_line = format_restart_line(status_raw, next_restart_msk)

    return (
        f"🎮 <b>Мониторинг сервера DayZ (PlayStation)</b>\n\n"
        f"📛 <b>Название:</b> {server_name}\n"
        f"📊 <b>Статус:</b> {status_display}\n"
        f"🗺️ <b>Карта:</b> {map_name}\n"
        f"👥 <b>Игроки:</b> <code>{players_online}/{slots}</code>\n"
        f"{restart_line}\n"
        f"🔄 <i>Обновлено: {now_str} (каждые {interval_min} мин.)</i>"
    )


async def start_server_monitoring(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    chat_id: int,
    thread_id: int | None = None,
    initial_message_id: int | None = None,
) -> None:
    """Фоновая задача для мониторинга статуса, времени рестарта и автоочистки заказов."""
    logger.info("[MONITOR] Фоновая задача мониторинга запущена.")

    message_id = initial_message_id
    if not message_id:
        try:
            saved_id = await db.get_setting("dayz_monitor_message_id")
            if saved_id and str(saved_id).strip().isdigit():
                message_id = int(str(saved_id).strip())
                logger.info("[MONITOR] Восстановлен ID сообщения из БД: %s", message_id)
        except Exception as db_err:
            logger.error("[MONITOR] Ошибка чтения ID сообщения из БД: %s", db_err)

    last_known_status: str | None = None
    next_restart_msk: datetime | None = None
    orders_cleared = False

    while True:
        try:
            server_data = await nitrado.get_server_status()

            if server_data:
                current_status = str(server_data.get("status", "")).lower()
                now_msk = datetime.now(MSK)

                # 1. Фиксируем рестарт по смене статуса в Nitrado API
                if last_known_status in {"restarting", "stopped", "stopping"} and current_status == "started":
                    if not orders_cleared:
                        logger.info("[MONITOR] Сервер перезапущен (по статусу API)! Очищаем bot_shop.json...")
                        if await nitrado.clear_json_spawn_orders():
                            orders_cleared = True
                    next_restart_msk = None

                # 2. Фиксируем плановый рестарт по таймеру из messages.xml (если API не менял статус)
                elif current_status == "started" and next_restart_msk and next_restart_msk <= now_msk:
                    if not orders_cleared:
                        logger.info("[MONITOR] Наступило плановое время рестарта! Очищаем bot_shop.json...")
                        if await nitrado.clear_json_spawn_orders():
                            orders_cleared = True
                    next_restart_msk = None

                # 3. Вычисляем время следующего рестарта
                if current_status == "started" and next_restart_msk is None:
                    fetched_restart = _ensure_msk(
                        await nitrado.get_next_restart_time(server_data=server_data)
                    )
                    # Принимаем новое время только если оно реально в будущем
                    if fetched_restart and fetched_restart > now_msk + timedelta(minutes=2):
                        next_restart_msk = fetched_restart
                        orders_cleared = False

                last_known_status = current_status

            text = format_server_info(server_data, next_restart_msk)

            if message_id:
                try:
                    await bot.edit_message_text(
                        text=text,
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode="HTML",
                    )
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    continue
                except TelegramAPIError as e:
                    err_text = str(e).lower()
                    if "message is not modified" in err_text:
                        pass
                    elif (
                        "message to edit not found" in err_text
                        or "message can't be edited" in err_text
                        or "chat not found" in err_text
                    ):
                        logger.warning(
                            "[MONITOR] Старое сообщение (%s) недоступно для правки, создаем новое.",
                            message_id,
                        )
                        message_id = None
                    else:
                        logger.error("[MONITOR] Ошибка Telegram API при редактировании: %s", e)

            if not message_id:
                try:
                    msg = await bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    message_id = msg.message_id
                    try:
                        await db.set_setting("dayz_monitor_message_id", str(message_id))
                        logger.info("[MONITOR] Новый ID сообщения (%s) сохранен в БД.", message_id)
                    except Exception as db_err:
                        logger.error("[MONITOR] Ошибка сохранения ID в БД: %s", db_err)
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after)
                    continue

        except asyncio.CancelledError:
            logger.info("[MONITOR] Задача мониторинга остановлена.")
            raise
        except Exception as e:
            logger.error("[MONITOR] Критическая ошибка в цикле: %s", e, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
