import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config.settings import load_settings
from database.db import Database
from handlers import common, economy, groups, start, support, summary,dayz
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

    # Зависимости доступны в обработчиках через параметры по имени.
    dp["db"] = db
    dp["settings"] = settings
    dp["game_api"] = GameAPI()

    dp.message.middleware(RegistrationMiddleware(db))
    dp.callback_query.middleware(RegistrationMiddleware(db))

    dp.include_router(start.router)
    dp.include_router(common.router)
    dp.include_router(economy.router)
    dp.include_router(groups.router)
    dp.include_router(support.router)
    dp.include_router(summary.router)
    dp.include_router(dayz.router)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
