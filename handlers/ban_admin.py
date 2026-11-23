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
async def ban_add_process(message: Message, settings, state: FSMContext, nitrado_api):
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
    wait_msg = await message.answer("⏳ Отправляем бан в Nitrado...")
    ok = await nitrado_api.ban_player(nick)

    if ok:
        await wait_msg.edit_text(
            f"⛔ Игрок <code>{html.escape(nick)}</code> успешно добавлен в бан-лист Nitrado!",
            reply_markup=_main_ban_markup(),
            parse_mode="HTML",
        )
    else:
        await wait_msg.edit_text(
            "❌ Не удалось добавить игрока в бан-лист Nitrado. Проверьте токен или статус сервера.",
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
@router.callback_query(F.data.startswith("aban:list:"))
async def autoban_zones_list(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    if not _is_admin(callback.from_user.id, settings):
        return

    _, _, kind = callback.data.split(":", 2)
    title = "🏗 Автобан за стройку" if kind == "build" else "⚔️ Автобан за убийство"
    zones = await db.get_autoban_zones(kind)

    rows = [
        [InlineKeyboardButton(text="➕ Добавить точку автобана", callback_data=f"aban:add:{kind}")]
    ]
    lines = [f"<b>{title}</b>\n"]

    if zones:
        for z in zones:
            lines.append(
                f"• <b>{html.escape(z['name'])}</b> — "
                f"<code>X: {z['x']} | Z: {z['z']}</code> (радиус <b>{int(z['radius'])} м</b>)"
            )
            rows.append([
                InlineKeyboardButton(
                    text=f"🗑 Удалить «{z['name']}»",
                    callback_data=f"aban:del:{kind}:{z['id']}",
                )
            ])
    else:
        lines.append("<i>Точки пока не добавлены.</i>")

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="ban:menu")])
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("aban:del:"))
async def autoban_zone_delete(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return

    _, _, kind, zone_id_str = callback.data.split(":", 3)
    await db.delete_autoban_zone(int(zone_id_str))
    await callback.answer("🗑 Точка автобана удалена.", show_alert=True)

    title = "🏗 Автобан за стройку" if kind == "build" else "⚔️ Автобан за убийство"
    zones = await db.get_autoban_zones(kind)
    rows = [[InlineKeyboardButton(text="➕ Добавить точку автобана", callback_data=f"aban:add:{kind}")]]
    lines = [f"<b>{title}</b>\n"]
    for z in zones:
        lines.append(
            f"• <b>{html.escape(z['name'])}</b> — "
            f"<code>X: {z['x']} | Z: {z['z']}</code> (радиус <b>{int(z['radius'])} м</b>)"
        )
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 Удалить «{z['name']}»",
                callback_data=f"aban:del:{kind}:{z['id']}",
            )
        ])
    if not zones:
        lines.append("<i>Точки пока не добавлены.</i>")
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="ban:menu")])
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("aban:add:"))
async def autoban_zone_add_start(callback: CallbackQuery, settings, state: FSMContext):
    if not _is_admin(callback.from_user.id, settings):
        return

    _, _, kind = callback.data.split(":", 2)
    await state.update_data(aban_kind=kind)
    await state.set_state(BanAdminStates.wait_zone_name)

    label = "стройки" if kind == "build" else "убийств"
    await callback.message.edit_text(
        f"➕ <b>Новая зона автобана ({label})</b>\n\n"
        "Введите <b>название точки</b> (например: <code>Трейд-зона</code> или <code>Бункер</code>) или /cancel:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(BanAdminStates.wait_zone_name)
async def autoban_zone_name(message: Message, settings, state: FSMContext):
    if not _is_admin(message.from_user.id, settings):
        await state.set_state(None)
        return

    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление зоны отменено.", reply_markup=_main_ban_markup())
        return
    if not name or len(name) > 40:
        await message.answer("❌ Название должно быть от 1 до 40 символов:")
        return

    await state.update_data(aban_name=name)
    await state.set_state(BanAdminStates.wait_zone_coords)
    await message.answer(
        "📍 Введите <b>координаты центра зоны (X и Z)</b> с карты iZurvive через пробел:\n"
        "Пример: <code>4512.5 10234.0</code>",
        parse_mode="HTML",
    )


@router.message(BanAdminStates.wait_zone_coords)
async def autoban_zone_coords(message: Message, settings, state: FSMContext):
    if not _is_admin(message.from_user.id, settings):
        await state.set_state(None)
        return

    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление зоны отменено.", reply_markup=_main_ban_markup())
        return

    nums = re.findall(r"\d+(?:\.\d+)?", raw.replace(",", "."))
    if len(nums) < 2:
        await message.answer("❌ Введите два числа (X и Z), например: <code>6540 8120</code>", parse_mode="HTML")
        return

    it = iter(nums)
    x = float(next(it))
    if len(nums) >= 3:
        next(it)
    z = float(next(it))

    if not (0 <= x <= 15360 and 0 <= z <= 15360):
        await message.answer("❌ Координаты должны быть в пределах карты (0 – 15360):")
        return

    await state.update_data(aban_x=x, aban_z=z)
    await state.set_state(BanAdminStates.wait_zone_radius)
    await message.answer(
        "📏 Введите <b>радиус действия зоны в метрах</b> (целое число, например: <code>150</code>):",
        parse_mode="HTML",
    )


@router.message(BanAdminStates.wait_zone_radius)
async def autoban_zone_radius(message: Message, db, settings, state: FSMContext):
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
        if radius <= 0 or radius > 5000:
            raise ValueError
    except ValueError:
        await message.answer("❌ Радиус должен быть числом от 1 до 5000 метров:")
        return

    data = await state.get_data()
    await state.set_state(None)
    kind = data.get("aban_kind")
    name = data.get("aban_name")
    x = data.get("aban_x")
    z = data.get("aban_z")

    if not kind or not name or x is None or z is None:
        await message.answer("❌ Сессия устарела. Начните заново.", reply_markup=_main_ban_markup())
        return

    await db.add_autoban_zone(kind=kind, name=name, x=x, z=z, radius=radius)
    kind_title = "стройка" if kind == "build" else "убийство"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку зон", callback_data=f"aban:list:{kind}")]
    ])
    await message.answer(
        f"✅ Точка автобана (<b>{kind_title}</b>) добавлена!\n\n"
        f"• <b>Название:</b> {html.escape(name)}\n"
        f"• <b>Координаты:</b> <code>X: {x} | Z: {z}</code>\n"
        f"• <b>Радиус:</b> <code>{int(radius)} м</code>",
        reply_markup=markup,
        parse_mode="HTML",
    )

