import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))

DEFAULT_WIPE_CONFIG = {
    "full_enabled": False,
    "full_interval_days": 30,
    "full_next_ts": 0,      # Unix timestamp следующего полного вайпа (МСК)
    "full_warned_1h": False,
    "car_enabled": False,
    "car_interval_days": 7,
    "car_next_ts": 0,       # Unix timestamp следующего вайпа авто (МСК)
    "car_warned_1h": False,
    "announce_public": True,
}


async def load_wipe_config(db: Database) -> dict:
    raw = await db.get_setting("dayz_wipe_config")
    cfg = dict(DEFAULT_WIPE_CONFIG)
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception as e:
            logger.warning("[WIPE] Не удалось прочитать dayz_wipe_config: %s", e)
    return cfg


async def save_wipe_config(db: Database, cfg: dict) -> None:
    await db.set_setting("dayz_wipe_config", json.dumps(cfg, ensure_ascii=False))


def format_ts_msk(ts: int) -> str:
    if not ts or ts <= 0:
        return "не задано"
    dt = datetime.fromtimestamp(ts, tz=MSK)
    return dt.strftime("%d.%m.%Y в %H:%M МСК")


async def execute_wipe_and_notify(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    wipe_type: str,
    cfg: dict,
    admin_chat_id: int | None,
    public_chat_id: int | None,
    public_thread_id: int | None,
    is_manual: bool = False,
) -> tuple[bool, str]:
    """Выполняет вайп сервера/авто, обнуляет БД при полном вайпе и рассылает уведомления."""
    label = "ПОЛНЫЙ ВАЙП СЕРВЕРА" if wipe_type == "full" else "ВАЙП АВТОМОБИЛЕЙ"
    if admin_chat_id:
        with contextlib.suppress(Exception):
            await bot.send_message(
                admin_chat_id,
                f"⏳ Начинаю <b>{label}</b> ({'вручную' if is_manual else 'по расписанию'})...",
                parse_mode="HTML",
            )

    if cfg.get("announce_public", True) and public_chat_id:
        start_pub_text = (
            "🔥 <b>Внимание! Начинается ПОЛНЫЙ ВАЙП сервера!</b>\n"
            "Сервер перезапускается для полной очистки карты, построек и персонажей."
            if wipe_type == "full"
            else "🚗 <b>Внимание! Начинается ВАЙП АВТОМОБИЛЕЙ!</b>\n"
                 "Сервер перезапускается для очистки старого транспорта и спавна нового."
        )
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=public_chat_id,
                message_thread_id=public_thread_id,
                text=start_pub_text,
                parse_mode="HTML",
            )

    ok, details = await nitrado.wipe_server(wipe_type=wipe_type)

    now_ts = int(datetime.now(MSK).timestamp())
    if ok:
        if wipe_type == "full":
            cfg["full_warned_1h"] = False
            if cfg.get("full_enabled"):
                cfg["full_next_ts"] = now_ts + int(cfg.get("full_interval_days", 30)) * 86400
            # При полном вайпе сдвигаем и таймер вайпа авто, чтобы он не сработал следом
            if cfg.get("car_enabled"):
                cfg["car_next_ts"] = now_ts + int(cfg.get("car_interval_days", 7)) * 86400
                cfg["car_warned_1h"] = False

            # Обнуляем статистику, рейтинг и кошельки всех игроков в БД
            try:
                wiped_users, start_bal = await db.wipe_all_players_data()
                details += (
                    f"\n📊 В БД обнулены статистика, лидерборд и кошельки "
                    f"(начислено по {start_bal} 🪙) у игроков: {wiped_users}."
                )
                logger.info("[WIPE] База данных игроков обнулена (обновлено профилей: %d).", wiped_users)
            except Exception as db_err:
                logger.error("[WIPE] Ошибка при обнулении БД игроков: %s", db_err, exc_info=True)
                details += f"\n⚠️ Внимание: не удалось обнулить БД игроков: {db_err}"

        elif wipe_type == "cars":
            cfg["car_warned_1h"] = False
            if cfg.get("car_enabled"):
                cfg["car_next_ts"] = now_ts + int(cfg.get("car_interval_days", 7)) * 86400

        await save_wipe_config(db, cfg)

        if admin_chat_id:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    admin_chat_id,
                    f"✅ <b>{label} завершён!</b>\n{details}",
                    parse_mode="HTML",
                )

        if cfg.get("announce_public", True) and public_chat_id:
            if wipe_type == "full":
                pub_text = (
                    "✅ <b>ПРОИЗОШЁЛ ПОЛНЫЙ ВАЙП СЕРВЕРА!</b>\n\n"
                    "Карта, постройки, тайники, персонажи, а также <b>игровая валюта и статистика</b> "
                    "полностью обнулены (начислен стартовый бонус).\n"
                    "Сервер запускается — заходите занимать лучшие места!"
                )
            else:
                pub_text = (
                    "✅ <b>ПРОИЗОШЁЛ ВАЙП АВТОМОБИЛЕЙ!</b>\n\n"
                    "Весь транспорт на карте очищен и заспавнился заново на штатных точках. "
                    "Сервер запускается!"
                )
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=public_chat_id,
                    message_thread_id=public_thread_id,
                    text=pub_text,
                    parse_mode="HTML",
                )
    else:
        retry_note = ""
        if not is_manual:
            if wipe_type == "full" and cfg.get("full_enabled"):
                cfg["full_next_ts"] = now_ts + 900
                retry_note = "\n⏳ Следующая автоматическая попытка через 15 минут."
            elif wipe_type == "cars" and cfg.get("car_enabled"):
                cfg["car_next_ts"] = now_ts + 900
                retry_note = "\n⏳ Следующая автоматическая попытка через 15 минут."
            await save_wipe_config(db, cfg)

        if admin_chat_id:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    admin_chat_id,
                    f"❌ <b>Ошибка при выполнении ({label}):</b>\n{details}{retry_note}",
                    parse_mode="HTML",
                )

    return ok, details
