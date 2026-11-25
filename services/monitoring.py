import asyncio
import contextlib
import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.nitrado_api import NitradoAPI
from services.raid_scheduler import format_days_list, is_raid_active_at, load_raid_config
from services.wipe_scheduler import format_ts_msk, load_wipe_config

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
POLL_INTERVAL_SECONDS = 120

# Чётные часы рестартов по МСК (каждые 2 часа начиная с 00:00)
RESTART_HOURS_MSK = (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)

STATUS_MAP = {
    "started": "🟢 Онлайн",
    "restarting": "🟡 Перезагрузка",
    "stopping": "🟠 Выключается",
    "stopped": "🔴 Выключен",
    "installing": "🔵 Установка / Обновление",
    "suspended": "⛔ Приостановлен",
}


def get_next_scheduled_restart_msk(now_msk: datetime | None = None) -> datetime:
    """Вычисляет ближайший чётный час рестарта по МСК."""
    current = now_msk or datetime.now(MSK)
    candidates: list[datetime] = []
    for day_offset in (0, 1):
        base_date = (current + timedelta(days=day_offset)).date()
        for hour in RESTART_HOURS_MSK:
            dt = datetime(base_date.year, base_date.month, base_date.day, hour, 0, 0, tzinfo=MSK)
            if dt > current:
                candidates.append(dt)
    return min(candidates)


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


def format_raid_block(raid_cfg: dict | None, now_msk: datetime | None = None) -> str:
    """Формирует блок со статусом и расписанием рейд-тайма по МСК."""
    cfg = raid_cfg or {}
    current_msk = now_msk or datetime.now(MSK)

    raid_active = is_raid_active_at(current_msk, cfg)
    raid_mode = cfg.get("mode", "auto")
    raid_days = [int(d) for d in cfg.get("days", ())]
    start_h = int(cfg.get("start_hour", 18))
    end_h = int(cfg.get("end_hour", 22))

    if raid_active:
        if raid_mode == "force_on":
            status_line = "💥 <b>Рейд-тайм:</b> ВКЛ (урон по стенам и палаткам активен)"
        else:
            status_line = f"💥 <b>Рейд-тайм:</b> ВКЛ (до <code>{end_h:02d}:00 МСК</code>)"
    else:
        status_line = "🛡 <b>Рейд-тайм:</b> ВЫКЛ (стены и палатки защищены)"

    if raid_mode == "auto" and raid_days:
        days_str = format_days_list(raid_days)
        schedule_line = (
            f"📅 <b>Расписание рейдов:</b> {days_str} | "
            f"с <code>{start_h:02d}:00</code> до <code>{end_h:02d}:00</code> МСК"
        )
    elif raid_mode == "auto":
        schedule_line = "📅 <b>Расписание рейдов:</b> <i>дни не заданы</i>"
    else:
        schedule_line = "📅 <b>Расписание рейдов:</b> <i>ручной режим администрации</i>"

    return f"{status_line}\n{schedule_line}\n"


def format_wipe_block(wipe_cfg: dict | None) -> str:
    """Формирует блок со временем следующего вайпа сервера и транспорта."""
    cfg = wipe_cfg or {}
    lines: list[str] = []

    if cfg.get("full_enabled") and int(cfg.get("full_next_ts") or 0) > 0:
        lines.append(f"🔥 <b>Следующий вайп сервера:</b> <code>{format_ts_msk(int(cfg['full_next_ts']))}</code>")

    if cfg.get("car_enabled") and int(cfg.get("car_next_ts") or 0) > 0:
        lines.append(f"🚗 <b>Следующий вайп авто:</b> <code>{format_ts_msk(int(cfg['car_next_ts']))}</code>")

    return ("\n".join(lines) + "\n") if lines else ""


def format_server_info(
    server_data: dict | None,
    next_restart_msk: datetime | None = None,
    raid_cfg: dict | None = None,
    wipe_cfg: dict | None = None,
) -> str:
    now_msk = datetime.now(MSK)
    now_str = now_msk.strftime("%H:%M:%S МСК")
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
    raid_block = format_raid_block(raid_cfg, now_msk)
    wipe_block = format_wipe_block(wipe_cfg)

    return (
        f"🎮 <b>Мониторинг сервера DayZ (PlayStation)</b>\n\n"
        f"📛 <b>Название:</b> {server_name}\n"
        f"📊 <b>Статус:</b> {status_display}\n"
        f"🗺️ <b>Карта:</b> {map_name}\n"
        f"👥 <b>Игроки:</b> <code>{players_online}/{slots}</code>\n"
        f"{restart_line}"
        f"{raid_block}"
        f"{wipe_block}\n"
        f"🔄 <i>Обновлено: {now_str} (каждые {interval_min} мин.)</i>"
    )
