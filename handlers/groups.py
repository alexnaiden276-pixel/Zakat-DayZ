import asyncio
import contextlib
import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

router = Router()
logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))


class CreateGroup(StatesGroup):
    name = State()
    tag = State()
    description = State()
    flag = State()


class GroupManage(StatesGroup):
    wait_invite_nick = State()
    wait_kick_nick = State()
    wait_promote_nick = State()
    wait_new_desc = State()


class AdminFlags(StatesGroup):
    add_name = State()
    add_photo = State()
    rename_name = State()
    replace_photo = State()


def _format_msk_dt(dt_str: str | None) -> str:
    """Переводит SQLite UTC строку 'YYYY-MM-DD HH:MM:SS' в 'ДД.ММ.ГГГГ ЧЧ:ММ' по МСК."""
    if not dt_str:
        return datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    try:
        dt_utc = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(dt_str)


async def _build_group_topic_card(db, settings, group_id: int) -> tuple[str, str | None] | None:
    """Формирует текст карточки группировки строго по шаблону и возвращает (text, photo_file_id)."""
    group = await db.fetchone("SELECT * FROM groups WHERE id = ?", (group_id,))
    if not group:
        return None

    members = await db.get_group_members(group_id)
    safe_name = html.escape(str(group["name"]))
    server_title = html.escape(getattr(settings, "server_display_name", "! Zakat Dayz"))
    flag_name = str(group["flag_name"] or "Без флага")
    safe_flag = html.escape(flag_name)

    created_str = _format_msk_dt(group["created_at"])
    updated_str = _format_msk_dt(group["updated_at"] or group["created_at"])

    photo_id = await db.get_group_flag_photo(flag_name)
    max_len = 1000 if photo_id else 4000

    header = (
        f"🏴 <b>Группировка:</b> {safe_name}\n"
        f"🎮 {server_title}\n"
        f"👥 Участников: {len(members)}\n"
        f"🏳️ Флаг: {safe_flag}\n\n"
        f"<b>Состав:</b>\n"
    )
    footer = f"\n\nСоздан: {created_str} / Изменён: {updated_str}"

    if not members:
        text = f"{header}• <i>Нет участников</i>{footer}"
        return text, photo_id

    member_lines: list[str] = []
    for idx, m in enumerate(members):
        role_title = {
            "leader": "Глава",
            "officer": "Заместитель",
            "player": "Боец",
        }.get(m["role"], "Боец")
        line = f"• {html.escape(str(m['game_nick']))} — {role_title}"

        candidate_lines = member_lines + [line]
        remaining = len(members) - (idx + 1)
        overflow_note = f"\n<i>...и ещё {remaining} бойцов</i>" if remaining > 0 else ""
        candidate_text = header + "\n".join(candidate_lines) + overflow_note + footer

        if len(candidate_text) > max_len and member_lines:
            left_count = len(members) - idx
            member_lines.append(f"<i>...и ещё {left_count} бойцов</i>")
            break
        member_lines.append(line)

    text = header + "\n".join(member_lines) + footer
    return text, photo_id
async def sync_group_topic_post(bot: Bot, db, settings, group_id: int) -> None:
    """Публикует новую или редактирует существующую карточку группировки в теме Telegram-группы."""
    chat_id = getattr(settings, "groups_chat_id", None)
    thread_id = getattr(settings, "groups_thread_id", None)
    if not chat_id:
        return

    await db.execute(
        "UPDATE groups SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (group_id,),
    )

    built = await _build_group_topic_card(db, settings, group_id)
    if not built:
        return
    text, photo_id = built

    group = await db.fetchone(
        "SELECT topic_message_id, topic_has_photo FROM groups WHERE id = ?",
        (group_id,),
    )
    msg_id = group["topic_message_id"] if group else None
    had_photo = bool(group["topic_has_photo"]) if group else False

    if msg_id and ((photo_id and had_photo) or (not photo_id and not had_photo)):
        try:
            if photo_id:
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=msg_id,
                    media=InputMediaPhoto(media=photo_id, caption=text, parse_mode="HTML"),
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=text,
                    parse_mode="HTML",
                )
            return
        except TelegramAPIError as e:
            if "message is not modified" in str(e).lower():
                return
            logger.warning("[GROUPS] Не удалось отредактировать карточку клана #%s (%s), пересоздаём...", group_id, e)

    if msg_id:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)

    try:
        if photo_id:
            sent = await bot.send_photo(
                chat_id=chat_id,
                message_thread_id=thread_id,
                photo=photo_id,
                caption=text,
                parse_mode="HTML",
            )
            has_photo_flag = 1
        else:
            sent = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
            )
            has_photo_flag = 0

        await db.execute(
            "UPDATE groups SET topic_message_id = ?, topic_has_photo = ? WHERE id = ?",
            (sent.message_id, has_photo_flag, group_id),
        )
    except Exception as send_err:
        logger.error("[GROUPS] Ошибка публикации карточки клана #%s в тему: %s", group_id, send_err)


