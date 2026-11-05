import asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from database.db import Database
from services.nitrado_api import NitradoAPI

# Шаблон вашего сообщения
def format_server_info(server_data: dict) -> str:
    if not server_data:
        return "❌ **Сервер DayZ временно недоступен (ошибка API)**"
    
    # Извлекаем параметры из ответа Nitrado API
    status = server_data.get("status", "unknown")
    settings = server_data.get("settings", {})
    query = server_data.get("query", {})
    
    server_name = settings.get("server_name", "DayZ PlayStation Server")
    slots = server_data.get("slots", 0)
    players_online = query.get("player_current", 0)
    map_name = query.get("map", "Chernarus")
    
    status_emoji = "🟢 Онлайн" if status == "started" else "🔴 Выключен"
    
    return (
        f"🎮 **Мониторинг сервера DayZ (PlayStation)**\n\n"
        f"📛 **Название:** {server_name}\n"
        f"📊 **Статус:** {status_emoji}\n"
        f"🗺️ **Карта:** {map_name}\n"
        f"👥 **Игроки:** `{players_online}/{slots}`\n\n"
        f"🔄 _Последнее обновление: автоматически каждые 5 минут_"
    )

async def start_server_monitoring(bot: Bot, db: Database, nitrado: NitradoAPI, chat_id: int, thread_id: int):
    """Фоновая задача для создания и обновления сообщения в теме группы"""
    
    # 1. Пытаемся получить сохраненный message_id из базы данных
    # Вам нужно будет добавить такую таблицу/метод в класс Database
    message_id = await db.get_setting("dayz_monitor_message_id") 
    
    while True:
        try:
            # Получаем свежие данные от Nitrado
            server_data = await nitrado.get_server_status()
            text = format_server_info(server_data)
            
            if not message_id:
                # Если сообщения еще нет, создаем его в нужной теме (message_thread_id)
                msg = await bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    text=text,
                    parse_mode="Markdown"
                )
                message_id = msg.message_id
                # Сохраняем ID сообщения в БД, чтобы не создавать новое при перезапуске бота
                await db.set_setting("dayz_monitor_message_id", message_id)
            else:
                # Если сообщение уже есть, просто редактируем его
                try:
                    await bot.edit_message_text(
                        text=text,
                        chat_id=chat_id,
                        message_id=int(message_id),
                        parse_mode="Markdown"
                    )
                except TelegramAPIError as e:
                    # Если сообщение удалили вручную, сбрасываем ID, чтобы создать заново
                    if "message to edit not found" in str(e).lower():
                        message_id = None
                        await db.set_setting("dayz_monitor_message_id", None)
                        continue
                        
        except Exception as e:
            print(f"Ошибка в цикле мониторинга: {e}")
            
        # Интервал обновления (например, 300 секунд / 5 минут)
        await asyncio.sleep(300)

