import asyncio
import html
import logging
import math

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import Settings
from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)


def _in_zone(x: float, z: float, zone) -> bool:
    return math.hypot(x - float(zone["x"]), z - float(zone["z"])) <= float(zone["radius"])


async def _is_exempt_admin(db: Database, settings: Settings, nick: str) -> bool:
    """Проверяет, является ли игрок администратором (таких игроков автобан не трогает)."""
    user = await db.get_user_by_nick(nick)
    if not user:
        return False
    if user["role"] == "admin":
        return True
    return int(user["telegram_id"]) in settings.admin_ids


def _build_unban_markup(nick: str) -> InlineKeyboardMarkup | None:
    """Создаёт кнопку разбана, если callback_data укладывается в лимит Telegram (64 байта)."""
    cb_data = f"ban:unban_direct:{nick}"
    if len(cb_data.encode("utf-8")) > 64:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"✅ Разбанить {nick[:24]}",
            callback_data=cb_data,
        )
    ]])


async def _notify_admins_safe(
    bot: Bot,
    chat_id: int,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Безопасная отправка уведомления админам с обработкой флуд-контроля Telegram."""
    for _ in range(2):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except TelegramRetryAfter as retry_err:
            logger.warning("[AUTOBAN] Флуд-контроль ТГ, ждём %s сек...", retry_err.retry_after)
            await asyncio.sleep(retry_err.retry_after + 0.5)
        except TelegramAPIError as tg_err:
            logger.error("[AUTOBAN] Ошибка Telegram API при отправке уведомления: %s", tg_err)
            return
        except Exception as err:
            logger.error("[AUTOBAN] Непредвиденная ошибка при отправке уведомления: %s", err)
            return


async def start_autoban_service(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    settings: Settings,
    poll_interval: int = 45,
) -> None:
    """Фоновый мониторинг .ADM логов на нарушения правил стройки и PvP в запретных зонах."""
    logger.info("[AUTOBAN] Служба гео-автобанов запущена.")

    last_file = (await db.get_setting("autoban_last_adm_file")) or None
    last_hash = (await db.get_setting("autoban_last_adm_hash")) or None

    while True:
        try:
            zones = await db.get_autoban_zones()
            build_zones = [z for z in zones if z["kind"] == "build"]
            kill_zones = [z for z in zones if z["kind"] == "kill"]

            events, updated_file, updated_hash = await nitrado.get_new_autoban_events(
                last_file_name=last_file,
                last_line_hash=last_hash,
            )

            banned_in_batch: set[str] = set()

            for ev in events:
                if ev["type"] == "build" and build_zones:
                    nick = str(ev["nick"]).strip()
                    px, pz = float(ev["x"]), float(ev["z"])
                    matched = next((z for z in build_zones if _in_zone(px, pz, z)), None)
                    if matched:
                        if await _is_exempt_admin(db, settings, nick):
                            logger.info(
                                "[AUTOBAN] Админ %s строил в зоне '%s' — пропущено.",
                                nick,
                                matched["name"],
                            )
                        elif nick.lower() not in banned_in_batch:
                            banned_in_batch.add(nick.lower())
                            ok = await nitrado.ban_player(nick)
                            logger.warning(
                                "[AUTOBAN] Автобан за стройку: игрок=%s, зона=%s, X=%.1f Z=%.1f, успех=%s",
                                nick,
                                matched["name"],
                                px,
                                pz,
                                ok,
                            )
                            if settings.admin_chat_id:
                                markup = _build_unban_markup(nick)
                                status_str = "⛔ Забанен в Nitrado" if ok else "⚠️ Ошибка выдачи бана через API"
                                hint = "" if markup else "\nℹ️ <i>Ник слишком длинный для кнопки — разбаньте через меню банов.</i>"
                                await _notify_admins_safe(
                                    bot=bot,
                                    chat_id=settings.admin_chat_id,
                                    text=(
                                        f"🚨 <b>АВТОБАН: Запрещённая стройка</b>\n\n"
                                        f"👤 <b>Игрок:</b> <code>{html.escape(nick)}</code>\n"
                                        f"🏗 <b>Объект:</b> <code>{html.escape(str(ev['item']))}</code>\n"
                                        f"📍 <b>Зона:</b> {html.escape(matched['name'])} "
                                        f"(<code>X: {px:.1f} | Z: {pz:.1f}</code>)\n"
                                        f"📊 <b>Статус:</b> {status_str}{hint}"
                                    ),
                                    markup=markup,
                                )

                elif ev["type"] == "kill" and kill_zones:
                    killer = str(ev["killer"]).strip()
                    victim = str(ev["victim"]).strip()
                    if killer.lower() == victim.lower():
                        continue

                    kx, kz = float(ev["kx"]), float(ev["kz"])
                    vx, vz = float(ev["vx"]), float(ev["vz"])
                    matched = next(
                        (z for z in kill_zones if _in_zone(kx, kz, z) or _in_zone(vx, vz, z)),
                        None,
                    )
                    if matched:
                        if await _is_exempt_admin(db, settings, killer):
                            logger.info(
                                "[AUTOBAN] Админ %s убил в зоне '%s' — пропущено.",
                                killer,
                                matched["name"],
                            )
                        elif killer.lower() not in banned_in_batch:
                            banned_in_batch.add(killer.lower())
                            ok = await nitrado.ban_player(killer)
                            logger.warning(
                                "[AUTOBAN] Автобан за убийство: убийца=%s, жертва=%s, зона=%s, успех=%s",
                                killer,
                                victim,
                                matched["name"],
                                ok,
                            )
                            if settings.admin_chat_id:
                                markup = _build_unban_markup(killer)
                                status_str = "⛔ Забанен в Nitrado" if ok else "⚠️ Ошибка выдачи бана через API"
                                hint = "" if markup else "\nℹ️ <i>Ник слишком длинный для кнопки — разбаньте через меню банов.</i>"
                                await _notify_admins_safe(
                                    bot=bot,
                                    chat_id=settings.admin_chat_id,
                                    text=(
                                        f"🚨 <b>АВТОБАН: Убийство в запретной зоне</b>\n\n"
                                        f"👤 <b>Убийца:</b> <code>{html.escape(killer)}</code>\n"
                                        f"⚰️ <b>Жертва:</b> <code>{html.escape(victim)}</code>\n"
                                        f"📍 <b>Зона:</b> {html.escape(matched['name'])} "
                                        f"(<code>X: {vx:.1f} | Z: {vz:.1f}</code>)\n"
                                        f"📊 <b>Статус:</b> {status_str}{hint}"
                                    ),
                                    markup=markup,
                                )

                if ev.get("file_name"):
                    last_file = ev["file_name"]
                    await db.set_setting("autoban_last_adm_file", last_file)
                if ev.get("hash"):
                    last_hash = ev["hash"]
                    await db.set_setting("autoban_last_adm_hash", last_hash)

            if updated_file and (updated_file != last_file or updated_hash != last_hash):
                last_file = updated_file
                await db.set_setting("autoban_last_adm_file", last_file)
                last_hash = updated_hash
                await db.set_setting("autoban_last_adm_hash", last_hash or "")

        except asyncio.CancelledError:
            logger.info("[AUTOBAN] Служба гео-автобанов остановлена.")
            raise
        except Exception as e:
            logger.error("[AUTOBAN] Ошибка в цикле гео-автобанов: %s", e, exc_info=True)

        await asyncio.sleep(poll_interval)
