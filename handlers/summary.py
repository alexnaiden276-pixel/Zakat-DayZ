from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()


@router.callback_query(F.data == "summary")
async def summary_menu(callback: CallbackQuery):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Онлайн сервера", callback_data="summary:online")],
        [InlineKeyboardButton(text="🏆 Лидеры группировок", callback_data="summary:groups")],
        [InlineKeyboardButton(text="🔪 Убийства", callback_data="summary:kills")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    await callback.message.edit_text("📊 Выберите раздел сводки:", reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "summary:online")
async def online(callback: CallbackQuery, game_api):
    count, nicks = await game_api.server_status()
    text = "Игроки онлайн:\n" + ("\n".join(f"• {n}" for n in nicks) or "Список пуст.")
    await callback.message.edit_text(
        f"🟢 Онлайн: {count}\n\n{text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="summary")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "summary:groups")
async def group_leaders(callback: CallbackQuery, db):
    rows = await db.fetchall(
        """SELECT g.name, g.tag, u.game_nick, u.kills
           FROM groups g JOIN users u ON u.id = g.leader_id
           ORDER BY u.kills DESC LIMIT 10"""
    )
    lines = [
        f"• {r['name']} [{r['tag']}] — лидер {r['game_nick']}, убийств: {r['kills']}"
        for r in rows
    ]
    await callback.message.edit_text(
        "🏆 Лидеры группировок:\n" + ("\n".join(lines) or "Данных пока нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="summary")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "summary:kills")
async def kill_leaders(callback: CallbackQuery, db):
    rows = await db.fetchall(
        "SELECT game_nick, kills FROM users ORDER BY kills DESC LIMIT 10"
    )
    lines = [f"• {r['game_nick']} — {r['kills']}" for r in rows]
    await callback.message.edit_text(
        "🔪 Топ игроков по убийствам:\n" + ("\n".join(lines) or "Данных пока нет."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="summary")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "restart")
async def restart(callback: CallbackQuery, settings, game_api):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Запросить рестарт", callback_data="restart:confirm")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    await callback.message.edit_text(
        f"Плановый рестарт: {settings.restart_at}\n"
        "Запрос немедленного рестарта сервера?",
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(F.data == "restart:confirm")
async def restart_confirm(callback: CallbackQuery, settings, game_api):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    ok = await game_api.restart_server()
    await callback.message.edit_text(
        "Запрос рестарта отправлен." if ok else "Не удалось отправить запрос рестарта."
    )
    await callback.answer()


@router.callback_query(F.data == "bans")
async def bans(callback: CallbackQuery, settings, game_api):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Раздел доступен только администрации.", show_alert=True)
        return
players = await game_api.banned_players()
    await callback.message.answer(
"🚫 Заблокированные игроки:\n" + ("\n".join(players) or "Список пуст.")
    )
    await callback.answer()