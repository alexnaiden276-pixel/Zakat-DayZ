import contextlib
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()


def _summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Онлайн сервера", callback_data="summary:online")],
        [InlineKeyboardButton(text="🏆 Лидеры группировок", callback_data="summary:groups")],
        [InlineKeyboardButton(text="🔪 Убийства", callback_data="summary:kills")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])


def _back_to_summary_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="summary")]
    ])


async def _safe_edit_or_answer(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup)
    else:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=markup)
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()


@router.callback_query(F.data == "summary")
async def summary_menu(callback: CallbackQuery):
    await _safe_edit_or_answer(
        callback,
        text="📊 Выберите раздел сводки:",
        markup=_summary_markup(),
    )


@router.callback_query(F.data == "summary:online")
async def online(callback: CallbackQuery, game_api):
    count, nicks = await game_api.server_status()
    safe_nicks = [f"• {html.escape(str(n))}" for n in nicks]
    text = "Игроки онлайн:\n" + ("\n".join(safe_nicks) or "Список пуст.")
    await _safe_edit_or_answer(
        callback,
        text=f"🟢 Онлайн: {count}\n\n{text}",
        markup=_back_to_summary_markup(),
    )


@router.callback_query(F.data == "summary:groups")
async def group_leaders(callback: CallbackQuery, db):
    rows = await db.fetchall(
        """SELECT g.name, g.tag, u.game_nick, u.kills
           FROM groups g JOIN users u ON u.id = g.leader_id
           ORDER BY u.kills DESC LIMIT 10"""
    )
    lines = [
        f"• {html.escape(str(r['name']))} [{html.escape(str(r['tag']))}] — "
        f"лидер {html.escape(str(r['game_nick']))}, убийств: {r['kills']}"
        for r in rows
    ]
    await _safe_edit_or_answer(
        callback,
        text="🏆 Лидеры группировок:\n" + ("\n".join(lines) or "Данных пока нет."),
        markup=_back_to_summary_markup(),
    )


@router.callback_query(F.data == "summary:kills")
async def kill_leaders(callback: CallbackQuery, db):
    rows = await db.fetchall(
        "SELECT game_nick, kills FROM users ORDER BY kills DESC LIMIT 10"
    )
    lines = [f"• {html.escape(str(r['game_nick']))} — {r['kills']}" for r in rows]
    await _safe_edit_or_answer(
        callback,
        text="🔪 Топ игроков по убийствам:\n" + ("\n".join(lines) or "Данных пока нет."),
        markup=_back_to_summary_markup(),
    )


@router.callback_query(F.data == "restart")
async def restart(callback: CallbackQuery, settings):
    if callback.from_user.id not in settings.admin_ids:
        if callback.id != "0":
            await callback.answer("Недостаточно прав.", show_alert=True)
        else:
            await callback.message.answer("Недостаточно прав.")
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Запросить рестарт", callback_data="restart:confirm")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    text = (
        f"Плановый рестарт: {settings.restart_at}\n"
        "Запрос немедленного рестарта сервера?"
    )
    await _safe_edit_or_answer(callback, text=text, markup=markup)


@router.callback_query(F.data == "restart:confirm")
async def restart_confirm(callback: CallbackQuery, settings, game_api):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text("⏳ Отправляем запрос на рестарт сервера...")
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer()

    ok = await game_api.restart_server()
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "✅ Запрос рестарта отправлен." if ok else "❌ Не удалось отправить запрос рестарта."
        )


@router.callback_query(F.data == "bans")
async def bans(callback: CallbackQuery, settings, game_api):
    if callback.from_user.id not in settings.admin_ids:
        if callback.id != "0":
            await callback.answer("Раздел доступен только администрации.", show_alert=True)
        else:
            await callback.message.answer("Раздел доступен только администрации.")
        return

    players = await game_api.banned_players()
    safe_players = [html.escape(str(p)) for p in players]
    await callback.message.answer(
        "🚫 Заблокированные игроки:\n" + ("\n".join(safe_players) or "Список пуст.")
    )
    if callback.id != "0":
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()
