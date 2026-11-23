from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()

    # 1. Базовые кнопки для всех игроков
    builder.row(
        KeyboardButton(text="🖥 Терминал"),
        KeyboardButton(text="💰 Кошелек")
    )
    builder.row(
        KeyboardButton(text="📁 Досье"),
        KeyboardButton(text="👥 Группировка")
    )
    builder.row(
        KeyboardButton(text="🆘 Поддержка"),
        KeyboardButton(text="📜 Правила")
    )
    builder.row(
        KeyboardButton(text="📊 Сводка")
    )

    # 2. Единая кнопка для входа в админ-панель
    if is_admin:
        builder.row(
            KeyboardButton(text="⚙️ Админ-панель")
        )

    return builder.as_markup(
        resize_keyboard=True,
        one_time_keyboard=False,
        persistent=True
    )


def admin_menu() -> ReplyKeyboardMarkup:
    """Клавиатура админ-панели с сохранением оригинальных текстов кнопок."""
    builder = ReplyKeyboardBuilder()

    builder.row(
        KeyboardButton(text="💸 Финансы (Админ)"),
        KeyboardButton(text="🛠 Управление магазином")
    )
    builder.row(
        KeyboardButton(text="🏳️ Кланы и флаги"),
        KeyboardButton(text="🔄 Рестарт сервера")
    )
    builder.row(
        KeyboardButton(text="⬅️ В главное меню")
    )

    return builder.as_markup(
        resize_keyboard=True,
        one_time_keyboard=False,
        persistent=True
    )


def back_button(target: str = "menu") -> InlineKeyboardMarkup:
    """Универсальная инлайн-кнопка назад."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
        ]
    )
