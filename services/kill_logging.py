import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from services.nitrado_api import NitradoAPI
from database.db import Database

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))


async def start_kill_logging(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    chat_id: int,
    thread_id: int | None = None,
    poll_interval: int = 60,
) -> None:
    """Фоновое отслеживание убийств по логам DayZ и вывод в отдельную тему."""
    logger.info("[KILL_LOG] Задача логирования убийств запущена.")

    last_file_name = await db.get_setting("last_kill_log_file")
    last_line_hash = await db.get_setting("last_kill_log_hash")

    while True:
        try:
            new_kills, updated_file, updated_hash = await nitrado.get_new_kills(
                last_file_name=last_file_name,
                last_line_hash=last_line_hash,
            )

            if new_kills:
                server_status = await nitrado.get_server_status()
                raw_server_name = (
                    server_status.get("query", {}).get("server_name", "RUSSVET")
                    if server_status
                    else "RUSSVET"
                )
                server_name = html.escape(raw_server_name)
                today_date = datetime.now(MSK).strftime("%Y-%m-%d")

                for kill in new_kills:
                    killer_safe = html.escape(kill["killer"])
                    victim_safe = html.escape(kill["victim"])
                    weapon_safe = html.escape(kill["weapon"])
                    kill_date = kill.get("date", today_date)
                    is_headshot = bool(kill.get("is_headshot", False))

                    # 1. Обновляем статистику в БД
                    try:
                        dist = float(kill["distance"])
                        await db.execute(
                            "UPDATE users SET kills = COALESCE(kills, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                            (kill["killer"],),
                        )
                        await db.execute(
                            "UPDATE users SET deaths = COALESCE(deaths, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                            (kill["victim"],),
                        )
                        await db.execute(
                            """
                            UPDATE users 
                            SET max_kill_distance = ? 
                            WHERE game_nick = ? COLLATE NOCASE 
                              AND COALESCE(max_kill_distance, 0) < ?
                            """,
                            (dist, kill["killer"], dist),
                        )

                        if is_headshot:
                            await db.execute(
                                "UPDATE users SET headshots = COALESCE(headshots, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                                (kill["killer"],),
                            )
                    except Exception as db_err:
                        logger.error("Ошибка обновления статистики убийства в БД: %s", db_err)

                    # 2. Формируем и отправляем сообщение в Telegram
                    headshot_badge = " 🎯 <b>(В ГОЛОВУ!)</b>" if is_headshot else ""
                    message_text = (
                        f"🎮 <b>{server_name}</b>\n"
                        f"Произошло убийство 💀{headshot_badge}\n"
                        f"<code>{kill['time']} {kill_date}</code>\n\n"
                        f"👤 <b>Игрок:</b> <code>{killer_safe}</code>\n"
                        f"⚰️ <b>Убил игрока:</b> <code>{victim_safe}</code>\n"
                        f"🔫 из <b>{weapon_safe}</b> с <code>{kill['distance']} м</code>"
                    )

                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                            text=message_text,
                            parse_mode="HTML",
                        )
                    except TelegramRetryAfter as retry_err:
                        logger.warning("Флуд-контроль ТГ, ждем %s сек...", retry_err.retry_after)
                        await asyncio.sleep(retry_err.retry_after)
                        await bot.send_message(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                            text=message_text,
                            parse_mode="HTML",
                        )
                    except Exception as tg_err:
                        logger.error("Не удалось отправить сообщение килл-лога в Telegram: %s", tg_err)

                    # Промежуточное сохранение прогресса
                    last_file_name = updated_file
                    last_line_hash = kill["hash"]
                    await db.set_setting("last_kill_log_file", last_file_name)
                    await db.set_setting("last_kill_log_hash", last_line_hash)

                    await asyncio.sleep(3)

            # Фиксируем последний хэш файла
            if updated_file and (updated_file != last_file_name or updated_hash != last_line_hash):
                last_file_name = updated_file
                last_line_hash = updated_hash
                await db.set_setting("last_kill_log_file", last_file_name)
                if last_line_hash:
                    await db.set_setting("last_kill_log_hash", last_line_hash)

        except asyncio.CancelledError:
            logger.info("[KILL_LOG] Фоновая задача остановлена.")
            raise
        except Exception as e:
            logger.error("[KILL_LOG] Ошибка в цикле проверки убийств: %s", e, exc_info=True)

        await asyncio.sleep(poll_interval)
