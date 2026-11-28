import asyncio
from collections.abc import Callable, Coroutine
import contextlib
import inspect
import logging
import os
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands

from config.settings import load_settings
from database.db import Database
from handlers import (
    ban_admin,
    common,
    economy,
    economy_admin,
    groups,
    hotzone_admin,
    raid_admin,
    start,
    summary,
    support,
    wipe_admin,
)
from middlewares.registration import RegistrationMiddleware
from services.autoban_service import start_autoban_service
from services.hotzone_service import HotzoneService, start_hotzone_scheduler
from services.kill_logging import start_kill_logging
from services.leaderboard import start_leaderboard_service
from services.monitoring import start_server_monitoring
from services.nitrado_api import NitradoAPI
from services.online_salary import start_online_salary
from services.raid_scheduler import start_raid_scheduler
from services.terrain_map import TerrainHeightMap
from services.wipe_scheduler import start_wipe_scheduler

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


async def _run_with_restart(
    name: str,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    restart_delay: float = 5.0,
) -> None:
    """Запускает фоновый сервис и перезапускает его при неожиданном падении."""
    while True:
        try:
            await coro_factory()
            logger.warning(
                "Фоновый сервис '%s' завершился штатно, перезапуск через %.1f сек...",
                name,
                restart_delay,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Сбой в фоновом сервисе '%s', перезапуск через %.1f сек...",
                name,
                restart_delay,
            )
        await asyncio.sleep(restart_delay)