async def _sync_all_groups_with_flag(bot: Bot, db, settings, flag_name: str) -> None:
    """Обновляет карточки в теме у всех группировок, использующих указанный флаг."""
    affected = await db.fetchall(
        "SELECT id FROM groups WHERE flag_name = ? COLLATE NOCASE",
        (flag_name,),
    )
    for idx, row in enumerate(affected):
        await sync_group_topic_post(bot, db, settings, int(row["id"]))
        if idx + 1 < len(affected):
            await asyncio.sleep(0.4)


# ==========================================
# АДМИН-ПАНЕЛЬ УПРАВЛЕНИЯ ФЛАГАМИ
# ==========================================

@router.message(F.text == "🏳️ Управление флагами")
@router.message(Command("flags"))
@router.callback_query(F.data == "aflags:menu")
async def admin_flags_menu(event: Message | CallbackQuery, db, settings, state: FSMContext):
    if state:
        await state.clear()

    if not event.from_user or event.from_user.id not in settings.admin_ids:
        return

    flags = await db.get_group_flags_list()
    rows = [
        [InlineKeyboardButton(text="➕ Добавить новый флаг", callback_data="aflags:add")]
    ]
    for fl in flags:
        icon = "🖼" if fl["photo_file_id"] else "🏳️"
        rows.append([
            InlineKeyboardButton(text=f"{icon} {fl['name']}", callback_data=f"aflags:item:{fl['id']}")
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = (
        "🏳️ <b>Управление флагами группировок</b>\n\n"
        "• 🖼 — флаг с привязанной картинкой\n"
        "• 🏳️ — флаг без картинки\n\n"
        "Нажмите на флаг, чтобы переименовать его, загрузить картинку или удалить:"
    )

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
# --- ДОБАВЛЕНИЕ ФЛАГА ---

@router.callback_query(F.data == "aflags:add")
async def aflags_add_start(callback: CallbackQuery, state: FSMContext, settings):
    if callback.from_user.id not in settings.admin_ids:
        return
    await state.set_state(AdminFlags.add_name)
    await callback.message.edit_text(
        "Введите <b>название нового флага</b> (например: <code>Бандиты</code>, <code>Орёл</code>) или /cancel:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminFlags.add_name)
async def aflags_add_name(message: Message, state: FSMContext, db):
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.clear()
        await message.answer("Добавление флага отменено.")
        return
    if not name or len(name) > 32 or name.lower() == "без флага":
        await message.answer("❌ Введите корректное название флага (от 1 до 32 символов):")
        return

    existing = await db.fetchone("SELECT id FROM group_flags WHERE name = ? COLLATE NOCASE", (name,))
    if existing:
        await message.answer("❌ Флаг с таким названием уже существует. Введите другое название:")
        return

    await state.update_data(new_flag_name=name)
    await state.set_state(AdminFlags.add_photo)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Создать без картинки", callback_data="aflags:add_skip_photo")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="aflags:menu")],
    ])
    await message.answer(
        f"Теперь отправьте <b>картинку (фото)</b> для флага <b>«{html.escape(name)}»</b> "
        "или нажмите кнопку ниже, чтобы создать без картинки:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(AdminFlags.add_photo, F.data == "aflags:add_skip_photo")
async def aflags_add_skip_photo(callback: CallbackQuery, state: FSMContext, db):
    data = await state.get_data()
    name = data.get("new_flag_name")
    await state.clear()
    if not name:
        await callback.answer("Сессия устарела, попробуйте снова.", show_alert=True)
        return

    try:
        await db.add_group_flag(name, photo_file_id=None)
    except Exception:
        await callback.answer("Ошибка: флаг уже существует.", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку флагов", callback_data="aflags:menu")]
    ])
    await callback.message.edit_text(
        f"✅ Флаг <b>«{html.escape(name)}»</b> добавлен (без картинки)!",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminFlags.add_photo)
async def aflags_add_photo_save(message: Message, state: FSMContext, db):
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear()
        await message.answer("Добавление флага отменено.")
        return

    if not message.photo:
        await message.answer(
            "❌ Пожалуйста, отправьте именно <b>фотографию</b> флага (или нажмите «Создать без картинки»):",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    name = data.get("new_flag_name")
    await state.clear()
    if not name:
        await message.answer("❌ Сессия устарела. Откройте /flags и начните заново.")
        return

    photo_id = message.photo[-1].file_id
    try:
        await db.add_group_flag(name, photo_file_id=photo_id)
    except Exception:
        await message.answer("❌ Не удалось добавить флаг (возможно, такое название уже занято).")
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку флагов", callback_data="aflags:menu")]
    ])
    await message.answer(
        f"✅ Флаг <b>«{html.escape(name)}»</b> с картинкой успешно добавлен!",
        reply_markup=markup,
        parse_mode="HTML",
    )


# --- ПРОСМОТР, ПЕРЕИМЕНОВАНИЕ, ЗАМЕНА ФОТО И УДАЛЕНИЕ ФЛАГА ---

