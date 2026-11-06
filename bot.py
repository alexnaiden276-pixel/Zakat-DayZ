import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config.settings import load_settings
from database.db import Database
from handlers import common, economy, groups, start, support, summary
from middlewares.registration import RegistrationMiddleware
from services.game_api import GameAPI
from services.monitoring import start_server_monitoring
from services.nitrado_api import NitradoAPI


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    db = Database(settings.db_path)
    await db.init()

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    nitrado_api = NitradoAPI(
        token=settings.nitrado_token,
        server_id=settings.nitrado_server_id,
    )

    # Передача зависимостей в хэндлеры
    dp["db"] = db
    dp["settings"] = settings
    dp["game_api"] = GameAPI()
    dp["nitrado_api"] = nitrado_api

    # Регистрация middleware
    dp.message.middleware(RegistrationMiddleware(db))
    dp.callback_query.middleware(RegistrationMiddleware(db))

    # Подключение роутеров (common — в самом конце)
    dp.include_router(start.router)
    dp.include_router(economy.router)
    dp.include_router(groups.router)
    dp.include_router(support.router)
    dp.include_router(summary.router)
    dp.include_router(common.router)

      # !!! ДОБАВЬТЕ ЭТУ СТРОКУ ДЛЯ ПРОВЕРКИ !!!
    print("\n[MAIN] >>> ПЫТАЕМСЯ ЗАПУСТИТЬ ТАСКУ МОНИТОРИНГА... <<<")

    # Запуск фонового мониторинга с сохранением ссылки на задачу
    monitoring_task = asyncio.create_task(
        start_server_monitoring(
            bot=bot,
            db=db,
            nitrado=nitrado_api,
            chat_id=settings.target_chat_id,
            thread_id=settings.target_thread_id,
        )
    )

    # !!! ДОБАВЬТЕ ЭТУ СТРОКУ ДЛЯ ПРОВЕРКИ !!!
    print("[MAIN] >>> ТАСКА МОНИТОРИНГА УСПЕШНО СОЗДАНА! Запускаем Polling... <<<\n")

    try:
        await dp.start_polling(bot)
    finally:
        # Корректно останавливаем мониторинг до закрытия сессии бота
        monitoring_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitoring_task

        # Явное закрытие сессий
        await nitrado_api.close()  # <-- Раскомментируйте это
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
