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


async def _safe_edit_or_answer(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "HTML",
) -> None:
    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode=parse_mode)
    else:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()


@router.callback_query(F.data == "summary")
async def summary_menu(callback: CallbackQuery):
    await _safe_edit_or_answer(
        callback,
        text="📊 <b>Выберите раздел сводки:</b>",
        markup=_summary_markup(),
    )


@router.callback_query(F.data == "summary:online")
async def online(callback: CallbackQuery, nitrado_api):
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer("⏳ Загружаем список игроков...")

    count, nicks = await nitrado_api.get_online_players()
    if nicks:
        safe_nicks = [f"• <code>{html.escape(str(n))}</code>" for n in nicks]
        text = "<b>Игроки онлайн:</b>\n" + "\n".join(safe_nicks)
    elif count > 0:
        text = "<i>Список ников временно недоступен (обновляется в логах раз в 5 мин.).</i>"
    else:
        text = "<i>Сейчас на сервере никого нет.</i>"

    await _safe_edit_or_answer(
        callback,
        text=f"🟢 <b>Онлайн: {count}</b>\n\n{text}",
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
        f"• <b>{html.escape(str(r['name']))} [{html.escape(str(r['tag']))}]</b> — "
        f"лидер <code>{html.escape(str(r['game_nick']))}</code>, убийств: <b>{r['kills']}</b>"
        for r in rows
    ]
    await _safe_edit_or_answer(
        callback,
        text="🏆 <b>Лидеры группировок:</b>\n" + ("\n".join(lines) or "<i>Данных пока нет.</i>"),
        markup=_back_to_summary_markup(),
    )


@router.callback_query(F.data == "summary:kills")
async def kill_leaders(callback: CallbackQuery, db):
    rows = await db.fetchall(
        "SELECT game_nick, kills FROM users ORDER BY kills DESC LIMIT 10"
    )
    lines = [f"• <code>{html.escape(str(r['game_nick']))}</code> — <b>{r['kills']}</b>" for r in rows]
    await _safe_edit_or_answer(
        callback,
        text="🔪 <b>Топ игроков по убийствам:</b>\n" + ("\n".join(lines) or "<i>Данных пока нет.</i>"),
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
        f"🕒 <b>Плановый рестарт:</b> {html.escape(str(settings.restart_at))}\n\n"
        "Отправить запрос немедленного рестарта сервера Nitrado?"
    )
    await _safe_edit_or_answer(callback, text=text, markup=markup)


@router.callback_query(F.data == "restart:confirm")
async def restart_confirm(callback: CallbackQuery, settings, nitrado_api):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text("⏳ Отправляем запрос на рестарт сервера Nitrado...")
    with contextlib.suppress(TelegramBadRequest):
        await callback.answer()

    ok = await nitrado_api.restart_server()
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "✅ Запрос рестарта успешно отправлен на сервер Nitrado."
            if ok
            else "❌ Не удалось отправить запрос рестарта (проверьте токен Nitrado или статус сервера)."
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
        "🚫 <b>Заблокированные игроки:</b>\n" + ("\n".join(safe_players) or "<i>Список пуст.</i>"),
        parse_mode="HTML",
    )
    if callback.id != "0":
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()
