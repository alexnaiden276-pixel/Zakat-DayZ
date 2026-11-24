import asyncio
import logging

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)

# Интервал начисления зарплаты за онлайн: 1 час (3600 секунд)
SALARY_INTERVAL_SECONDS = 3600


async def start_online_salary(
    db: Database,
    nitrado: NitradoAPI,
    poll_interval: int = SALARY_INTERVAL_SECONDS,
) -> None:
    """
    Фоновая служба начисления зарплаты игрокам, находящимся онлайн на сервере DayZ.
    Срабатывает раз в час и начисляет сумму eco_online_reward из настроек экономики.
    """
    logger.info(
        "[ONLINE_SALARY] Служба почасовой зарплаты запущена (интервал: %d сек).",
        poll_interval,
    )

    while True:
        try:
            # Ждём 1 час перед очередным начислением
            await asyncio.sleep(poll_interval)
            eco = await db.get_economy_config()
            hourly_reward = int(eco.get("eco_online_reward", 60))

            if hourly_reward <= 0:
                logger.debug("[ONLINE_SALARY] Почасовая награда равна 0 — начисление пропущено.")
                continue

            _, online_nicks = await nitrado.get_online_players()
            if not online_nicks:
                logger.debug("[ONLINE_SALARY] На сервере нет игроков онлайн.")
                continue

            rewarded_count = await db.reward_online_players(
                nicks=online_nicks,
                amount=hourly_reward,
            )
            logger.info(
                "[ONLINE_SALARY] Начислено по %d 🪙 %d зарегистрированным игрокам (всего онлайн: %d).",
                hourly_reward,
                rewarded_count,
                len(online_nicks),
            )

        except asyncio.CancelledError:
            logger.info("[ONLINE_SALARY] Служба почасовой зарплаты остановлена.")
            raise
        except Exception as e:
            logger.error("[ONLINE_SALARY] Ошибка при начислении зарплаты за онлайн: %s", e, exc_info=True)