async def _process_post_restart_orders(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    delay_seconds: int = 240,
) -> None:
    """
    Ждёт прогрузки миссии DayZ после рестарта (240 сек), очищает XML-спавны
    и рассылает уведомления игрокам, чьи заказы были оформлены до рестарта.
    """
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        if await nitrado.clear_event_spawn_orders():
            await db.set_setting("monitor_pending_xml_cleanup", "0")
            orders_to_notify = await db.pop_pending_shop_orders()
            for order in orders_to_notify:
                try:
                    safe_summary = html.escape(str(order.get("items_summary", "")))
                    await bot.send_message(
                        chat_id=order["telegram_id"],
                        text=(
                            "📦 <b>Ваш заказ из магазина доставлен!</b>\n\n"
                            f"🛒 <b>Состав:</b>\n{safe_summary}\n"
                            f"📍 <b>Координаты спавна:</b> <code>X={order['x']} / Z={order['z']}</code>\n\n"
                            "<i>Предметы уже ждут вас на сервере в радиусе 3 м от точки.</i>"
                        ),
                        parse_mode="HTML",
                    )
                    await asyncio.sleep(0.2)
                except Exception as notify_err:
                    logger.warning(
                        "[MONITOR] Не удалось уведомить игрока %s о заказе #%s: %s",
                        order["telegram_id"],
                        order["id"],
                        notify_err,
                    )
        else:
            logger.warning("[MONITOR] Не удалось очистить XML-спавны после рестарта (флаг сохранён для повтора).")
    except asyncio.CancelledError:
        logger.info("[MONITOR] Фоновая обработка заказов после рестарта отменена.")
        raise
    except Exception as e:
        logger.error("[MONITOR] Ошибка при обработке заказов после рестарта: %s", e, exc_info=True)


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

    last_adm_file: str | None = await db.get_setting("monitor_last_adm_file")
    post_restart_tasks: set[asyncio.Task] = set()

    def _schedule_xml_cleanup(tag: str, delay: int = 240) -> None:
        if any(not t.done() for t in post_restart_tasks):
            return
        task = asyncio.create_task(
            _process_post_restart_orders(
                bot=bot,
                db=db,
                nitrado=nitrado,
                delay_seconds=delay,
            ),
            name=f"post_restart_{tag}",
        )
        post_restart_tasks.add(task)
        task.add_done_callback(post_restart_tasks.discard)

    if await db.get_setting("monitor_pending_xml_cleanup") == "1":
        logger.info("[MONITOR] Обнаружена незавершённая очистка XML после перезапуска бота — возобновляем...")
        _schedule_xml_cleanup(tag="recovery", delay=60)

    try:
        while True:
            try:
                server_data = await nitrado.get_server_status()
                now_msk = datetime.now(MSK)
                next_restart_msk = get_next_scheduled_restart_msk(now_msk)
                raid_cfg = await load_raid_config(db)
                wipe_cfg = await load_wipe_config(db)

                if server_data:
                    current_status = str(server_data.get("status", "")).lower()

                    if current_status == "started":
                        current_adm_file = await nitrado.get_latest_adm_filename()
                        if current_adm_file and last_adm_file and current_adm_file != last_adm_file:
                            logger.info(
                                "[MONITOR] Обнаружен новый лог %s (был %s) — рестарт завершён! "
                                "Ставим флаг и запускаем отложенную очистку XML через 240 сек...",
                                current_adm_file,
                                last_adm_file,
                            )
                            last_adm_file = current_adm_file
                            await db.set_setting("monitor_last_adm_file", current_adm_file)
                            await db.set_setting("monitor_pending_xml_cleanup", "1")
                            _schedule_xml_cleanup(tag=current_adm_file, delay=240)

                        elif current_adm_file and not last_adm_file:
                            last_adm_file = current_adm_file
                            await db.set_setting("monitor_last_adm_file", current_adm_file)

                text = format_server_info(server_data, next_restart_msk, raid_cfg, wipe_cfg)

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
                        with contextlib.suppress(TelegramAPIError):
                            await bot.edit_message_text(
                                text=text,
                                chat_id=chat_id,
                                message_id=message_id,
                                parse_mode="HTML",
                            )
                    except TelegramAPIError as e:
                        err_text = str(e).lower()
                        if "message is not modified" in err_text:
                            pass
                        elif (
                            "message to edit not found" in err_text
                            or "message_id_invalid" in err_text
                            or "message can't be edited" in err_text
                        ):
                            logger.warning("[MONITOR] Старое сообщение удалено или недоступно, создаем новое...")
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
                        with contextlib.suppress(TelegramAPIError):
                            await bot.pin_chat_message(
                                chat_id=chat_id,
                                message_id=message_id,
                                disable_notification=True,
                            )
                        try:
                            await db.set_setting("dayz_monitor_message_id", str(message_id))
                            logger.info("[MONITOR] Новый ID сообщения (%s) сохранен в БД.", message_id)
                        except Exception as db_err:
                            logger.error("[MONITOR] Ошибка записи ID сообщения в БД: %s", db_err)
                    except Exception as send_err:
                        logger.error("[MONITOR] Не удалось отправить новое сообщение: %s", send_err)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[MONITOR] Критическая ошибка в цикле: %s", e, exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        logger.info("[MONITOR] Задача мониторинга остановлена.")
        for task in post_restart_tasks:
            task.cancel()
        if post_restart_tasks:
            await asyncio.gather(*post_restart_tasks, return_exceptions=True)
