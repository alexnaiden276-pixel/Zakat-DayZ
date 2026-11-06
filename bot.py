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
     print("[DEBUG] Шаг 4: Запускаем мониторинг...", flush=True)
    
    # Пытаемся получить сохраненный ID сообщения из базы данных
    try:
        raw_msg_id = await db.get_setting("dayz_monitor_message_id")
        initial_message_id = int(raw_msg_id) if raw_msg_id else None
        print(f"[MAIN] Найдено существующее сообщение мониторинга с ID: {initial_message_id}", flush=True)
    except Exception as e:
        print(f"[MAIN] Запись мониторинга не найдена в БД, создаем заново. (Инфо: {e})", flush=True)
        initial_message_id = None

    # Передаем initial_message_id в таску
    monitoring_task = asyncio.create_task(
        start_server_monitoring(
            bot=bot,
            db=db,
            nitrado=nitrado_api,
            chat_id=settings.target_chat_id,
            thread_id=settings.target_thread_id,
            initial_message_id=initial_message_id,
        )
    )

    print("[MAIN] >>> ТАСКА МОНИТОРИНГА УСПЕШНО СОЗДАНА! Запускаем Polling... <<<\n", flush=True)

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
