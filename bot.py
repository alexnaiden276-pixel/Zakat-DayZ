import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault, MenuButtonCommands

from config.settings import load_settings
from database.db import Database
from handlers import ban_admin, common, economy, groups, raid_admin, start, support, summary
from middlewares.registration import RegistrationMiddleware
from services.autoban_service import start_autoban_service
from services.game_api import GameAPI
from services.kill_logging import start_kill_logging
from services.leaderboard import start_leaderboard_service
from services.monitoring import start_server_monitoring
from services.nitrado_api import NitradoAPI
from services.online_salary import start_online_salary
from services.raid_scheduler import start_raid_scheduler

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Логирует необработанное исключение, если фоновая задача завершилась аварийно."""
    if not task.cancelled():
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Фоновая задача '%s' завершилась с критической ошибкой:",
                task.get_name(),
                exc_info=exc,
            )


async def setup_start_button(bot: Bot) -> None:
    """Оставляет в меню строки ввода только команду /start."""
    await bot.set_my_commands(
        commands=[BotCommand(command="start", description="Запустить / Главное меню")],
        scope=BotCommandScopeDefault(),
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Шаг 1: Загружаем настройки...")
    settings = load_settings()

    logger.info("Шаг 2: Инициализируем базу данных...")
    db = Database(settings.db_path)
    await db.init()

    logger.info("Шаг 3: База готова. Создаем бота и API-клиенты...")
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    game_api = GameAPI()
    nitrado_api = NitradoAPI(
        token=settings.nitrado_token,
        server_id=settings.nitrado_server_id,
        ftp_host=getattr(settings, "nitrado_ftp_host", None) or os.getenv("NITRADO_FTP_HOST"),
        ftp_user=getattr(settings, "nitrado_ftp_user", None) or os.getenv("NITRADO_FTP_USER"),
        ftp_pass=getattr(settings, "nitrado_ftp_pass", None) or os.getenv("NITRADO_FTP_PASS"),
    )

    dp.workflow_data.update({
        "db": db,
        "settings": settings,
        "game_api": game_api,
        "nitrado_api": nitrado_api,
    })

    dp.message.outer_middleware(RegistrationMiddleware(db))
    dp.callback_query.outer_middleware(RegistrationMiddleware(db))

    dp.include_router(start.router)
    dp.include_router(raid_admin.router)
    dp.include_router(ban_admin.router)
    dp.include_router(economy.router)
    dp.include_router(groups.router)
    dp.include_router(support.router)
    dp.include_router(summary.router)
    dp.include_router(common.router)

    logger.info("Шаг 4: Запускаем фоновые задачи и поллинг...")
    background_tasks = [
        asyncio.create_task(
            start_kill_logging(
                bot=bot,
                db=db,
                nitrado=nitrado_api,
                chat_id=settings.kill_chat_id,
                thread_id=settings.kill_thread_id,
            ),
            name="kill_logging",
        ),
        asyncio.create_task(
            start_server_monitoring(
                bot=bot,
                db=db,
                nitrado=nitrado_api,
                chat_id=settings.target_chat_id,
                thread_id=settings.target_thread_id,
            ),
            name="server_monitoring",
        ),
        asyncio.create_task(
            start_leaderboard_service(
                bot=bot,
                db=db,
                chat_id=settings.leaderboard_chat_id,
                thread_id=settings.leaderboard_thread_id,
            ),
            name="leaderboard",
        ),
        asyncio.create_task(
            start_raid_scheduler(
                bot=bot,
                db=db,
                nitrado=nitrado_api,
                admin_chat_id=settings.admin_chat_id,
                public_chat_id=settings.raid_chat_id,
                public_thread_id=settings.raid_thread_id,
            ),
            name="raid_scheduler",
        ),
        asyncio.create_task(
            start_autoban_service(
                bot=bot,
                db=db,
                nitrado=nitrado_api,
                settings=settings,
            ),
            name="autoban_service",
        ),
        asyncio.create_task(
            start_online_salary(
                db=db,
                nitrado=nitrado_api,
            ),
            name="online_salary",
        ),
    ]
    for task in background_tasks:
        task.add_done_callback(_log_task_exception)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await setup_start_button(bot)
        await dp.start_polling(bot)
    finally:
        logger.info("Завершение работы: останавливаем фоновые задачи и закрываем соединения...")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)

        await nitrado_api.close()
        if hasattr(game_api, "close"):
            await game_api.close()
        await bot.session.close()
        if hasattr(db, "close"):
            await db.close()
        logger.info("Бот корректно остановлен.")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
