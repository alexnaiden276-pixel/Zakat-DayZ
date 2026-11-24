
import asyncio
import html
import logging
import math
import random
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from database.db import Database, MSK_TZ
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)


def format_hotzone_announcement(
    event: dict[str, Any],
    base_online_reward: int,
    is_immediate: bool = False,
) -> str:
    """Формирует подробное сообщение о запуске Опасной зоны (где, когда и какая награда)."""
    z_name = html.escape(str(event["name"]))
    x = int(round(float(event["x"])))
    z = int(round(float(event["z"])))
    radius = int(round(float(event["radius"])))

    sal_mult = float(event["salary_mult"])
    kill_mult = float(event["kill_mult"])
    boosted_salary = int(round(base_online_reward * sal_mult))

    starts_at = datetime.fromisoformat(event["starts_at"]).strftime("%H:%M")
    ends_at = datetime.fromisoformat(event["ends_at"]).strftime("%H:%M")

    if is_immediate:
        header = "☢️ <b>КРОВАВЫЙ ЧАС НАЧАЛСЯ!</b>\n<i>Аномальная активность зафиксирована прямо сейчас!</i>"
        time_block = f"🕒 <b>Когда:</b> прямо сейчас — до <b>{ends_at} МСК</b>"
    else:
        header = "⚠️ <b>ВНИМАНИЕ! ОПАСНАЯ ЗОНА!</b>\n<i>Приближается выброс аномальной активности!</i>"
        time_block = f"🕒 <b>Когда:</b> с <b>{starts_at}</b> до <b>{ends_at} МСК</b> (успейте занять позиции!)"

    return (
        f"{header}\n\n"
        f"📍 <b>Где:</b> <b>{z_name}</b>\n"
        f"🧭 <b>Координаты:</b> <code>X={x}, Z={z}</code> (радиус <b>{radius} м</b>)\n"
        f"{time_block}\n\n"
        f"🏆 <b>Награды за участие в зоне:</b>\n"
        f"• 💰 <b>Онлайн внутри зоны:</b> <b>x{sal_mult:g}</b> (<b>{boosted_salary} 🪙</b> вместо {base_online_reward} 🪙)\n"
        f"• 🔫 <b>Убийство в зоне:</b> <b>x{kill_mult:g}</b> к базовой награде за цель\n"
        f"• 💀 <b>Штраф за смерть:</b> без изменений"
    )


class HotzoneService:
    def __init__(self, db: Database):
        self.db = db
        self.active_zone: dict[str, Any] | None = None
        self._last_preset_id: int | None = None

    def is_inside(self, x: float, z: float) -> bool:
        """Возвращает True, если координаты (x, z) попадают в активную опасную зону."""
        if not self.active_zone or self.active_zone.get("status") != "active":
            return False
        dx = x - float(self.active_zone["x"])
        dz = z - float(self.active_zone["z"])
        return math.hypot(dx, dz) <= float(self.active_zone["radius"])

    def get_salary_multiplier(self, x: float, z: float) -> float:
        return float(self.active_zone["salary_mult"]) if self.is_inside(x, z) else 1.0

    def get_kill_multiplier(self, x: float, z: float) -> float:
        return float(self.active_zone["kill_mult"]) if self.is_inside(x, z) else 1.0

    async def launch_event(
        self,
        preset_id: int,
        announce_minutes: int = 15,
        duration_minutes: int = 60,
        salary_mult: float = 3.0,
        kill_mult: float = 2.0,
    ) -> dict[str, Any] | None:
        now = datetime.now(MSK_TZ)
        starts_at = now + timedelta(minutes=announce_minutes)
        ends_at = starts_at + timedelta(minutes=duration_minutes)
        status = "active" if announce_minutes <= 0 else "announced"

        row = await self.db.create_hotzone_event(
            preset_id=preset_id,
            status=status,
            salary_mult=salary_mult,
            kill_mult=kill_mult,
            starts_at=starts_at.isoformat(),
            ends_at=ends_at.isoformat(),
        )
        event = dict(row) if row else None
        self._last_preset_id = preset_id
        self.active_zone = event if status == "active" else None
        return event

    async def stop_event(self) -> None:
        self.active_zone = None
        await self.db.finish_all_hotzone_events()


