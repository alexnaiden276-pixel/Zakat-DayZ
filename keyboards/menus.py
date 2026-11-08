from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    # Базовые кнопки меню для всех игроков
    keyboard_buttons = [
        [KeyboardButton(text="🖥 Терминал"), KeyboardButton(text="📦 Сдать лут")],
        [KeyboardButton(text="💰 Кошелек"), KeyboardButton(text="📁 Досье")],
        [KeyboardButton(text="👥 Группировка"), KeyboardButton(text="🆘 Поддержка")],
        [KeyboardButton(text="📜 Правила"), KeyboardButton(text="📊 Сводка")]
    ]
    
    # Если пользователь админ, добавляем дополнительные кнопки
    if is_admin:
        keyboard_buttons.append([KeyboardButton(text="💸 Финансы (Админ)")])
        keyboard_buttons.append([KeyboardButton(text="🔄 Рестарт сервера")])
        
    return ReplyKeyboardMarkup(
        keyboard=keyboard_buttons,
        resize_keyboard=True,    # Делает кнопки компактными по высоте
        one_time_keyboard=True,  # КЛАВИАТУРА АВТОМАТИЧЕСКИ ПРЯЧЕТСЯ ПОСЛЕ НАЖАТИЯ
        persistent=False         # Разрешаем клавиатуре скрываться (убираем жесткое закрепление)
    )

# Кнопка назад (если она используется внутри инлайн-сообщений подменю)
def back_button(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=target)]
        ]
    )
