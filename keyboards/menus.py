from enum import StrEnum

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


class MenuBtn(StrEnum):
    TERMINAL = "🖥 Терминал"
    WALLET = "💰 Кошелек"
    DOSSIER = "📁 Досье"
    GROUP = "👥 Группировка"
    SUPPORT = "🆘 Поддержка"
    RULES = "📜 Правила"
    SUMMARY = "📊 Сводка"

    # Вход и выход из админ-панели
    ADMIN_PANEL = "⚙️ Админ-панель"
    BACK_TO_MAIN = "⬅️ В главное меню"

    # Админ-кнопки
    ADMIN_FINANCE = "💸 Финансы (Админ)"
    ADMIN_SHOP = "🛠 Управление магазином"
    ADMIN_CLANS = "🏳️ Кланы и флаги"
    ADMIN_RAID = "💥 Рейд-тайм"
    ADMIN_BANS = "🔨 Баны и Автобан"
    ADMIN_RESTART = "🔄 Рестарт сервера"


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()

    user_buttons = [
        MenuBtn.TERMINAL, MenuBtn.WALLET,
        MenuBtn.DOSSIER, MenuBtn.GROUP,
        MenuBtn.SUPPORT, MenuBtn.RULES,
        MenuBtn.SUMMARY,
    ]
    for btn in user_buttons:
        builder.button(text=btn)

    if is_admin:
        builder.button(text=MenuBtn.ADMIN_PANEL)
        builder.adjust(2, 2, 2, 1, 1)
    else:
        builder.adjust(2, 2, 2, 1)

    return builder.as_markup(
        resize_keyboard=True,
        one_time_keyboard=False,
        persistent=True,
    )


def admin_menu() -> ReplyKeyboardMarkup:
    """Клавиатура админ-панели."""
    builder = ReplyKeyboardBuilder()

    admin_buttons = [
        MenuBtn.ADMIN_FINANCE, MenuBtn.ADMIN_SHOP,
        MenuBtn.ADMIN_CLANS, MenuBtn.ADMIN_RAID,
        MenuBtn.ADMIN_BANS, MenuBtn.ADMIN_RESTART,
        MenuBtn.BACK_TO_MAIN,
    ]
    for btn in admin_buttons:
        builder.button(text=btn)

    builder.adjust(2, 2, 2, 1)

    return builder.as_markup(
        resize_keyboard=True,
        one_time_keyboard=False,
        persistent=True,
    )


def back_button(target: str = "menu") -> InlineKeyboardMarkup:
    """Универсальная инлайн-кнопка назад."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=target)
    return builder.as_markup()