async def start_hotzone_scheduler(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    hotzone_service: HotzoneService,
    chat_id: int,
    thread_id: int | None = None,
) -> None:
    """Фоновый планировщик рандомного и ручного запуска Опасных зон."""
    triggered_slots: set[str] = set()

    while True:
        try:
            now = datetime.now(MSK_TZ)
            row = await db.get_current_hotzone_event()
            current = dict(row) if row else None

            if current:
                starts_at = datetime.fromisoformat(current["starts_at"])
                ends_at = datetime.fromisoformat(current["ends_at"])
                z_name = html.escape(str(current["name"]))

                # 1. Наступило время старта после предварительного анонса
                if current["status"] == "announced" and now >= starts_at:
                    await db.update_hotzone_event_status(current["id"], "active")
                    current["status"] = "active"
                    hotzone_service.active_zone = current
                    await bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        text=(
                            f"☢️ <b>КРОВАВЫЙ ЧАС НАЧАЛСЯ!</b>\n\n"
                            f"📍 Локация: <b>{z_name}</b> (радиус {int(current['radius'])} м)\n"
                            f"💰 Онлайн в зоне: <b>x{current['salary_mult']:g}</b>\n"
                            f"🔫 Награда за убийство в зоне: <b>x{current['kill_mult']:g}</b>\n"
                            f"💀 Штраф за смерть без изменений!\n"
                            f"⏳ Активно до <b>{ends_at.strftime('%H:%M')} МСК</b>"
                        ),
                        parse_mode="HTML",
                    )

                # 2. Ивент завершился по времени
                elif now >= ends_at:
                    await hotzone_service.stop_event()
                    await bot.send_message(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        text=(
                            f"✅ <b>Аномальная активность в районе «{z_name}» завершена.</b>\n"
                            f"Множители начислений отключены."
                        ),
                        parse_mode="HTML",
                    )

                # 3. Синхронизация кэша после рестарта бота
                elif current["status"] == "active":
                    hotzone_service.active_zone = current

            else:
                hotzone_service.active_zone = None
                eco = await db.get_economy_config()

                if int(eco.get("eco_hotzone_enabled", 1)) == 1:
                    announce_min = int(eco.get("eco_hotzone_announce_min", 15))
                    duration_min = int(eco.get("eco_hotzone_duration_min", 60))
                    salary_mult = float(eco.get("eco_hotzone_salary_mult", 3))
                    kill_mult = float(eco.get("eco_hotzone_kill_mult", 2))
                    min_online = int(eco.get("eco_hotzone_min_online", 0))
                    prime_hours = await db.get_hotzone_hours()

                    for hour in prime_hours:
                        target_start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                        announce_at = target_start - timedelta(minutes=announce_min)
                        slot_key = target_start.strftime("%Y-%m-%d-%H")

                        if announce_at <= now < target_start and slot_key not in triggered_slots:
                            triggered_slots.add(slot_key)

                            # Проверка условия минимального онлайна на сервере
                            if min_online > 0:
                                status_data = await nitrado.get_server_status()
                                cur_online = int((status_data or {}).get("query", {}).get("player_current") or 0)
                                if cur_online < min_online:
                                    logger.info(
                                        "[HOTZONE] Автозапуск на %02d:00 пропущен: онлайн %d < %d.",
                                        hour,
                                        cur_online,
                                        min_online,
                                    )
                                    continue

                            presets = await db.get_hotzone_presets(only_enabled=True)
                            if not presets:
                                continue

                            pool = [p for p in presets if p["id"] != hotzone_service._last_preset_id] or presets
                            chosen = random.choice(pool)

                            event = await hotzone_service.launch_event(
                                preset_id=chosen["id"],
                                announce_minutes=announce_min,
                                duration_minutes=duration_min,
                                salary_mult=salary_mult,
                                kill_mult=kill_mult,
                            )
                            if event:
                                msg_text = format_hotzone_announcement(
                                    event=event,
                                    base_online_reward=int(eco.get("eco_online_reward", 15)),
                                    is_immediate=False,
                                )
                                await bot.send_message(
                                    chat_id=chat_id,
                                    message_thread_id=thread_id,
                                    text=msg_text,
                                    parse_mode="HTML",
                                )

        except asyncio.CancelledError:
            logger.info("[HOTZONE] Планировщик опасных зон остановлен.")
            raise
        except Exception:
            logger.exception("Ошибка в цикле планировщика опасных зон")

        await asyncio.sleep(15)
