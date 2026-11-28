import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))

# Чётные часы плановых рестартов по МСК
RESTART_HOURS_MSK = (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)

DAY_NAMES_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

DEFAULT_RAID_CONFIG = {
    "mode": "auto",          # "auto" | "force_on" | "force_off"
    "days": list((5, 6)),    # 0=Пн ... 5=Сб, 6=Вс (по умолчанию Сб и Вс)
    "start_hour": 18,        # Начало рейд-тайма (по МСК, кратно 2 часам)
    "end_hour": 22,          # Конец рейд-тайма (по МСК, кратно 2 часам)
    "announce_public": True, # Отправлять ли анонс в общий чат (target_chat_id)
}

async def load_raid_config(db: Database) -> dict:
    """Загружает конфигурацию рейд-тайма из таблицы settings."""
    raw = await db.get_setting("raid_time_config")
    cfg = dict(DEFAULT_RAID_CONFIG)
    cfg["days"] = list(DEFAULT_RAID_CONFIG["days"])
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cfg.update(parsed)
        except Exception:
            pass
    return cfg

async def save_raid_config(db: Database, cfg: dict) -> None:
    """Сохраняет конфигурацию рейд-тайма в таблицу settings."""
    await db.set_setting("raid_time_config", json.dumps(cfg, ensure_ascii=False))

def is_raid_active_at(dt_msk: datetime, cfg: dict) -> bool:
    """
    Определяет, должны ли быть включены рейды в заданный момент времени dt_msk (МСК).
    Учитывает как дневные окна (18:00–22:00), так и переход через полночь (20:00–02:00).
    """
    mode = cfg.get("mode", "auto")
    if mode == "force_on":
        return True
    if mode == "force_off":
        return False

    days: list[int] = [int(d) for d in cfg.get("days", ())]
    if not days:
        return False

    start_h = int(cfg.get("start_hour", 18))
    end_h = int(cfg.get("end_hour", 22))
    if start_h == end_h:
        return False

    hour = dt_msk.hour
    weekday = dt_msk.weekday()  # 0=Пн ... 6=Вс

    if start_h < end_h:
        return (weekday in days) and (start_h <= hour < end_h)
    else:
        if hour >= start_h:
            return weekday in days
        if hour < end_h:
            prev_day = (weekday - 1) % 7
            return prev_day in days
        return False

def format_days_list(days: list[int]) -> str:
    if not days:
        return "не выбраны"
    sorted_days = sorted(set(int(d) for d in days if 0 <= int(d) <= 6))
    return ", ".join(DAY_NAMES_SHORT[d] for d in sorted_days)

async def apply_raid_state_now(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    target_state: bool,
    cfg: dict,
    admin_chat_id: int | None = None,
    public_chat_id: int | None = None,
    public_thread_id: int | None = None,
    reason: str = "плановое расписание",
) -> bool:
    """Записывает новое состояние рейд-тайма в cfggameplay.json и отправляет уведомления."""
    ok = await nitrado.set_raid_mode(raid_enabled=target_state)
    if not ok:
        if admin_chat_id:
            try:
                await bot.send_message(
                    chat_id=admin_chat_id,
                    text="⚠️ <b>[Рейд-тайм]</b> Не удалось обновить <code>cfggameplay.json</code> на FTP Nitrado!",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return False

    await db.set_setting("raid_applied_file_state", "1" if target_state else "0")

    state_label = (
        "🟢 ВКЛЮЧЕНЫ (урон по стенам и палаткам активен)"
        if target_state
        else "🔴 ВЫКЛЮЧЕНЫ (постройки и палатки защищены)"
    )
    if admin_chat_id:
        try:
            await bot.send_message(
                chat_id=admin_chat_id,
                text=(
                    f"💥 <b>Конфиг рейд-тайма обновлён</b> ({reason})\n"
                    f"Новое состояние: <b>{state_label}</b>\n"
                    f"<i>Изменения вступят в силу на сервере после ближайшего рестарта.</i>"
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

    if cfg.get("announce_public", True) and public_chat_id:
        try:
            if target_state:
                end_h = int(cfg.get("end_hour", 22))
                pub_text = (
                    "🔥 <b>ВНИМАНИЕ, ВЫЖИВШИЕ! РЕЙД-ТАЙМ!</b>\n\n"
                    "После ближайшего рестарта сервера <b>включается урон по постройкам, воротам и палаткам</b>!\n"
                    f"⏳ Окончание рейд-тайма: в <b>{end_h:02d}:00 МСК</b>."
                )
            else:
                pub_text = (
                    "🛡 <b>РЕЙД-ТАЙМ ЗАВЕРШЁН</b>\n\n"
                    "После ближайшего рестарта сервера <b>урон по базам, стенам и палаткам будет отключён</b>."
                )
            await bot.send_message(
                chat_id=public_chat_id,
                message_thread_id=public_thread_id,
                text=pub_text,
                parse_mode="HTML",
            )
        except Exception as pub_err:
            logger.warning("[RAID] Не удалось отправить публичный анонс рейд-тайма: %s", pub_err)

    return True

async def start_raid_scheduler(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    admin_chat_id: int | None = None,
    public_chat_id: int | None = None,
    public_thread_id: int | None = None,
) -> None:
    """
    Фоновая служба управления рейд-таймом.
    За 15 минут до каждого чётного часа рестарта проверяет, каким должен быть режим
    в момент рестарта, и заранее обновляет cfggameplay.json.
    """
    logger.info("[RAID] Фоновый планировщик рейд-тайма запущен.")

    while True:
        try:
            now_msk = datetime.now(MSK)
            cfg = await load_raid_config(db)

            upcoming_dt = now_msk + timedelta(minutes=16)
            desired_state = is_raid_active_at(upcoming_dt, cfg)

            applied_raw = await db.get_setting("raid_applied_file_state")
            applied_state: bool | None = None
            if applied_raw in ("0", "1"):
                applied_state = applied_raw == "1"

            if applied_state is None or applied_state != desired_state:
                logger.info(
                    "[RAID] Подготовка cfggameplay.json к рестарту: текущее=%s -> нужно=%s",
                    applied_state,
                    desired_state,
                )
                await apply_raid_state_now(
                    bot=bot,
                    db=db,
                    nitrado=nitrado,
                    target_state=desired_state,
                    cfg=cfg,
                    admin_chat_id=admin_chat_id,
                    public_chat_id=public_chat_id,
                    public_thread_id=public_thread_id,
                    reason="подготовка к плановому рестарту",
                )

        except asyncio.CancelledError:
            logger.info("[RAID] Планировщик рейд-тайма остановлен.")
            raise
        except Exception as e:
            logger.error("[RAID] Ошибка в цикле планировщика рейд-тайма: %s", e, exc_info=True)

        await asyncio.sleep(60)
