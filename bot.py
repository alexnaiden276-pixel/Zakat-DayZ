import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config.settings import load_settings
from database.db import Database
from handlers import common, economy, groups, start, support, summary
from middlewares.registration import RegistrationMiddleware
from services.game_api import GameAPI


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    db = Database(settings.db_path)
    await db.init()

    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    # Инициализируем Nitrado API
    nitrado_api = NitradoAPI(token=settings.nitrado_token, server_id=settings.nitrado_server_id)

    # Зависимости доступны в обработчиках через параметры по имени.
    dp["db"] = db
    dp["settings"] = settings
    dp["game_api"] = GameAPI()
    # --- ЗАПУСК МОНИТОРИНГА ---
    # Запускаем фоновую асинхронную задачу
    asyncio.create_task(
        start_server_monitoring(
            bot=bot,
            db=db,
            nitrado=nitrado_api,
            chat_id=settings.target_chat_id,     # ID вашей группы (например, -100123456789)
            thread_id=settings.target_thread_id   # ID темы (топика) внутри этой группы
        )
    )

    dp.message.middleware(RegistrationMiddleware(db))
    dp.callback_query.middleware(RegistrationMiddleware(db))

    dp.include_router(start.router)
    dp.include_router(common.router)
    dp.include_router(economy.router)
    dp.include_router(groups.router)
    dp.include_router(support.router)
    dp.include_router(summary.router)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
