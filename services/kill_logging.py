import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
SERVER_NAME_CACHE_TTL = 1800  # Кэшируем имя сервера на 30 минут


async def _send_telegram_safe(
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    text: str,
) -> bool:
    """Безопасная отправка сообщения с обработкой флуд-контроля."""
    for attempt in range(2):
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
            )
            return True
        except TelegramRetryAfter as retry_err:
            logger.warning("[KILL_LOG] Флуд-контроль ТГ, ждём %s сек...", retry_err.retry_after)
            await asyncio.sleep(retry_err.retry_after + 0.5)
        except TelegramAPIError as tg_err:
            logger.error("[KILL_LOG] Ошибка Telegram API при отправке килл-лога: %s", tg_err)
            return False
        except Exception as err:
            logger.error("[KILL_LOG] Непредвиденная ошибка при отправке килл-лога: %s", err)
            return False
    return False


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

    cached_server_name = "RUSSVET"
    last_name_fetch = 0.0

    while True:
        try:
            new_kills, updated_file, updated_hash = await nitrado.get_new_kills(
                last_file_name=last_file_name,
                last_line_hash=last_line_hash,
            )

            if new_kills:
                # Обновляем название сервера не чаще раза в 30 минут
                now_mono = time.monotonic()
                if now_mono - last_name_fetch > SERVER_NAME_CACHE_TTL:
                    server_status = await nitrado.get_server_status()
                    if server_status:
                        raw_name = (
                            server_status.get("query", {}).get("server_name")
                            or cached_server_name
                        )
                        cached_server_name = html.escape(str(raw_name))
                        last_name_fetch = now_mono

                today_date = datetime.now(MSK).strftime("%Y-%m-%d")

                for kill in new_kills:
                    killer_raw = str(kill["killer"])
                    victim_raw = str(kill["victim"])
                    killer_safe = html.escape(killer_raw)
                    victim_safe = html.escape(victim_raw)
                    weapon_safe = html.escape(str(kill["weapon"]))
                    kill_date = kill.get("date", today_date)
                    is_headshot = bool(kill.get("is_headshot", False))

                    # 1. Обновляем статистику в БД (игнорируем самоубийства в статистике убийств)
                    try:
                        dist = float(kill.get("distance", 0))
                        if killer_raw.lower() != victim_raw.lower():
                            await db.execute(
                                "UPDATE users SET kills = COALESCE(kills, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                                (killer_raw,),
                            )
                            await db.execute(
                                """
                                UPDATE users 
                                SET max_kill_distance = ? 
                                WHERE game_nick = ? COLLATE NOCASE 
                                  AND COALESCE(max_kill_distance, 0) < ?
                                """,
                                (dist, killer_raw, dist),
                            )
                            if is_headshot:
                                await db.execute(
                                    "UPDATE users SET headshots = COALESCE(headshots, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                                    (killer_raw,),
                                )

                        await db.execute(
                            "UPDATE users SET deaths = COALESCE(deaths, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                            (victim_raw,),
                        )
                    except Exception as db_err:
                        logger.error("[KILL_LOG] Ошибка обновления статистики убийства в БД: %s", db_err)

                    # 2. Формируем и отправляем сообщение в Telegram
                    headshot_badge = "\n🎯 <b>(В ГОЛОВУ!)</b>" if is_headshot else ""
                    message_text = (
                        f"🎮 <b>{cached_server_name}</b>\n"
                        f"Произошло убийство 💀{headshot_badge}\n"
                        f"<code>{html.escape(str(kill['time']))} {html.escape(str(kill_date))}</code>\n"
                        f"👤 <b>Игрок:</b> <code>{killer_safe}</code>\n"
                        f"⚰️ <b>Убил игрока:</b> <code>{victim_safe}</code>\n"
                        f"🔫 из <b>{weapon_safe}</b> с <code>{kill['distance']} м</code>"
                    )

                    await _send_telegram_safe(
                        bot=bot,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        text=message_text,
                    )

                    # Промежуточно сохраняем хэш (имя файла обновляем только если оно передано для конкретной записи)
                    kill_file = kill.get("file_name")
                    if kill_file:
                        last_file_name = kill_file
                        await db.set_setting("last_kill_log_file", last_file_name)

                    last_line_hash = kill["hash"]
                    await db.set_setting("last_kill_log_hash", last_line_hash)

                    await asyncio.sleep(2)

            # Фиксируем итоговое состояние файла и последнего хэша
            if updated_file and (updated_file != last_file_name or updated_hash != last_line_hash):
                last_file_name = updated_file
                last_line_hash = updated_hash or ""
                await db.set_setting("last_kill_log_file", last_file_name)
                await db.set_setting("last_kill_log_hash", last_line_hash)

        except asyncio.CancelledError:
            logger.info("[KILL_LOG] Фоновая задача остановлена.")
            raise
        except Exception as e:
            logger.error("[KILL_LOG] Ошибка в цикле проверки убийств: %s", e, exc_info=True)

        await asyncio.sleep(poll_interval)
