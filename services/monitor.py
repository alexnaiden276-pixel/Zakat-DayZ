import asyncio
import html
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from database.db import Database
from services.nitrado_api import NitradoAPI


def format_server_info(server_data: dict) -> str:
    if not server_data:
        return "❌ <b>Сервер DayZ временно недоступен (ошибка API)</b>"
    
    # Если API возвращает обертку {"gameserver": {...}}, раскомментируйте строку ниже:
    # server_data = server_data.get("gameserver", server_data)

    status = server_data.get("status", "unknown")
    settings = server_data.get("settings") or {}
    query = server_data.get("query") or {}
    
    # Экранируем спецсимволы в названии сервера для безопасности HTML
    raw_name = settings.get("server_name", "DayZ PlayStation Server")
    server_name = html.escape(str(raw_name))
    
    slots = server_data.get("slots", 0)
    players_online = query.get("player_current", 0)
    map_name = html.escape(str(query.get("map", "Chernarus")))
    
    status_emoji = "🟢 Онлайн" if status == "started" else "🔴 Выключен"
    
    return (
        f"🎮 <b>Мониторинг сервера DayZ (PlayStation)</b>\n\n"
        f"📛 <b>Название:</b> {server_name}\n"
        f"📊 <b>Статус:</b> {status_emoji}\n"
        f"🗺️ <b>Карта:</b> {map_name}\n"
        f"👥 <b>Игроки:</b> <code>{players_online}/{slots}</code>\n\n"
        f"🔄 <i>Последнее обновление: автоматически каждые 5 минут</i>"
    )


async def start_server_monitoring(bot: Bot, db: Database, nitrado: NitradoAPI, chat_id: int, thread_id: int):
    """Фоновая задача для создания и обновления сообщения в теме группы"""
    message_id = await db.get_setting("dayz_monitor_message_id") 
    
    while True:
        try:
            server_data = await nitrado.get_server_status()
            text = format_server_info(server_data)
            
            if not message_id:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode="HTML"
                )
                message_id = msg.message_id
                await db.set_setting("dayz_monitor_message_id", message_id)
            else:
                try:
                    await bot.edit_message_text(
                        text=text,
                        chat_id=chat_id,
                        message_id=int(message_id),
                        parse_mode="HTML"
                    )
                except TelegramAPIError as e:
                    err_text = str(e).lower()
                    if "message is not modified" in err_text:
                        # Текст не изменился, ничего делать не нужно
                        pass
                    elif "message to edit not found" in err_text or "message can't be edited" in err_text:
                        # Сообщение удалено или устарело — сбрасываем ID и создаем заново
                        message_id = None
                        await db.set_setting("dayz_monitor_message_id", None)
                        continue
                    else:
                        print(f"Неизвестная ошибка Telegram при редактировании: {e}")
                        
        except Exception as e:
            print(f"Ошибка в цикле мониторинга: {e}")
            
        await asyncio.sleep(300)
