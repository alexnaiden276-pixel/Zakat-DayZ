import contextlib
import html
import logging
import math
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

    await wait_msg.edit_text(
        f"⏳ Отправляем бан для <code>{html.escape(canonical_nick)}</code> в Nitrado...",
        parse_mode="HTML",
    )
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


# ==================== ГЕО-АВТОБАНЫ (СТРОЙКА И УБИЙСТВА) ====================

def _kind_title(kind: str) -> str:
    return "🏗 Стройка" if kind == "build" else "⚔️ Убийство"


async def _render_zones_list(callback: CallbackQuery, db, kind: str) -> None:
    zones = await db.get_autoban_zones(kind)

    rows = []
    for z in zones:
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {z['name']} (X:{float(z['x']):.0f}, Z:{float(z['z']):.0f}, R:{float(z['radius']):.0f}м)",
                callback_data=f"aban:del:{kind}:{z['id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(text="➕ Добавить зону", callback_data=f"aban:add:{kind}")
    ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Назад в меню банов", callback_data="ban:menu")
    ])

    text = (
        f"📍 <b>Зоны автобана ({_kind_title(kind)}):</b>\n\n"
        + (
            "Нажмите на зону, чтобы <b>удалить</b> её, или добавьте новую:"
            if zones
            else "<i>Список зон пока пуст.</i>"
        )
    )
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("aban:list:"))
async def aban_list_view(callback: CallbackQuery, db, settings, state: FSMContext):
    if not _is_admin(callback.from_user.id, settings):
        return
    await state.set_state(None)
    _, _, kind = callback.data.split(":", 2)
    await _render_zones_list(callback, db, kind)
    await callback.answer()


@router.callback_query(F.data.startswith("aban:del:"))
async def aban_delete_zone(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, kind, zone_id_str = callback.data.split(":", 3)
    await db.delete_autoban_zone(int(zone_id_str))
    await callback.answer("🗑 Зона удалена.", show_alert=True)
    await _render_zones_list(callback, db, kind)


@router.callback_query(F.data.startswith("aban:add:"))
async def aban_add_start(callback: CallbackQuery, settings, state: FSMContext):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, kind = callback.data.split(":", 2)
    await state.update_data(zone_kind=kind)
    await state.set_state(BanAdminStates.wait_zone_name)

    await callback.message.edit_text(
        f"➕ <b>Новая зона автобана ({_kind_title(kind)})</b>\n\n"
        "Шаг 1/3: Введите <b>название зоны</b> (например: <code>Зелёная гора</code> или <code>Трейд СЗ</code>).\n"
        "Для отмены введите <code>/cancel</code>:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BanAdminStates.wait_zone_name)
async def aban_process_name(message: Message, settings, state: FSMContext):
    if not _is_admin(message.from_user.id, settings):
        await state.set_state(None)
        return

    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление зоны отменено.", reply_markup=_main_ban_markup())
        return

    if len(name) < 2 or len(name) > 50:
        await message.answer("❌ Название должно быть от 2 до 50 символов. Попробуйте ещё раз:")
        return

    await state.update_data(zone_name=name)
    await state.set_state(BanAdminStates.wait_zone_coords)
    await message.answer(
        "Шаг 2/3: Введите <b>координаты центра зоны (X и Z)</b> через пробел или запятую.\n"
        "Пример: <code>3715.5 5980.2</code> или <code>3715, 5980</code>\n"
        "<i>(Высота Y не нужна, только X и Z с iZurvive)</i>:",
        parse_mode="HTML",
    )


@router.message(BanAdminStates.wait_zone_coords)
async def aban_process_coords(message: Message, settings, state: FSMContext):
    if not _is_admin(message.from_user.id, settings):
        await state.set_state(None)
        return

    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление зоны отменено.", reply_markup=_main_ban_markup())
        return

    nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
    if len(nums) != 2:
        await message.answer(
            "❌ Нужно ввести ровно два числа (X и Z).\n"
            "Например: <code>4500.5 10200.0</code>",
            parse_mode="HTML",
        )
        return

    x_val = float(nums[0])
    z_val = float(nums[1])
    if not (math.isfinite(x_val) and math.isfinite(z_val) and 0 <= x_val <= 15500 and 0 <= z_val <= 15500):
        await message.answer(
            "❌ Координаты выходят за пределы карты (0 – 15500). Проверьте значения и введите снова:",
            parse_mode="HTML",
        )
        return

    await state.update_data(zone_x=x_val, zone_z=z_val)
    await state.set_state(BanAdminStates.wait_zone_radius)
    await message.answer(
        f"✅ Координаты приняты: <code>X: {x_val} | Z: {z_val}</code>\n\n"
        "Шаг 3/3: Введите <b>радиус зоны в метрах</b> (число, например: <code>150</code>):",
        parse_mode="HTML",
    )


@router.message(BanAdminStates.wait_zone_radius)
async def aban_process_radius(message: Message, db, settings, state: FSMContext):
    if not _is_admin(message.from_user.id, settings):
        await state.set_state(None)
        return

    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление зоны отменено.", reply_markup=_main_ban_markup())
        return

    try:
        radius = float(raw.replace(",", "."))
        if radius <= 0 or radius > 20000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное число от 1 до 20000 (в метрах):")
        return

    data = await state.get_data()
    kind = data["zone_kind"]
    name = data["zone_name"]
    x_val = data["zone_x"]
    z_val = data["zone_z"]

    await db.add_autoban_zone(
        kind=kind,
        name=name,
        x=x_val,
        z=z_val,
        radius=radius,
    )
    await state.set_state(None)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К списку зон", callback_data=f"aban:list:{kind}")],
        [InlineKeyboardButton(text="⬅️ В меню банов", callback_data="ban:menu")],
    ])
    await message.answer(
        f"✅ <b>Зона автобана создана!</b>\n\n"
        f"• <b>Тип:</b> {_kind_title(kind)}\n"
        f"• <b>Название:</b> {html.escape(name)}\n"
        f"• <b>Центр:</b> <code>X: {x_val} | Z: {z_val}</code>\n"
        f"• <b>Радиус:</b> <code>{radius:.0f} м</code>",
        reply_markup=markup,
        parse_mode="HTML",
    )
