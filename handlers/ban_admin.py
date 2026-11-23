import contextlib
import html
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from keyboards.menus import MenuBtn

router = Router()
logger = logging.getLogger(__name__)


class BanAdminStates(StatesGroup):
    wait_ban_nick = State()
    wait_zone_name = State()
    wait_zone_coords = State()
    wait_zone_radius = State()


def _is_admin(user_id: int, settings) -> bool:
    return user_id in getattr(settings, "admin_ids", set())


def _main_ban_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚫 Забанить игрока", callback_data="ban:add_prompt"),
            InlineKeyboardButton(text="✅ Список банов / Разбан", callback_data="ban:list"),
        ],
        [
            InlineKeyboardButton(text="🏗 Автобан: Стройка", callback_data="aban:list:build"),
        ],
        [
            InlineKeyboardButton(text="⚔️ Автобан: Убийство", callback_data="aban:list:kill"),
        ],
    ])


@router.message(F.text == MenuBtn.ADMIN_BANS)
@router.callback_query(F.data == "ban:menu")
async def ban_admin_menu(event: Message | CallbackQuery, settings, state: FSMContext):
    await state.set_state(None)
    if not event.from_user or not _is_admin(event.from_user.id, settings):
        return

    text = (
        "🔨 <b>Управление банами и гео-автобаном</b>\n\n"
        "• <b>Ручной бан / разбан</b> — синхронизируется напрямую с панелью Nitrado.\n"
        "• <b>Автобан за стройку</b> — банит игроков (кроме админов) за установку построек в указанном радиусе.\n"
        "• <b>Автобан за убийство</b> — банит за PvP-убийство внутри запретной зоны."
    )
    markup = _main_ban_markup()

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "ban:add_prompt")
async def ban_add_prompt(callback: CallbackQuery, settings, state: FSMContext):
    if not _is_admin(callback.from_user.id, settings):
        return
    await state.set_state(BanAdminStates.wait_ban_nick)
    await callback.message.edit_text(
        "🚫 <b>Бан игрока</b>\n\n"
        "Введите <b>PSN-ник</b> игрока (точно как в игре) или <code>/cancel</code> для отмены:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BanAdminStates.wait_ban_nick)
async def ban_add_process(message: Message, db, settings, state: FSMContext, nitrado_api):
    if not _is_admin(message.from_user.id, settings):
        await state.set_state(None)
        return

    nick = (message.text or "").strip()
    if nick.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Бан отменён.", reply_markup=_main_ban_markup())
        return

    if len(nick) < 2 or len(nick) > 48:
        await message.answer("❌ Некорректный ник. Введите игровой ник или /cancel:")
        return

    await state.set_state(None)
    wait_msg = await message.answer("🔍 Проверяем игрока в базе и логах сервера...")

    # 1. Проверяем игрока в БД бота или в свежих .ADM логах сервера
    canonical_nick: str | None = None
    db_user = await db.get_user_by_nick(nick)
    if db_user:
        canonical_nick = str(db_user["game_nick"]).strip()
    else:
        player_id = await nitrado_api.find_player_id_in_logs(nick)
        if player_id:
            canonical_nick = nick

    if not canonical_nick:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Всё равно забанить", callback_data=f"ban:force:{nick[:40]}")],
            [InlineKeyboardButton(text="⬅️ В меню банов", callback_data="ban:menu")],
        ])
        await wait_msg.edit_text(
            f"❌ Игрок <code>{html.escape(nick)}</code> <b>не найден</b> ни в базе бота, ни в свежих логах сервера.\n\n"
            "Проверьте правильность написания ника или нажмите кнопку ниже, если игрок заходил давно.",
            reply_markup=markup,
            parse_mode="HTML",
        )
        return

    await wait_msg.edit_text(f"⏳ Отправляем бан для <code>{html.escape(canonical_nick)}</code> в Nitrado...", parse_mode="HTML")
    ok = await nitrado_api.ban_player(canonical_nick)

    if ok:
        await wait_msg.edit_text(
            f"⛔ Игрок <code>{html.escape(canonical_nick)}</code> успешно добавлен в бан-лист Nitrado!",
            reply_markup=_main_ban_markup(),
            parse_mode="HTML",
        )
    else:
        await wait_msg.edit_text(
            "❌ Не удалось добавить игрока в бан-лист Nitrado. Проверьте токен или статус сервера.",
            reply_markup=_main_ban_markup(),
        )


@router.callback_query(F.data.startswith("ban:force:"))
async def ban_force_process(callback: CallbackQuery, settings, nitrado_api):
    if not _is_admin(callback.from_user.id, settings):
        return

    _, _, nick = callback.data.split(":", 2)
    await callback.answer("⏳ Отправляем бан...")
    ok = await nitrado_api.ban_player(nick)

    if ok:
        await callback.message.edit_text(
            f"⛔ Игрок <code>{html.escape(nick)}</code> принудительно добавлен в бан-лист Nitrado!",
            reply_markup=_main_ban_markup(),
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            "❌ Не удалось добавить игрока в бан-лист Nitrado.",
            reply_markup=_main_ban_markup(),
        )


@router.callback_query(F.data == "ban:list")
async def ban_list_view(callback: CallbackQuery, settings, nitrado_api):
    if not _is_admin(callback.from_user.id, settings):
        return

    await callback.answer("⏳ Загружаем список банов...")
    bans = await nitrado_api.get_ban_list()

    rows = []
    for idx, identifier in enumerate(bans[:30]):
        rows.append([
            InlineKeyboardButton(
                text=f"✅ Разбанить: {identifier}",
                callback_data=f"ban:unban_idx:{idx}",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="ban:menu")])

    text = (
        f"📋 <b>Список заблокированных игроков ({len(bans)}):</b>\n"
        + ("Нажмите на игрока, чтобы снять бан:" if bans else "\n<i>Список банов пуст.</i>")
    )
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("ban:unban_idx:"))
async def ban_unban_by_index(callback: CallbackQuery, settings, nitrado_api):
    if not _is_admin(callback.from_user.id, settings):
        return

    _, _, idx_str = callback.data.split(":", 2)
    idx = int(idx_str)
    bans = await nitrado_api.get_ban_list()
    if idx < 0 or idx >= len(bans):
        await callback.answer("Запись не найдена, обновите список.", show_alert=True)
        return

    target = bans[idx]
    ok = await nitrado_api.unban_player(target)
    if ok:
        await callback.answer(f"✅ Игрок {target} разбанен!", show_alert=True)
    else:
        await callback.answer("❌ Ошибка при снятии бана в Nitrado.", show_alert=True)

    await ban_list_view(callback, settings, nitrado_api)


@router.callback_query(F.data.startswith("ban:unban_direct:"))
async def ban_unban_direct(callback: CallbackQuery, settings, nitrado_api):
    if not _is_admin(callback.from_user.id, settings):
        return

    _, _, target = callback.data.split(":", 2)
    ok = await nitrado_api.unban_player(target)
    if ok:
        await callback.answer(f"✅ Игрок {target} разбанен!", show_alert=True)
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                (callback.message.html_text or "") + "\n\n✅ <b>Разбанен администратором.</b>",
                parse_mode="HTML",
            )
    else:
        await callback.answer("❌ Ошибка при снятии бана в Nitrado.", show_alert=True)
