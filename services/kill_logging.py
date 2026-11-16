import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
SERVER_TZ = ZoneInfo("Europe/Berlin")  # Франкфурт (автоучёт зимнего/летнего времени)
SERVER_NAME_CACHE_TTL = 1800  # Кэшируем имя сервера на 30 минут

ADM_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _convert_kill_dt_to_msk(time_str: str, file_name: str | None) -> tuple[str, str]:
    """Переводит время и дату убийства из часового пояса сервера (Франкфурт) в МСК."""
    now_msk = datetime.now(MSK)
    try:
        h, m, s = map(int, time_str.split(":"))
        base_date = now_msk.astimezone(SERVER_TZ).date()

        if file_name:
            m_date = ADM_DATE_PATTERN.search(file_name)
            if m_date:
                base_date = datetime.strptime(m_date.group(1), "%Y-%m-%d").date()

        server_dt = datetime(
            base_date.year,
            base_date.month,
            base_date.day,
            h,
            m,
            s,
            tzinfo=SERVER_TZ,
        )
        # Если файл создан до полуночи (например, 23:50), а убийство произошло после (00:15)
        if file_name and server_dt.hour < 4:
            m_time = re.search(r"\d{4}-\d{2}-\d{2}_(\d{2})-", file_name)
            if m_time and int(m_time.group(1)) >= 20:
                server_dt += timedelta(days=1)

        msk_dt = server_dt.astimezone(MSK)
        return msk_dt.strftime("%H:%M:%S"), msk_dt.strftime("%Y-%m-%d")
    except Exception:
        return time_str, now_msk.strftime("%Y-%m-%d")


async def _send_telegram_safe(
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    text: str,
) -> bool:
    """Безопасная отправка сообщения с обработкой флуд-контроля."""
    for _ in range(2):
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

    last_file_name = (await db.get_setting("last_kill_log_file")) or None
    last_line_hash = (await db.get_setting("last_kill_log_hash")) or None

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

                for kill in new_kills:
                    killer_raw = str(kill["killer"])
                    victim_raw = str(kill["victim"])
                    killer_safe = html.escape(killer_raw)
                    victim_safe = html.escape(victim_raw)
                    weapon_safe = html.escape(str(kill["weapon"]))
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
                        dist = 0.0
                        logger.error("[KILL_LOG] Ошибка обновления статистики убийства в БД: %s", db_err)

                    # 2. Переводим время в МСК и форматируем дистанцию
                    kill_file = kill.get("file_name") or last_file_name
                    kill_time_msk, kill_date_msk = _convert_kill_dt_to_msk(
                        str(kill["time"]),
                        kill_file,
                    )
                    dist_int = int(round(dist))
                    dist_str = f" с <code>{dist_int} м</code>" if dist_int > 0 else " (в упор)"

                    # 3. Формируем и отправляем сообщение в Telegram
                    headshot_badge = "\n🎯 <b>(В ГОЛОВУ!)</b>" if is_headshot else ""
                    message_text = (
                        f"🎮 <b>{cached_server_name}</b>\n"
                        f"Произошло убийство 💀{headshot_badge}\n"
                        f"<code>{kill_time_msk} {kill_date_msk} МСК</code>\n"
                        f"👤 <b>Игрок:</b> <code>{killer_safe}</code>\n"
                        f"⚰️ <b>Убил игрока:</b> <code>{victim_safe}</code>\n"
                        f"🔫 из <b>{weapon_safe}</b>{dist_str}"
                    )

                    await _send_telegram_safe(
                        bot=bot,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        text=message_text,
                    )

                    # Промежуточно сохраняем прогресс после каждого убийства
                    if kill.get("file_name"):
                        last_file_name = kill["file_name"]
                        await db.set_setting("last_kill_log_file", last_file_name)

                    last_line_hash = kill["hash"]
                    await db.set_setting("last_kill_log_hash", last_line_hash)

                    await asyncio.sleep(2)

            # Фиксируем итоговое состояние файла и последнего хэша (в т.ч. при пустом новом файле после рестарта)
            if updated_file and (updated_file != last_file_name or updated_hash != last_line_hash):
                last_file_name = updated_file
                last_line_hash = updated_hash
                await db.set_setting("last_kill_log_file", last_file_name)
                await db.set_setting("last_kill_log_hash", last_line_hash or "")

        except asyncio.CancelledError:
            logger.info("[KILL_LOG] Фоновая задача остановлена.")
            raise
        except Exception as e:
            logger.error("[KILL_LOG] Ошибка в цикле проверки убийств: %s", e, exc_info=True)

        await asyncio.sleep(poll_interval)