async def start_wipe_scheduler(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    admin_chat_id: int | None,
    public_chat_id: int | None,
    public_thread_id: int | None,
) -> None:
    """Фоновая задача проверки расписания вайпов и отправки предупреждений."""
    logger.info("[WIPE] Планировщик автоматических вайпов запущен.")
    while True:
        try:
            cfg = await load_wipe_config(db)
            now_ts = int(datetime.now(MSK).timestamp())
            announce = bool(cfg.get("announce_public", True))
            changed = False

            full_next = int(cfg.get("full_next_ts") or 0)
            car_next = int(cfg.get("car_next_ts") or 0)

            # Предупреждение за 1 час до полного вайпа
            if cfg.get("full_enabled") and full_next > 0:
                if 0 < (full_next - now_ts) <= 3600 and not cfg.get("full_warned_1h"):
                    cfg["full_warned_1h"] = True
                    changed = True
                    if announce and public_chat_id:
                        with contextlib.suppress(Exception):
                            await bot.send_message(
                                chat_id=public_chat_id,
                                message_thread_id=public_thread_id,
                                text=(
                                    "⏳ <b>Внимание! Через 1 час состоится плановый ПОЛНЫЙ ВАЙП сервера!</b>\n"
                                    f"🕒 Время: <code>{format_ts_msk(full_next)}</code>"
                                ),
                                parse_mode="HTML",
                            )

            # Предупреждение за 1 час до вайпа авто
            if cfg.get("car_enabled") and car_next > 0:
                if 0 < (car_next - now_ts) <= 3600 and not cfg.get("car_warned_1h"):
                    cfg["car_warned_1h"] = True
                    changed = True
                    if announce and public_chat_id:
                        with contextlib.suppress(Exception):
                            await bot.send_message(
                                chat_id=public_chat_id,
                                message_thread_id=public_thread_id,
                                text=(
                                    "⏳ <b>Внимание! Через 1 час состоится плановый ВАЙП АВТОМОБИЛЕЙ!</b>\n"
                                    "<i>Заберите ценный лут из багажников заранее.</i>\n"
                                    f"🕒 Время: <code>{format_ts_msk(car_next)}</code>"
                                ),
                                parse_mode="HTML",
                            )

            if changed:
                await save_wipe_config(db, cfg)

            # Полный вайп имеет приоритет над вайпом авто
            if cfg.get("full_enabled") and full_next > 0 and now_ts >= full_next:
                await execute_wipe_and_notify(
                    bot=bot,
                    db=db,
                    nitrado=nitrado,
                    wipe_type="full",
                    cfg=cfg,
                    admin_chat_id=admin_chat_id,
                    public_chat_id=public_chat_id,
                    public_thread_id=public_thread_id,
                    is_manual=False,
                )
            elif cfg.get("car_enabled") and car_next > 0 and now_ts >= car_next:
                await execute_wipe_and_notify(
                    bot=bot,
                    db=db,
                    nitrado=nitrado,
                    wipe_type="cars",
                    cfg=cfg,
                    admin_chat_id=admin_chat_id,
                    public_chat_id=public_chat_id,
                    public_thread_id=public_thread_id,
                    is_manual=False,
                )

        except asyncio.CancelledError:
            logger.info("[WIPE] Планировщик вайпов остановлен.")
            raise
        except Exception as e:
            logger.error("[WIPE] Ошибка в цикле планировщика вайпов: %s", e, exc_info=True)

        await asyncio.sleep(60)
