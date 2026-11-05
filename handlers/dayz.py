from aiogram import Router, Types
from aiogram.filters import Command
from services.game_api import GameAPI

router = Router()

# ID вашей целевой группы (можно узнать через ботов вроде @username_to_id_bot)
# Важно: ID групп в Telegram всегда начинаются с минуса (например, -100123456789)
TARGET_GROUP_ID = 4491805684/2 
NITRADO_SERVER_ID = 20101252 # ID вашего сервера в Nitrado

@router.message(Command("server"))
async def cmd_server_status(message: types.Message, game_api: GameAPI):
    # Проверяем, что команда вызвана именно в нужной группе
    if message.chat.id != TARGET_GROUP_ID:
        return # Игнорируем команду в других чатах

    await message.channel.trigger_typing()
    
    server_data = await game_api.get_dayz_server_status(NITRADO_SERVER_ID)
    
    if not server_data:
        await message.answer("❌ Не удалось получить данные от хостинга Nitrado.")
        return

    status_emoji = "🟢 Онлайн" if server_data["status"] == "started" else "🔴 Офлайн"
    
    text = (
        f"🎮 **Статус сервера DayZ (PS)**\n\n"
        f"📛 **Название:** {server_data['name']}\n"
        f"⚡ **Статус:** {status_emoji}\n"
        f"👥 **Игроки:** {server_data['players_online']}/{server_data['players_max']}\n"
        f"🗺 **Карта:** {server_data['map']}"
    )
    
    await message.answer(text, parse_mode="Markdown")