async def setup_start_button(bot: Bot) -> None:
    """Показывает команду /start только в личных сообщениях с ботом (не в группах)."""
    await bot.delete_my_commands()
    await bot.set_my_commands(
        commands=[BotCommand(command="start", description="Запустить / Главное меню")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def _safe_close(resource: object, name: str) -> None:
    """Безопасно закрывает соединение/ресурс, не прерывая очистку остальных."""
    if resource is None:
        return
    close_method = getattr(resource, "close", None)
    if callable(close_method):
        try:
            if inspect.iscoroutinefunction(close_method):
                await close_method()
            else:
                close_method()
        except Exception as err:
            logger.warning("Ошибка при закрытии %s: %s", name, err)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db: Database | None = None
    bot: Bot | None = None
    nitrado_api: NitradoAPI | None = None
    background_tasks: list[asyncio.Task] = []

    try:
        logger.info("Шаг 1: Загружаем настройки...")
        settings = load_settings()

        logger.info("Шаг 2: Инициализируем базу данных...")
        db = Database(settings.db_path)
        await db.init()

        logger.info("Шаг 3: База готова. Создаем бота, сервисы и API-клиенты...")
        bot = Bot(token=settings.bot_token)
        dp = Dispatcher(storage=MemoryStorage())

        nitrado_api = NitradoAPI(
            token=settings.nitrado_token,
            server_id=settings.nitrado_server_id,
            ftp_host=getattr(settings, "nitrado_ftp_host", None)
            or os.getenv("NITRADO_FTP_HOST"),
            ftp_user=getattr(settings, "nitrado_ftp_user", None)
            or os.getenv("NITRADO_FTP_USER"),
            ftp_pass=getattr(settings, "nitrado_ftp_pass", None)
            or os.getenv("NITRADO_FTP_PASS"),
        )

        hotzone_service = HotzoneService(db)
        terrain_map = TerrainHeightMap()
        await asyncio.to_thread(terrain_map.load)

        dp.workflow_data.update({
            "db": db,
            "settings": settings,
            "nitrado_api": nitrado_api,
            "hotzone_service": hotzone_service,
            "terrain_map": terrain_map,
        })

        # Игнорируем все команды и нажатия кнопок из групп (кроме ответов админов в support)
        private_routers = (
            start.router,
            raid_admin.router,
            ban_admin.router,
            wipe_admin.router,
            hotzone_admin.router,
            economy_admin.router,
            economy.router,
            groups.router,
            summary.router,
            common.router,
        )
        for r in private_routers:
            r.message.filter(F.chat.type == "private")
            r.callback_query.filter(F.message & (F.message.chat.type == "private"))

        support.router.message.filter(
            (F.chat.type == "private") | (F.chat.id == settings.admin_chat_id)
        )
        support.router.callback_query.filter(
            F.message
            & ((F.message.chat.type == "private") | (F.message.chat.id == settings.admin_chat_id))
        )

        # Обычный middleware сработает только после прохождения фильтров роутера
        dp.message.middleware(RegistrationMiddleware(db))
        dp.callback_query.middleware(RegistrationMiddleware(db))

        dp.include_router(start.router)
        dp.include_router(raid_admin.router)
        dp.include_router(ban_admin.router)
        dp.include_router(wipe_admin.router)
        dp.include_router(hotzone_admin.router)
        dp.include_router(economy_admin.router)
        dp.include_router(economy.router)
        dp.include_router(groups.router)
        dp.include_router(support.router)
        dp.include_router(summary.router)
        dp.include_router(common.router)
        logger.info("Шаг 4: Проверяем соединение с Telegram, меню и незавершённые бэкапы...")
        await nitrado_api.restore_pending_events_backup()

        for attempt in range(1, 4):
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                await setup_start_button(bot)
                break
            except TelegramNetworkError as net_err:
                logger.warning(
                    "Сетевой сбой Telegram при старте (попытка %d/3): %s",
                    attempt,
                    net_err,
                )
                if attempt == 3:
                    logger.warning(
                        "Пропускаем предварительную настройку команд и запускаем поллинг..."
                    )
                else:
                    await asyncio.sleep(3)

        logger.info("Шаг 5: Запускаем фоновые задачи и поллинг...")
        services: list[tuple[str, Callable[[], Coroutine[Any, Any, Any]]]] = [
            (
                "kill_logging",
                lambda: start_kill_logging(
                    bot=bot,
                    db=db,
                    nitrado=nitrado_api,
                    chat_id=settings.kill_chat_id,
                    thread_id=settings.kill_thread_id,
                    hotzone_service=hotzone_service,
                ),
            ),
            (
                "server_monitoring",
                lambda: start_server_monitoring(
                    bot=bot,
                    db=db,
                    nitrado=nitrado_api,
                    chat_id=settings.target_chat_id,
                    thread_id=settings.target_thread_id,
                ),
            ),
            (
                "leaderboard",
                lambda: start_leaderboard_service(
                    bot=bot,
                    db=db,
                    chat_id=settings.leaderboard_chat_id,
                    thread_id=settings.leaderboard_thread_id,
                ),
            ),
            (
                "raid_scheduler",
                lambda: start_raid_scheduler(
                    bot=bot,
                    db=db,
                    nitrado=nitrado_api,
                    admin_chat_id=settings.admin_chat_id,
                    public_chat_id=settings.raid_chat_id,
                    public_thread_id=settings.raid_thread_id,
                ),
            ),
            (
                "wipe_scheduler",
                lambda: start_wipe_scheduler(
                    bot=bot,
                    db=db,
                    nitrado=nitrado_api,
                    admin_chat_id=settings.admin_chat_id,
                    public_chat_id=settings.target_chat_id,
                    public_thread_id=settings.target_thread_id,
                ),
            ),
            (
                "autoban_service",
                lambda: start_autoban_service(
                    bot=bot,
                    db=db,
                    nitrado=nitrado_api,
                    settings=settings,
                ),
            ),
            (
                "online_salary",
                lambda: start_online_salary(
                    bot=bot,
                    db=db,
                    nitrado=nitrado_api,
                    hotzone_service=hotzone_service,
                ),
            ),
            (
                "hotzone_scheduler",
                lambda: start_hotzone_scheduler(
                    bot=bot,
                    db=db,
                    nitrado=nitrado_api,
                    hotzone_service=hotzone_service,
                    chat_id=settings.hotzone_chat_id,
                    thread_id=settings.hotzone_thread_id,
                ),
            ),
        ]

        background_tasks = [
            asyncio.create_task(_run_with_restart(name, factory), name=name)
            for name, factory in services
        ]

        for task in background_tasks:
            task.add_done_callback(_log_task_exception)

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    finally:
        logger.info("Завершение работы: останавливаем фоновые задачи и закрываем соединения...")
        for task in background_tasks:
            task.cancel()

        if background_tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*background_tasks, return_exceptions=True),
                    timeout=10.0,
                )

        await _safe_close(nitrado_api, "nitrado_api")
        if bot is not None:
            await _safe_close(bot.session, "bot.session")
        await _safe_close(db, "db")
        logger.info("Бот корректно остановлен.")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
