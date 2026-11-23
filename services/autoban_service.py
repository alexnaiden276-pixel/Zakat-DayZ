
import asyncio
import html
import logging
import math

from aiogram import Bot
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

            for ev in events:
                if ev["type"] == "build" and build_zones:
                    nick = ev["nick"]
                    px, pz = ev["x"], ev["z"]
                    matched = next((z for z in build_zones if _in_zone(px, pz, z)), None)
                    if matched:
                        if await _is_exempt_admin(db, settings, nick):
                            logger.info(
                                "[AUTOBAN] Админ %s строил в зоне '%s' — пропущено.",
                                nick,
                                matched["name"],
                            )
                        else:
                            ban_target = nick
                            ok = await nitrado.ban_player(ban_target)
                            logger.warning(
                                "[AUTOBAN] Автобан за стройку: игрок=%s, зона=%s, X=%.1f Z=%.1f, успех=%s",
                                nick,
                                matched["name"],
                                px,
                                pz,
                                ok,
                            )
                            if settings.admin_chat_id:
                                markup = InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(
                                        text=f"✅ Разбанить {nick}",
                                        callback_data=f"ban:unban_direct:{ban_target}",
                                    )
                                ]])
                                status_str = "⛔ Забанен в Nitrado" if ok else "⚠️ Ошибка выдачи бана через API"
                                await bot.send_message(
                                    chat_id=settings.admin_chat_id,
                                    text=(
                                        f"🚨 <b>АВТОБАН: Запрещённая стройка</b>\n\n"
                                        f"👤 <b>Игрок:</b> <code>{html.escape(nick)}</code>\n"
                                        f"🏗 <b>Объект:</b> <code>{html.escape(ev['item'])}</code>\n"
                                        f"📍 <b>Зона:</b> {html.escape(matched['name'])} "
                                        f"(<code>X: {px:.1f} | Z: {pz:.1f}</code>)\n"
                                        f"📊 <b>Статус:</b> {status_str}"
                                    ),
                                    reply_markup=markup,
                                    parse_mode="HTML",
                                )

                elif ev["type"] == "kill" and kill_zones:
                    killer = ev["killer"]
                    victim = ev["victim"]
                    if killer.lower() == victim.lower():
                        continue

                    kx, kz = ev["kx"], ev["kz"]
                    vx, vz = ev["vx"], ev["vz"]
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
                        else:
                            ban_target = killer
                            ok = await nitrado.ban_player(ban_target)
                            logger.warning(
                                "[AUTOBAN] Автобан за убийство: убийца=%s, жертва=%s, зона=%s, успех=%s",
                                killer,
                                victim,
                                matched["name"],
                                ok,
                            )
                            if settings.admin_chat_id:
                                markup = InlineKeyboardMarkup(inline_keyboard=[[
                                    InlineKeyboardButton(
                                        text=f"✅ Разбанить {killer}",
                                        callback_data=f"ban:unban_direct:{ban_target}",
                                    )
                                ]])
                                status_str = "⛔ Забанен в Nitrado" if ok else "⚠️ Ошибка выдачи бана через API"
                                await bot.send_message(
                                    chat_id=settings.admin_chat_id,
                                    text=(
                                        f"🚨 <b>АВТОБАН: Убийство в запретной зоне</b>\n\n"
                                        f"👤 <b>Убийца:</b> <code>{html.escape(killer)}</code>\n"
                                        f"⚰️ <b>Жертва:</b> <code>{html.escape(victim)}</code>\n"
                                        f"📍 <b>Зона:</b> {html.escape(matched['name'])} "
                                        f"(<code>X: {vx:.1f} | Z: {vz:.1f}</code>)\n"
                                        f"📊 <b>Статус:</b> {status_str}"
                                    ),
                                    reply_markup=markup,
                                    parse_mode="HTML",
                                )

                if ev.get("file_name"):
                    last_file = ev["file_name"]
                    await db.set_setting("autoban_last_adm_file", last_file)
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