async def _render_flag_detail(callback: CallbackQuery, db, flag_id: int) -> bool:
    fl = await db.get_group_flag_by_id(flag_id)
    if not fl:
        await callback.answer("Флаг не найден.", show_alert=True)
        return False

    has_photo = "Привязана ✅" if fl["photo_file_id"] else "Нет ❌"
    text = (
        f"🏳️ <b>Управление флагом</b>\n\n"
        f"Название: <b>{html.escape(fl['name'])}</b>\n"
        f"Картинка: <b>{has_photo}</b>"
    )
    rows = [
        [InlineKeyboardButton(text="✏️ Переименовать флаг", callback_data=f"aflags:rename:{flag_id}")],
        [InlineKeyboardButton(text="🖼 Загрузить / заменить картинку", callback_data=f"aflags:photo:{flag_id}")],
    ]
    if fl["photo_file_id"]:
        rows.append([InlineKeyboardButton(text="🧹 Убрать картинку (оставить текст)", callback_data=f"aflags:clear_photo:{flag_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить флаг", callback_data=f"aflags:del_ask:{flag_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К списку флагов", callback_data="aflags:menu")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    return True
@router.callback_query(F.data.startswith("aflags:item:"))
async def aflags_item_detail(callback: CallbackQuery, db, settings):
    if callback.from_user.id not in settings.admin_ids:
        return

    flag_id = int(callback.data.split(":", 2)[2])
    if await _render_flag_detail(callback, db, flag_id):
        await callback.answer()


@router.callback_query(F.data.startswith("aflags:rename:"))
async def aflags_rename_prompt(callback: CallbackQuery, state: FSMContext, settings):
    if callback.from_user.id not in settings.admin_ids:
        return
    flag_id = int(callback.data.split(":", 2)[2])
    await state.update_data(edit_flag_id=flag_id)
    await state.set_state(AdminFlags.rename_name)
    await callback.message.edit_text("Введите <b>новое название флага</b> (или /cancel):", parse_mode="HTML")
    await callback.answer()


@router.message(AdminFlags.rename_name)
async def aflags_rename_save(message: Message, state: FSMContext, db, bot: Bot, settings):
    new_name = (message.text or "").strip()
    if new_name.lower() == "/cancel":
        await state.clear()
        await message.answer("Переименование отменено.")
        return
    if not new_name or len(new_name) > 32 or new_name.lower() == "без флага":
        await message.answer("❌ Введите корректное название (от 1 до 32 символов):")
        return

    data = await state.get_data()
    flag_id = data.get("edit_flag_id")
    await state.clear()
    if not flag_id:
        await message.answer("❌ Сессия устарела. Откройте /flags и попробуйте снова.")
        return

    try:
        old_name = await db.rename_group_flag(flag_id, new_name)
    except Exception:
        await message.answer("❌ Флаг с таким названием уже существует.")
        return

    if old_name:
        await _sync_all_groups_with_flag(bot, db, settings, new_name)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К флагу", callback_data=f"aflags:item:{flag_id}")]
    ])
    await message.answer(
        f"✅ Флаг переименован в <b>«{html.escape(new_name)}»</b>!",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("aflags:photo:"))
async def aflags_photo_prompt(callback: CallbackQuery, state: FSMContext, settings):
    if callback.from_user.id not in settings.admin_ids:
        return
    flag_id = int(callback.data.split(":", 2)[2])
    await state.update_data(edit_flag_id=flag_id)
    await state.set_state(AdminFlags.replace_photo)
    await callback.message.edit_text("Отправьте <b>новую картинку (фото)</b> для этого флага (или /cancel):", parse_mode="HTML")
    await callback.answer()


@router.message(AdminFlags.replace_photo)
async def aflags_photo_save(message: Message, state: FSMContext, db, bot: Bot, settings):
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear()
        await message.answer("Загрузка картинки отменена.")
        return

    if not message.photo:
        await message.answer("❌ Пожалуйста, отправьте именно <b>фотографию</b> флага (или /cancel):", parse_mode="HTML")
        return

    data = await state.get_data()
    flag_id = data.get("edit_flag_id")
    await state.clear()
    if not flag_id:
        await message.answer("❌ Сессия устарела. Откройте /flags и попробуйте снова.")
        return

    photo_id = message.photo[-1].file_id
    await db.update_group_flag_photo(flag_id, photo_id)

    fl = await db.get_group_flag_by_id(flag_id)
    if fl:
        await _sync_all_groups_with_flag(bot, db, settings, fl["name"])

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К флагу", callback_data=f"aflags:item:{flag_id}")]
    ])
    await message.answer("✅ Картинка флага обновлена!", reply_markup=markup)


@router.callback_query(F.data.startswith("aflags:clear_photo:"))
async def aflags_clear_photo(callback: CallbackQuery, db, bot: Bot, settings):
    if callback.from_user.id not in settings.admin_ids:
        return
    flag_id = int(callback.data.split(":", 2)[2])
    await db.update_group_flag_photo(flag_id, None)

    fl = await db.get_group_flag_by_id(flag_id)
    if fl:
        await _sync_all_groups_with_flag(bot, db, settings, fl["name"])

    await callback.answer("🧹 Картинка отвязана от флага.", show_alert=True)
    await _render_flag_detail(callback, db, flag_id)


