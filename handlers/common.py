import contextlib
import html
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from keyboards.menus import back_button

router = Router()


@router.callback_query(F.data == "rules")
async def rules(callback: CallbackQuery, settings):
    text = settings.rules_text
    reply_markup = back_button()

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user:
        if callback.id != "0":
            await callback.answer("Профиль не найден. Нажмите /start", show_alert=True)
        return

    group_text = "Нет группировки"
    position = "—"

    if user["group_id"]:
        group = await db.fetchone(
            "SELECT name FROM groups WHERE id = ?", (user["group_id"],)
        )
        if group:
            group_text = html.escape(group["name"])
            position = {
                "leader": "Лидер",
                "officer": "Офицер",
                "player": "Рядовой",
            }.get(user["role"], html.escape(user["role"]))

    nick_safe = html.escape(user["game_nick"])
    kills = user["kills"] or 0
    deaths = user["deaths"] or 0
    headshots = user["headshots"] or 0
    max_dist = round(user["max_kill_distance"] or 0, 1)

    # Считаем K/D (убийства / смерти) и процент хедшотов (HS%)
    kd_ratio = round(kills / deaths, 2) if deaths > 0 else float(kills)
    hs_percent = round((headshots / kills) * 100, 1) if kills > 0 else 0.0

    text_content = (
        "📁 <b>Досье</b>\n\n"
        f"Ник: <code>{nick_safe}</code>\n"
        f"Убийства: <b>{kills}</b> | Смерти: <b>{deaths}</b> (K/D: {kd_ratio})\n"
        f"Хедшоты: <b>{headshots}</b> ({hs_percent}%)\n"
        f"Макс. дистанция убийства: <code>{max_dist} м</code>\n"
        f"Баланс: <b>{user['balance']}</b> 🪙\n"
        f"Группировка: {group_text}\n"
        f"Должность: {position}"
    )
    reply_markup = back_button()

    if callback.id == "0":
        await callback.message.answer(text_content, reply_markup=reply_markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(text_content, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()
