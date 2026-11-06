import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))

def format_server_info(server_data: dict | None) -> str:
    now_str = datetime.now(MSK).strftime("%H:%M:%S МСК")

    if not server_data:
        return (
            "❌ <b>Сервер DayZ временно недоступен (ошибка API)</b>\n\n"
            f"🕒 <i>Обновлено: {now_str}</i>"
        )

    settings = server_data.get("settings") or {}
    config = settings.get("config") or {}
    query = server_data.get("query") or {}

    raw_name = (
        query.get("server_name")
        or config.get("hostname")
        or "DayZ PlayStation Server"
    )
    server_name = html.escape(str(raw_name))

    status = server_data.get("status")
    slots = server_data.get("slots") or query.get("player_max", 0)
    players_online = query.get("player_current", 0)
    map_name = html.escape(str(query.get("map", "Chernarus")))

    status_emoji = "🟢 Онлайн" if status == "started" else "🔴 Выключен"

    return (
        f"🎮 <b>Мониторинг сервера DayZ (PlayStation)</b>\n\n"
        f"📛 <b>Название:</b> {server_name}\n"
        f"📊 <b>Статус:</b> {status_emoji}\n"
        f"🗺️ <b>Карта:</b> {map_name}\n"
        f"👥 <b>Игроки:</b> <code>{players_online}/{slots}</code>\n\n"
        f"🔄 <i>Обновлено: {now_str} (каждые 5 мин.)</i>"
    )

async def start_server_monitoring(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    chat_id: int,
    thread_id: int | None = None,
    initial_message_id: int | None = None,
) -> None:
    """Фоновая задача для создания и обновления сообщения в теме группы."""
    print("\n[MONITOR] >>> ФОНОВАЯ ЗАДАЧА УСПЕШНО СТАРТОВАЛА В ПОТОКЕ! <<<", flush=True)
    
    message_id = initial_message_id

    while True:
        try:
            print("[MONITOR] Шаг 1: Запрашиваем данные от Nitrado API...", flush=True)
            server_data = await nitrado.get_server_status()
            
            status_raw = server_data.get('status') if server_data else 'None'
            print(f"[MONITOR] Шаг 2: Данные получены! Статус сервера: {status_raw}", flush=True)
            text = format_server_info(server_data)

            print(f"[MONITOR] Шаг 3: Пытаемся отправить сообщение в Chat ID: {chat_id}, Thread ID: {thread_id}", flush=True)
            
                        if not message_id:
                print("[MONITOR] Отправляем НОВОЕ сообщение...", flush=True)
                msg = await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode="HTML",
                )
                message_id = msg.message_id
                print(f"[MONITOR] УСПЕХ! Новое сообщение создано в Telegram с ID: {message_id}", flush=True)
                
                # Сохраняем ID нового сообщения в базу данных
                try:
                    await db.set_setting("dayz_monitor_message_id", str(message_id))
                    print("[MONITOR] ID сообщения успешно зафиксирован в SQLite.", flush=True)
                except Exception as db_err:
                    print(f"[MONITOR] Ошибка сохранения ID в базу: {db_err}", flush=True)

                    print(f"[MONITOR] Не удалось сохранить ID в базу (но сообщение отправлено): {db_err}", flush=True)
            else:
                try:
                    await bot.edit_message_text(
                        text=text,
                        chat_id=chat_id,
                        message_id=message_id,
                        parse_mode="HTML",
                    )
                    print(f"[MONITOR] УСПЕХ! Сообщение {message_id} успешно обновлено.", flush=True)
                except TelegramRetryAfter as e:
                    logger.warning("Флуд-контроль Telegram, пауза %s сек.", e.retry_after)
                    await asyncio.sleep(e.retry_after)
                    continue
                except TelegramAPIError as e:
                    err_text = str(e).lower()
                    if "message is not modified" in err_text:
                        print("[MONITOR] Текст сообщения не изменился.", flush=True)
                                        elif (
                        "message to edit not found" in err_text
                        or "message can't be edited" in err_text
                        or "chat not found" in err_text
                    ):
                        print("[MONITOR] Старое сообщение потеряно. Сбрасываем ID.", flush=True)
                        message_id = None
                        try:
                            await db.set_setting("dayz_monitor_message_id", "")
                        except:
                            pass
                        continue
                    else:
                        print(f"[MONITOR] Ошибка Telegram API: {e}", flush=True)

        except asyncio.CancelledError:
            print("[MONITOR] Задача мониторинга остановлена.", flush=True)
            raise
        except Exception as e:
            print(f"\n[MONITOR] !!! КРИТИЧЕСКАЯ ОШИБКА В ЦИКЛЕ: {e} !!!\n", flush=True)

        print("[MONITOR] Шаг 4: Засыпаем на 5 минут...\n", flush=True)
        await asyncio.sleep(300)