@router.callback_query(F.data.startswith("aflags:del_ask:"))
async def aflags_delete_confirm(callback: CallbackQuery, db, settings):
    if callback.from_user.id not in settings.admin_ids:
        return
    flag_id = int(callback.data.split(":", 2)[2])
    fl = await db.get_group_flag_by_id(flag_id)
    if not fl:
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"aflags:del_yes:{flag_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"aflags:item:{flag_id}"),
    ]])
    await callback.message.edit_text(
        f"⚠️ Удалить флаг <b>«{html.escape(fl['name'])}»</b>?\n"
        "У группировок с этим флагом он автоматически изменится на «Без флага».",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("aflags:del_yes:"))
async def aflags_delete_execute(callback: CallbackQuery, db, bot: Bot, settings, state: FSMContext):
    if callback.from_user.id not in settings.admin_ids:
        return
    flag_id = int(callback.data.split(":", 2)[2])
    fl = await db.get_group_flag_by_id(flag_id)
    affected_ids = []
    if fl:
        rows = await db.fetchall(
            "SELECT id FROM groups WHERE flag_name = ? COLLATE NOCASE",
            (fl["name"],),
        )
        affected_ids = [int(r["id"]) for r in rows]

    old_name = await db.delete_group_flag(flag_id)
    if old_name:
        for idx, gid in enumerate(affected_ids):
            await sync_group_topic_post(bot, db, settings, gid)
            if idx + 1 < len(affected_ids):
                await asyncio.sleep(0.4)

    await callback.answer("🗑 Флаг удалён!", show_alert=True)
    await admin_flags_menu(callback, db, settings, state)
# ==========================================
# КАРУСЕЛЬ ВЫБОРА ФЛАГА С ПРЕДПРОСМОТРОМ КАРТИНОК
# ==========================================

async def _get_flag_carousel_items(db) -> list[dict]:
    """Собирает список флагов из БД + вариант 'Без флага' в конец."""
    db_flags = await db.get_group_flags_list()
    items = [
        {
            "id": int(fl["id"]),
            "name": str(fl["name"]),
            "photo_file_id": fl["photo_file_id"],
        }
        for fl in db_flags
    ]
    items.append({"id": 0, "name": "Без флага", "photo_file_id": None})
    return items


