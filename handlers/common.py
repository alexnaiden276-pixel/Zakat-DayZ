from aiogram import F, Router
from aiogram.types import CallbackQuery

from keyboards.menus import back_button

router = Router()

@router.callback_query(F.data == "rules")
async def rules(callback: CallbackQuery, settings):
    text = settings.rules_text
    reply_markup = back_button()
    
    # ИСПРАВЛЕНО: Проверка на вызов из нижнего текстового меню
    if callback.id == "0":
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        await callback.answer()

@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    group_text = "Нет группировки"
    position = "—"

    if user["group_id"]:
        group = await db.fetchone(
            "SELECT name FROM groups WHERE id = ?", (user["group_id"],)
        )
        group_text = group["name"] if group else "Нет группировки"
        position = {
            "leader": "Лидер",
            "officer": "Офицер",
            "player": "Рядовой",
        }.get(user["role"], user["role"])

    text_content = (
        "📁 <b>Досье</b>\n\n"
        f"Ник: <code>{user['game_nick']}</code>\n"
        f"Убийства: {user['kills']}\n"
        f"Смерти: {user['deaths']}\n"
        f"Хедшоты: {user['headshots']}\n"
        f"Максимальная дистанция убийства: {user['max_kill_distance']} м\n"
        f"Баланс: {user['balance']} 🪙\n"
        f"Группировка: {group_text}\n"
        f"Должность: {position}"
    )
    reply_markup = back_button()

    # ИСПРАВЛЕНО: Проверка на вызов из нижнего текстового меню
    if callback.id == "0":
        await callback.message.answer(text_content, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text_content, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()
