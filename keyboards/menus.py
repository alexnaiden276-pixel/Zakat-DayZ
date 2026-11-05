from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🖥 Терминал", callback_data="terminal"),
            InlineKeyboardButton(text="📦 Сдать лут", callback_data="sell"),
        ],
        [
            InlineKeyboardButton(text="💰 Кошелек", callback_data="wallet"),
            InlineKeyboardButton(text="📁 Досье", callback_data="profile"),
        ],
        [
            InlineKeyboardButton(text="👥 Группировка", callback_data="group"),
            InlineKeyboardButton(text="🆘 Поддержка", callback_data="support"),
        ],
        [
            InlineKeyboardButton(text="📜 Правила", callback_data="rules"),
            InlineKeyboardButton(text="📊 Сводка", callback_data="summary"),
        ],
    ]
    if is_admin:
        rows.append(
            [InlineKeyboardButton(text="🔄 Рестарт сервера", callback_data="restart")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_button(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
        ]
    )