def _build_flag_carousel_markup(
    items: list[dict], index: int, callback_prefix: str, back_callback: str
) -> InlineKeyboardMarkup:
    total = len(items)
    current = items[index]
    prev_idx = (index - 1) % total
    next_idx = (index + 1) % total

    rows = []
    if total > 1:
        rows.append([
            InlineKeyboardButton(text="⬅️", callback_data=f"{callback_prefix}:page:{prev_idx}"),
            InlineKeyboardButton(text=f"{index + 1} / {total}", callback_data=f"{callback_prefix}:noop"),
            InlineKeyboardButton(text="➡️", callback_data=f"{callback_prefix}:page:{next_idx}"),
        ])

    rows.append([
        InlineKeyboardButton(
            text=f"✅ Выбрать: {current['name']}",
            callback_data=f"{callback_prefix}:pick:{current['id']}",
        )
    ])
    rows.append([
        InlineKeyboardButton(text="❌ Отмена / Назад", callback_data=back_callback)
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_or_update_flag_carousel(
    target: Message | CallbackQuery,
    db,
    index: int,
    callback_prefix: str,
    back_callback: str,
    title: str = "🏳️ <b>Выберите флаг вашей группировки:</b>",
) -> None:
    items = await _get_flag_carousel_items(db)
    index = index % len(items)
    current = items[index]
    markup = _build_flag_carousel_markup(items, index, callback_prefix, back_callback)

    photo_id = current["photo_file_id"]
    photo_status = "🖼 <i>Изображение флага прикреплено</i>" if photo_id else "ℹ️ <i>Флаг без изображения</i>"
    caption = (
        f"{title}\n\n"
        f"Текущий вариант: <b>{html.escape(current['name'])}</b>\n"
        f"{photo_status}"
    )

    if isinstance(target, Message):
        if photo_id:
            await target.answer_photo(photo=photo_id, caption=caption, reply_markup=markup, parse_mode="HTML")
        else:
            await target.answer(text=caption, reply_markup=markup, parse_mode="HTML")
        return

    msg = target.message
    had_photo = bool(msg.photo)

    try:
        if photo_id and had_photo:
            await msg.edit_media(
                media=InputMediaPhoto(media=photo_id, caption=caption, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        if not photo_id and not had_photo:
            await msg.edit_text(text=caption, reply_markup=markup, parse_mode="HTML")
            return
    except TelegramAPIError as e:
        if "message is not modified" in str(e).lower():
            return

    with contextlib.suppress(Exception):
        await msg.delete()

    if photo_id:
        await msg.answer_photo(photo=photo_id, caption=caption, reply_markup=markup, parse_mode="HTML")
    else:
        await msg.answer(text=caption, reply_markup=markup, parse_mode="HTML")


# ==========================================
# ГЛАВНОЕ МЕНЮ ГРУППИРОВКИ И СПИСОК КЛАНОВ
# ==========================================

@router.callback_query(F.data == "group")
async def group_menu(callback: CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.clear()

    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    if not user["group_id"]:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать группировку", callback_data="group:create")],
            [InlineKeyboardButton(text="📋 Список группировок сервера", callback_data="group:list")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ])
        text = (
            "👥 <b>Группировки</b>\n\n"
            "Вы пока не состоите ни в одной группировке.\n"
            "Вы можете создать свой клан или посмотреть список существующих:"
        )

        if callback.id == "0":
            await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
            await callback.answer()
        return

    group = await db.fetchone("SELECT * FROM groups WHERE id = ?", (user["group_id"],))
    if not group:
        await db.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE id = ?", (user["id"],))
        await group_menu(callback, db, state)
        return

    members = await db.get_group_members(user["group_id"])
    safe_name = html.escape(group["name"])
    safe_tag = html.escape(group["tag"])
    safe_flag = html.escape(group["flag_name"] or "Без флага")
    safe_desc = html.escape(group["description"] or "Описание не задано.")

    lines = [
        f"🏴 <b>{safe_name} [{safe_tag}]</b>",
        f"🏳️ Флаг: <b>{safe_flag}</b>",
        f"<i>{safe_desc}</i>",
        "",
        f"<b>Состав ({len(members)} чел.):</b>",
    ]
    for member in members:
        role_badge = {
            "leader": "👑 Глава",
            "officer": "🎖 Заместитель",
            "player": "👤 Боец",
        }.get(member["role"], "👤 Боец")
        lines.append(f"• <code>{html.escape(member['game_nick'])}</code> — {role_badge}")
    rows = []
    if user["role"] in ("leader", "officer"):
        rows.append([
            InlineKeyboardButton(text="➕ Пригласить игрока", callback_data="group:add"),
            InlineKeyboardButton(text="🗑 Исключить", callback_data="group:remove"),
        ])
    if user["role"] == "leader":
        rows.append([
            InlineKeyboardButton(text="🎖 Должности", callback_data="group:promote"),
            InlineKeyboardButton(text="✏️ Описание", callback_data="group:edit_desc"),
        ])
        rows.append([
            InlineKeyboardButton(text="🏳️ Сменить флаг", callback_data="group:change_flag"),
            InlineKeyboardButton(text="💥 Распустить клан", callback_data="group:dissolve_ask"),
        ])
    else:
        rows.append([InlineKeyboardButton(text="🚪 Покинуть группировку", callback_data="group:leave_ask")])

    rows.append([InlineKeyboardButton(text="📋 Все группировки сервера", callback_data="group:list")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])

    text_content = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)

    if callback.id == "0":
        await callback.message.answer(text_content, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text_content, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data == "group:list")
async def group_list(callback: CallbackQuery, db):
    groups = await db.fetchall(
        """SELECT g.id, g.name, g.tag, g.flag_name, u.game_nick AS leader_nick,
                  (SELECT COUNT(*) FROM users WHERE group_id = g.id) AS members_count
           FROM groups g
           LEFT JOIN users u ON g.leader_id = u.id
           ORDER BY members_count DESC, g.name ASC
           LIMIT 50"""
    )

    if not groups:
        text = "📋 <b>Группировки сервера</b>\n\n<i>На сервере пока не создано ни одной группировки.</i>"
    else:
        lines = ["📋 <b>Группировки сервера:</b>\n"]
        for idx, g in enumerate(groups, start=1):
            leader = html.escape(g["leader_nick"] or "Неизвестно")
            flag = html.escape(g["flag_name"] or "Без флага")
            lines.append(
                f"{idx}. <b>{html.escape(g['name'])} [{html.escape(g['tag'])}]</b> — "
                f"{g['members_count']} чел. | 🏳️ {flag} (Глава: <code>{leader}</code>)"
            )
        text = "\n".join(lines)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="group")]
    ])
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


# ==========================================
# СОЗДАНИЕ ГРУППИРОВКИ И ВЫБОР ФЛАГА
# ==========================================

@router.callback_query(F.data == "group:create")
async def create_start(callback: CallbackQuery, state: FSMContext, db):
    user = await db.get_user(callback.from_user.id)
    if user and user["group_id"]:
        await callback.answer("Вы уже состоите в группировке!", show_alert=True)
        return

    await state.set_state(CreateGroup.name)
    await callback.message.answer(
        "Введите <b>название группировки</b> (от 3 до 40 символов) или /cancel для отмены:",
        parse_mode="HTML",
    )
    if callback.id != "0":
        await callback.answer()


@router.message(CreateGroup.name)
async def create_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.clear()
        await message.answer("Создание группировки отменено.")
        return
    if not 3 <= len(name) <= 40:
        await message.answer("❌ Название должно содержать от 3 до 40 символов.")
        return

    await state.update_data(name=name)
    await state.set_state(CreateGroup.tag)
    await message.answer(
        "Введите <b>тег группировки</b> (2–8 букв или цифр, например: <code>RUSH</code>):",
        parse_mode="HTML",
    )


@router.message(CreateGroup.tag)
async def create_tag(message: Message, state: FSMContext):
    tag = (message.text or "").strip().upper()
    if tag.lower() == "/cancel":
        await state.clear()
        await message.answer("Создание группировки отменено.")
        return
    if not 2 <= len(tag) <= 8 or not tag.isalnum():
        await message.answer("❌ Тег должен содержать 2–8 букв или цифр без пробелов и спецсимволов.")
        return

    await state.update_data(tag=tag)
    await state.set_state(CreateGroup.description)
    await message.answer("Введите <b>описание группировки</b> (до 500 символов):", parse_mode="HTML")


