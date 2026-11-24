import asyncio
import logging

from database.db import Database
from services.hotzone_service import HotzoneService
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)

# Интервал начисления зарплаты за онлайн: 15 минут (900 секунд)
SALARY_INTERVAL_SECONDS = 900


async def start_online_salary(
    db: Database,
    nitrado: NitradoAPI,
    hotzone_service: HotzoneService | None = None,
    poll_interval: int = SALARY_INTERVAL_SECONDS,
) -> None:
    """
    Фоновая служба начисления зарплаты игрокам, находящимся онлайн на сервере DayZ.
    Срабатывает каждые 15 минут и начисляет eco_online_reward (с множителем x3 в Опасной зоне).
    """
    logger.info(
        "[ONLINE_SALARY] Служба зарплаты за онлайн запущена (интервал: %d сек).",
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
            inside_hotzone_nicks: set[str] = set()
            zone_mult = 1
            if hotzone_service is not None and hotzone_service.active_zone is not None:
                hz = hotzone_service.active_zone
                if hz.get("status") == "active":
                    inside_hotzone_nicks = await nitrado.get_players_in_sell_zone(
                        center_x=float(hz["x"]),
                        center_z=float(hz["z"]),
                        radius=float(hz["radius"]),
                    )
                    zone_mult = int(round(float(hz.get("salary_mult", 3))))

            rewards_map: dict[str, int] = {}
            boosted_players = 0

            for nick in online_nicks:
                if nick.strip().lower() in inside_hotzone_nicks and zone_mult > 1:
                    rewards_map[nick] = base_reward * zone_mult
                    boosted_players += 1
                else:
                    rewards_map[nick] = base_reward

            rewarded_count = await db.reward_online_players_map(rewards_map)
            logger.info(
                "[ONLINE_SALARY] Начислена зарплата %d зарегистрированным игрокам (база: %d 🪙, в опасной зоне x%d: %d, всего онлайн: %d).",
                rewarded_count,
                base_reward,
                zone_mult,
                boosted_players,
                len(online_nicks),
            )

        except asyncio.CancelledError:
            logger.info("[ONLINE_SALARY] Служба зарплаты за онлайн остановлена.")
            raise
        except Exception as e:
            logger.error("[ONLINE_SALARY] Ошибка при начислении зарплаты за онлайн: %s", e, exc_info=True)
