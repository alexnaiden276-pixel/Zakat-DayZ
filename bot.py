import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config.settings import load_settings
from database.db import Database
from handlers import common, economy, groups, start, support, summary
from middlewares.registration import RegistrationMiddleware
from services.game_api import GameAPI
from services.kill_logging import start_kill_logging
from services.monitoring import start_server_monitoring
from services.nitrado_api import NitradoAPI

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

    # Регистрация middleware (outer отрабатывает до фильтров роутеров)
    dp.message.outer_middleware(RegistrationMiddleware(db))
    dp.callback_query.outer_middleware(RegistrationMiddleware(db))

    # Подключение роутеров (common — в самом конце)
    dp.include_router(start.router)
    dp.include_router(economy.router)
    dp.include_router(groups.router)
    dp.include_router(support.router)
    dp.include_router(summary.router)
    dp.include_router(common.router)

    logger.info("Шаг 4: Запускаем фоновые задачи и поллинг...")

    kill_logging_task = asyncio.create_task(
        start_kill_logging(
            bot=bot,
            db=db,
            nitrado=nitrado_api,
            chat_id=settings.kill_chat_id,
            thread_id=settings.kill_thread_id,
        ),
        name="kill_logging",
    )
    kill_logging_task.add_done_callback(_log_task_exception)

    monitoring_task = asyncio.create_task(
        start_server_monitoring(
            bot=bot,
            db=db,
            nitrado=nitrado_api,
            chat_id=settings.target_chat_id,
            thread_id=settings.target_thread_id,
        ),
        name="server_monitoring",
    )
    monitoring_task.add_done_callback(_log_task_exception)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("Завершение работы: останавливаем фоновые задачи и закрываем соединения...")
        kill_logging_task.cancel()
        monitoring_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(kill_logging_task, monitoring_task)

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