@router.message(CreateGroup.description)
async def create_description(message: Message, state: FSMContext, db):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Создание группировки отменено.")
        return

    await state.update_data(description=raw[:500])
    await state.set_state(CreateGroup.flag)

    await _send_or_update_flag_carousel(
        target=message,
        db=db,
        index=0,
        callback_prefix="gflag",
        back_callback="gflag:cancel",
    )


@router.callback_query(CreateGroup.flag, F.data.startswith("gflag:"))
async def create_choose_flag(callback: CallbackQuery, state: FSMContext, db, bot: Bot, settings):
    parts = callback.data.split(":", 2)
    action = parts

    if action == "noop":
        await callback.answer()
        return

    if action == "page":
        await _send_or_update_flag_carousel(
            target=callback,
            db=db,
            index=int(parts),
            callback_prefix="gflag",
            back_callback="gflag:cancel",
        )
        await callback.answer()
        return

    async def _reply_clean(text: str, markup: InlineKeyboardMarkup | None = None):
        if callback.message.photo:
            with contextlib.suppress(Exception):
                await callback.message.delete()
            await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
        else:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")

    if action == "cancel":
        await state.clear()
        await _reply_clean("❌ Создание группировки отменено.")
        await callback.answer()
        return

    if action != "pick":
        await callback.answer()
        return

    data = await state.get_data()
    if not all(k in data for k in ("name", "tag", "description")):
        await state.clear()
        await _reply_clean("❌ Сессия создания группировки устарела. Начните заново через меню.")
        await callback.answer()
        return

    flag_id = int(parts)
    if flag_id == 0:
        flag_name = "Без флага"
    else:
        fl = await db.get_group_flag_by_id(flag_id)
        flag_name = str(fl["name"]) if fl else "Без флага"

    user = await db.get_user(callback.from_user.id)
    if not user or user["group_id"]:
        await state.clear()
        await _reply_clean("❌ Вы уже состоите в группировке.")
        await callback.answer()
        return

    try:
        async with db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                """INSERT INTO groups(name, tag, description, leader_id, flag_name)
                   VALUES (?, ?, ?, ?, ?)""",
                (data["name"], data["tag"], data["description"], user["id"], flag_name),
            )
            group_id = cursor.lastrowid
            await conn.execute(
                "UPDATE users SET group_id = ?, role = 'leader' WHERE id = ?",
                (group_id, user["id"]),
            )
            await conn.commit()
    except Exception:
        await state.clear()
        await _reply_clean("❌ Не удалось создать группировку. Возможно, название или тег уже заняты.")
        await callback.answer()
        return

    await state.clear()
    await sync_group_topic_post(bot, db, settings, group_id)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Открыть меню группировки", callback_data="group")]
    ])
    await _reply_clean(
        f"🎉 Группировка <b>{html.escape(data['name'])} [{html.escape(data['tag'])}]</b> создана!\n"
        f"🏳️ Выбранный флаг: <b>{html.escape(flag_name)}</b>\n"
        f"📢 Информация о группировке опубликована в теме сервера.",
        markup=markup,
    )
    await callback.answer()
# ==========================================
# ПРИГЛАШЕНИЕ, ИСКЛЮЧЕНИЕ И ДОЛЖНОСТИ
# ==========================================

@router.callback_query(F.data == "group:add")
async def group_add_prompt(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] not in ("leader", "officer"):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    await state.set_state(GroupManage.wait_invite_nick)
    await callback.message.answer("Введите <b>игровой ник</b> игрока, которого хотите пригласить (или /cancel):", parse_mode="HTML")
    await callback.answer()


@router.message(GroupManage.wait_invite_nick)
async def group_add_process(message: Message, state: FSMContext, db, bot: Bot):
    nick = (message.text or "").strip()
    if nick.lower() == "/cancel":
        await state.clear()
        await message.answer("Приглашение отменено.")
        return

    actor = await db.get_user(message.from_user.id)
    if not actor or not actor["group_id"] or actor["role"] not in ("leader", "officer"):
        await state.clear()
        return

    target = await db.get_user_by_nick(nick)
    if not target:
        await message.answer("❌ Игрок с таким ником не зарегистрирован в боте. Попробуйте снова или /cancel:")
        return
    if target["id"] == actor["id"]:
        await message.answer("❌ Нельзя пригласить самого себя.")
        return
    if target["group_id"]:
        await state.clear()
        await message.answer(f"❌ Игрок <code>{html.escape(target['game_nick'])}</code> уже состоит в группировке.", parse_mode="HTML")
        return

    group = await db.fetchone("SELECT * FROM groups WHERE id = ?", (actor["group_id"],))
    await state.clear()

    invite_markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Вступить", callback_data=f"ginv:accept:{group['id']}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ginv:decline:{group['id']}"),
    ]])
    back_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В группировку", callback_data="group")]
    ])

    try:
        await bot.send_message(
            chat_id=target["telegram_id"],
            text=(
                f"📩 <b>Приглашение в группировку!</b>\n\n"
                f"Игрок <code>{html.escape(actor['game_nick'])}</code> приглашает вас в клан "
                f"<b>{html.escape(group['name'])} [{html.escape(group['tag'])}]</b>."
            ),
            reply_markup=invite_markup,
            parse_mode="HTML",
        )
        await message.answer(
            f"✅ Приглашение отправлено игроку <code>{html.escape(target['game_nick'])}</code>!",
            reply_markup=back_markup,
            parse_mode="HTML",
        )
    except Exception:
        await message.answer(
            "❌ Не удалось отправить приглашение (возможно, игрок заблокировал бота).",
            reply_markup=back_markup,
        )


