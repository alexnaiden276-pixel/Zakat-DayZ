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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print("[DEBUG] Шаг 1: Загружаем настройки...", flush=True)
    settings = load_settings()
    print("[DEBUG] Шаг 2: Инициализируем базу данных...", flush=True)
    db = Database(settings.db_path)
    await db.init()
    print("[DEBUG] Шаг 3: База готова. Создаем бота...", flush=True)
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    nitrado_api = NitradoAPI(
        token=settings.nitrado_token,
        server_id=settings.nitrado_server_id,
        ftp_host=os.getenv("NITRADO_FTP_HOST"),
        ftp_user=os.getenv("NITRADO_FTP_USER"),
        ftp_pass=os.getenv("NITRADO_FTP_PASS"),
    )

    dp.workflow_data.update({
        "db": db,
        "settings": settings,
        "game_api": GameAPI(),
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

    print("[DEBUG] Шаг 4: Запускаем фоновые задачи и поллинг...", flush=True)

    # Запуск фонового логирования убийств
    kill_logging_task = asyncio.create_task(
        start_kill_logging(
            bot=bot,
            db=db,
            nitrado=nitrado_api,
            chat_id=settings.kill_chat_id,
            thread_id=settings.kill_thread_id,
        )
    )

    # Запуск фонового мониторинга сервера (проверьте сигнатуру в services/monitoring.py)
    monitoring_task = asyncio.create_task(
        start_server_monitoring(
            bot=bot,
            db=db,
            nitrado=nitrado_api,
            chat_id=settings.target_chat_id,
            thread_id=settings.target_thread_id,
        )
    )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        print("[DEBUG] Завершение работы, отменяем фоновые задачи...", flush=True)
        kill_logging_task.cancel()
        monitoring_task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(kill_logging_task, monitoring_task)

        await bot.session.close()
        if hasattr(db, "close"):
            await db.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
