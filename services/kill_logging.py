import asyncio
import logging
from datetime import datetime, timezone, timedelta
from aiogram import Bot
from services.nitrado_api import NitradoAPI
from database.db import Database

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))

async def start_kill_logging(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    chat_id: int,
    thread_id: int | None = None
) -> None:
    """Фоновое отслеживание убийств по логам DayZ и вывод в отдельную тему."""
    logger.info("[KILL_LOG] Задача логирования убийств запущена.")
    
    # Пытаемся инициализировать время последней записи, чтобы не спамить старыми киллами при рестарте бота
    last_processed_time = await db.get_setting("last_kill_log_time")
    
    # Если бот запущен впервые, за отправную точку берем текущее время на момент старта
    if not last_processed_time:
        last_processed_time = datetime.now(MSK).strftime("%H:%M:%S")

    while True:
        try:
            # Запрашиваем новые убийства
            new_kills, updated_time = await nitrado.get_new_kills(last_processed_time)
            
            if new_kills:
                server_status = await nitrado.get_server_status()
                # Получаем динамическое название сервера из настроек Nitrado (или дефолт)
                server_name = server_status.get("query", {}).get("server_name", "RUSSVET") if server_status else "RUSSVET"
                today_date = datetime.now(MSK).strftime("%Y-%m-%d")

                for kill in new_kills:
                    # Форматируем сообщение по вашему примеру
                    message_text = (
                        f"🎮 <b>{server_name}</b>\n"
                        f"Произошло убийство 💀\n"
                        f"<code>{kill['time']} {today_date} МСК</code>\n\n"
                        f"👤 <b>Игрок:</b> <code>{kill['killer']}</code>\n"
                        f"⚰️ <b>Убил игрока:</b> <code>{kill['victim']}</code>\n"
                        f"🔫 из <b>{kill['weapon']}</b> с <code>{kill['distance']} м</code>"
                    )
                    
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                            text=message_text,
                            parse_mode="HTML"
                        )
                        # Автоматически обновляем статистику убийств/смертей в вашей СУБД SQLite
                        await db.execute(
                            "UPDATE users SET kills = kills + 1 WHERE game_nick = ? COLLATE NOCASE", 
                            (kill['killer'],)
                        )
                        await db.execute(
                            "UPDATE users SET deaths = deaths + 1 WHERE game_nick = ? COLLATE NOCASE", 
                            (kill['victim'],)
                        )
                        # Обновляем дистанцию, если это новый рекорд игрока
                        await db.execute(
                            "UPDATE users SET max_kill_distance = ? WHERE game_nick = ? COLLATE NOCASE AND max_kill_distance < ?",
                            (float(kill['distance']), kill['killer'], float(kill['distance']))
                        )
                    except Exception as tg_err:
                        logger.error("Не удалось отправить сообщение килл-лога в Telegram: %s", tg_err)
                    
                    # Защита от флуда Telegram при массовых ПВП замесах
                    await asyncio.sleep(1)

                # Сохраняем временную метку последней обработанной строки в базу данных
                if updated_time:
                    last_processed_time = updated_time
                    await db.set_setting("last_kill_log_time", last_processed_time)

        except asyncio.CancelledError:
            logger.info("[KILL_LOG] Фоновая задача остановлена.")
            raise
        except Exception as e:
            logger.error("[KILL_LOG] Ошибка в цикле проверки убийств: %s", e, exc_info=True)

        # Оптимальный интервал проверки для Nitrado FTP — раз в 45 секунд
        await asyncio.sleep(45)
