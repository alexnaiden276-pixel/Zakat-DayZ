import asyncio
import logging

from aiogram import Bot

from database.db import Database
from services.hotzone_service import HotzoneService
from services.nitrado_api import NitradoAPI
from utils.ui import notify_balance_change

logger = logging.getLogger(__name__)

# Интервал начисления зарплаты за онлайн: 1 час (3600 секунд)
SALARY_INTERVAL_SECONDS = 3600


async def start_online_salary(
    db: Database,
    nitrado: NitradoAPI,
    hotzone_service: HotzoneService | None = None,
    poll_interval: int = SALARY_INTERVAL_SECONDS,
    bot: Bot | None = None,
) -> None:
    """
    Фоновая служба начисления зарплаты игрокам, находящимся онлайн на сервере DayZ.
    Срабатывает раз в час и начисляет eco_online_reward (с множителем в Опасной зоне).
    """
    logger.info(
        "[ONLINE_SALARY] Служба почасовой зарплаты запущена (интервал: %d сек).",
        poll_interval,
    )

    while True:
        try:
            await asyncio.sleep(poll_interval)
            eco = await db.get_economy_config()
            base_reward = int(eco.get("eco_online_reward", 15))

            if base_reward <= 0:
                logger.debug("[ONLINE_SALARY] Базовая награда за онлайн равна 0 — начисление пропущено.")
                continue

            _, online_nicks = await nitrado.get_online_players()
            if not online_nicks:
                logger.debug("[ONLINE_SALARY] На сервере нет игроков онлайн.")
                continue

            # Если активна Опасная зона — проверяем, кто из игроков находится внутри её радиуса
            inside_hotzone_lower: set[str] = set()
            zone_mult = 1.0
            if hotzone_service is not None and hotzone_service.active_zone is not None:
                hz = hotzone_service.active_zone
                if hz.get("status") == "active":
                    raw_inside = await nitrado.get_players_in_sell_zone(
                        center_x=float(hz["x"]),
                        center_z=float(hz["z"]),
                        radius=float(hz["radius"]),
                    )
                    inside_hotzone_lower = {n.strip().lower() for n in raw_inside}
                    zone_mult = float(hz.get("salary_mult", 3.0))

            rewards_map: dict[str, int] = {}
            boosted_players = 0

            for nick in online_nicks:
                clean_nick = nick.strip()
                if not clean_nick:
                    continue
                if clean_nick.lower() in inside_hotzone_lower and zone_mult > 1.0:
                    rewards_map[clean_nick] = int(round(base_reward * zone_mult))
                    boosted_players += 1
                else:
                    rewards_map[clean_nick] = base_reward

            rewarded_count = await db.reward_online_players_map(rewards_map)
            logger.info(
                "[ONLINE_SALARY] Начислена зарплата %d зарегистрированным игрокам (база: %d 🪙, в опасной зоне x%g: %d, всего онлайн: %d).",
                rewarded_count,
                base_reward,
                zone_mult,
                boosted_players,
                len(online_nicks),
            )

            if rewarded_count > 0 and bot:
                for nick, reward in rewards_map.items():
                    if reward <= 0:
                        continue
                    user = await db.get_user_by_nick(nick)
                    if not user or not user["telegram_id"]:
                        continue

                    is_boosted = nick.strip().lower() in inside_hotzone_lower and zone_mult > 1.0
                    reason = (
                        f"Почасовой онлайн в ☢️ Опасной зоне (множитель x{zone_mult:g})"
                        if is_boosted
                        else "Почасовой онлайн на сервере"
                    )
                    await notify_balance_change(
                        bot=bot,
                        telegram_id=int(user["telegram_id"]),
                        delta=reward,
                        reason=reason,
                        new_balance=int(user["balance"] or 0),
                    )
                    await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            logger.info("[ONLINE_SALARY] Служба почасовой зарплаты остановлена.")
            raise
        except Exception as e:
            logger.error("[ONLINE_SALARY] Ошибка при начислении зарплаты за онлайн: %s", e, exc_info=True)