@router.callback_query(F.data.startswith("ginv:"))
async def group_invite_response(callback: CallbackQuery, db, bot: Bot, settings):
    _, decision, group_id_str = callback.data.split(":", 2)
    group_id = int(group_id_str)
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    if decision == "decline":
        await callback.message.edit_text("❌ Вы отклонили приглашение в группировку.")
        await callback.answer()
        return

    if user["group_id"]:
        await callback.message.edit_text("❌ Вы уже состоите в другой группировке.")
        await callback.answer()
        return

    group = await db.fetchone("SELECT * FROM groups WHERE id = ?", (group_id,))
    if not group:
        await callback.message.edit_text("❌ Эта группировка уже распущена.")
        await callback.answer()
        return

    await db.execute(
        "UPDATE users SET group_id = ?, role = 'player' WHERE id = ?",
        (group_id, user["id"]),
    )
    await sync_group_topic_post(bot, db, settings, group_id)

    await callback.message.edit_text(
        f"🎉 Вы вступили в группировку <b>{html.escape(group['name'])} [{html.escape(group['tag'])}]</b>!",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "group:remove")
async def group_remove_prompt(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] not in ("leader", "officer"):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    await state.set_state(GroupManage.wait_kick_nick)
    await callback.message.answer("Введите <b>игровой ник</b> участника для исключения (или /cancel):", parse_mode="HTML")
    await callback.answer()


@router.message(GroupManage.wait_kick_nick)
async def group_remove_process(message: Message, state: FSMContext, db, bot: Bot, settings):
    nick = (message.text or "").strip()
    if nick.lower() == "/cancel":
        await state.clear()
        await message.answer("Исключение отменено.")
        return

    actor = await db.get_user(message.from_user.id)
    if not actor or not actor["group_id"] or actor["role"] not in ("leader", "officer"):
        await state.clear()
        return

    target = await db.get_user_by_nick(nick)
    if not target or target["group_id"] != actor["group_id"]:
        await message.answer("❌ Игрок не найден среди участников вашей группировки. Попробуйте снова или /cancel:")
        return
    if target["id"] == actor["id"]:
        await message.answer("❌ Нельзя исключить самого себя.")
        return
    if target["role"] == "leader" or (actor["role"] == "officer" and target["role"] == "officer"):
        await state.clear()
        await message.answer("❌ Недостаточно прав для исключения этого участника.")
        return

    group_id = actor["group_id"]
    await db.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE id = ?", (target["id"],))
    await state.clear()
    await sync_group_topic_post(bot, db, settings, group_id)

    await message.answer(f"🗑 Игрок <code>{html.escape(target['game_nick'])}</code> исключён из группировки.", parse_mode="HTML")


@router.callback_query(F.data == "group:promote")
async def group_promote_prompt(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await callback.answer("Назначать должности может только глава.", show_alert=True)
        return

    await state.set_state(GroupManage.wait_promote_nick)
    await callback.message.answer("Введите <b>игровой ник</b> участника для смены должности (или /cancel):", parse_mode="HTML")
    await callback.answer()


@router.message(GroupManage.wait_promote_nick)
async def group_promote_process(message: Message, state: FSMContext, db):
    nick = (message.text or "").strip()
    if nick.lower() == "/cancel":
        await state.clear()
        await message.answer("Смена должности отменена.")
        return

    actor = await db.get_user(message.from_user.id)
    if not actor or not actor["group_id"] or actor["role"] != "leader":
        await state.clear()
        return

    target = await db.get_user_by_nick(nick)
    if not target or target["group_id"] != actor["group_id"]:
        await message.answer("❌ Игрок не найден среди участников вашей группировки. Попробуйте снова или /cancel:")
        return
    if target["id"] == actor["id"]:
        await message.answer("❌ Нельзя изменить должность самому себе.")
        return

    await state.clear()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎖 Заместитель", callback_data=f"role:officer:{target['id']}")],
        [InlineKeyboardButton(text="👤 Боец", callback_data=f"role:player:{target['id']}")],
    ])
    await message.answer(
        f"Выберите новую должность для <code>{html.escape(target['game_nick'])}</code>:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("role:"))
async def set_group_role(callback: CallbackQuery, db, bot: Bot, settings):
    _, role, target_id_str = callback.data.split(":", 2)
    actor = await db.get_user(callback.from_user.id)
    target_id = int(target_id_str)

    if not actor or actor["role"] != "leader":
        await callback.answer("Только глава может назначать должности.", show_alert=True)
        return

    target = await db.fetchone("SELECT * FROM users WHERE id = ?", (target_id,))
    if not target or target["group_id"] != actor["group_id"] or role not in ("officer", "player"):
        await callback.answer("Некорректный участник или должность.", show_alert=True)
        return

    await db.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))
    await sync_group_topic_post(bot, db, settings, actor["group_id"])

    role_name = "Заместитель" if role == "officer" else "Боец"
    await callback.message.edit_text(
        f"✅ Игроку <code>{html.escape(target['game_nick'])}</code> назначена должность: <b>{role_name}</b>.",
        parse_mode="HTML",
    )
    await callback.answer()
