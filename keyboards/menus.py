from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    buttons = [
        InlineKeyboardButton(text="🖥 Терминал", callback_data="terminal"),
        InlineKeyboardButton(text="📦 Сдать лут", callback_data="sell"),
        InlineKeyboardButton(text="💰 Кошелек", callback_data="wallet"),
        InlineKeyboardButton(text="📁 Досье", callback_data="profile"),
        InlineKeyboardButton(text="👥 Группировка", callback_data="group"),
        InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
        InlineKeyboardButton(text="📜 Правила", callback_data="rules"),
        InlineKeyboardButton(text="📊 Сводка", callback_data="summary"),
    ]
    
    builder.add(*buttons)
    builder.adjust(2) 
    
    if is_admin:
        # Размещаем админские кнопки друг под другом внизу меню
        builder.row(InlineKeyboardButton(text="💸 Финансы (Админ)", callback_data="admin_finance"))
        builder.row(InlineKeyboardButton(text="🔄 Рестарт сервера", callback_data="restart"))
        
    return builder.as_markup()

# ФУНКЦИЯ ДЛЯ БОЛЬШОЙ КНОПКИ СНИЗУ ЭКРАНА
def start_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Старт 🚀")]
        ],
        resize_keyboard=True,          # Делает кнопку компактной по высоте
        persistent=True                # Кнопка не будет скрываться под иконку
    )

def back_button(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
        ]
    )