# ==========================================
# СМЕНА ФЛАГА, ОПИСАНИЯ, ВЫХОД И РОСПУСК
# ==========================================

@router.callback_query(F.data == "group:change_flag")
async def group_change_flag_menu(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await callback.answer("Сменить флаг может только глава группировки.", show_alert=True)
        return

    await _send_or_update_flag_carousel(
        target=callback,
        db=db,
        index=0,
        callback_prefix="gsetflag",
        back_callback="gsetflag:back",
        title="🏳️ <b>Выберите новый флаг для вашей группировки:</b>",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gsetflag:"))
async def group_change_flag_save(callback: CallbackQuery, db, state: FSMContext, bot: Bot, settings):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    action = parts[1]

    if action == "noop":
        await callback.answer()
        return

    if action == "page":
        await _send_or_update_flag_carousel(
            target=callback,
            db=db,
            index=int(parts[2]),
            callback_prefix="gsetflag",
            back_callback="gsetflag:back",
            title="🏳️ <b>Выберите новый флаг для вашей группировки:</b>",
        )
        await callback.answer()
        return

    async def _return_to_group_menu():
        if callback.message.photo:
            with contextlib.suppress(Exception):
                await callback.message.delete()
            fake_cb = callback.model_copy(update={"id": "0"})
            await group_menu(fake_cb, db, state)
        else:
            await group_menu(callback, db, state)

    if action == "back":
        await callback.answer()
        await _return_to_group_menu()
        return

    if action != "pick":
        await callback.answer()
        return

    flag_id = int(parts[2])
    if flag_id == 0:
        new_flag = "Без флага"
    else:
        fl = await db.get_group_flag_by_id(flag_id)
        new_flag = str(fl["name"]) if fl else "Без флага"

    group_id = user["group_id"]
    await db.execute("UPDATE groups SET flag_name = ? WHERE id = ?", (new_flag, group_id))
    await sync_group_topic_post(bot, db, settings, group_id)

    await callback.answer(f"✅ Флаг изменён на «{new_flag}»!", show_alert=True)
    await _return_to_group_menu()


@router.callback_query(F.data == "group:edit_desc")
async def group_edit_desc_prompt(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await callback.answer("Только глава может менять описание.", show_alert=True)
        return

    await state.set_state(GroupManage.wait_new_desc)
    await callback.message.answer("Введите новое <b>описание группировки</b> (до 500 символов) или /cancel:", parse_mode="HTML")
    await callback.answer()


@router.message(GroupManage.wait_new_desc)
async def group_edit_desc_save(message: Message, state: FSMContext, db, bot: Bot, settings):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("Изменение описания отменено.")
        return

    user = await db.get_user(message.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await state.clear()
        return

    await db.execute("UPDATE groups SET description = ? WHERE id = ?", (raw[:500], user["group_id"]))
    await state.clear()
    await sync_group_topic_post(bot, db, settings, user["group_id"])
    await message.answer("✅ Описание группировки обновлено!")


@router.callback_query(F.data == "group:leave_ask")
async def group_leave_confirm(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] == "leader":
        await callback.answer("Глава не может покинуть клан — только распустить его.", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, выйти", callback_data="group:leave_yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="group"),
    ]])
    await callback.message.edit_text("🚪 Вы уверены, что хотите покинуть группировку?", reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "group:leave_yes")
async def group_leave_execute(callback: CallbackQuery, db, bot: Bot, settings):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] == "leader":
        return

    group_id = user["group_id"]
    await db.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE id = ?", (user["id"],))
    await sync_group_topic_post(bot, db, settings, group_id)

    await callback.message.edit_text("🚪 Вы покинули группировку.")
    await callback.answer()


@router.callback_query(F.data == "group:dissolve_ask")
async def group_dissolve_confirm(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await callback.answer("Распустить клан может только глава.", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💥 Да, распустить навсегда", callback_data="group:dissolve_yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="group"),
    ]])
    await callback.message.edit_text(
        "⚠️ <b>Вы уверены, что хотите распустить группировку?</b>\nВсе участники будут исключены, а клан удалён.",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "group:dissolve_yes")
async def group_dissolve_execute(callback: CallbackQuery, db, bot: Bot, settings):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        return

    group_id = user["group_id"]
    group = await db.fetchone("SELECT topic_message_id FROM groups WHERE id = ?", (group_id,))
    msg_id = group["topic_message_id"] if group else None

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE group_id = ?", (group_id,))
        await conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        await conn.commit()

    chat_id = getattr(settings, "groups_chat_id", None)
    if chat_id and msg_id:
        with contextlib.suppress(Exception):
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)

    await callback.message.edit_text("💥 Группировка распущена.")
    await callback.answer()
