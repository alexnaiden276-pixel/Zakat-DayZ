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

# Стандартный набор игровых флагов DayZ
DEFAULT_DAYZ_FLAGS = [
    "Пираты",
    "Волки",
    "Медведи",
    "Череп",
    "Щит",
    "Огонь",
    "Звезда",
    "Анархия",
    "Ворон",
    "Сталкеры",
    "Олень",
    "Без флага",
]


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
    """
    Формирует текст карточки группировки строго по шаблону и возвращает (text, photo_file_id).
    """
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

    member_lines = []
    for m in members:
        role_title = {
            "leader": "Глава",
            "officer": "Заместитель",
            "player": "Боец",
        }.get(m["role"], "Боец")
        member_lines.append(f"• {html.escape(str(m['game_nick']))} — {role_title}")

    roster_block = "\n".join(member_lines) if member_lines else "• <i>Нет участников</i>"

    text = (
        f"🏴 <b>Группировка:</b> {safe_name}\n"
        f"🎮 {server_title}\n"
        f"👥 Участников: {len(members)}\n"
        f"🏳️ Флаг: {safe_flag}\n\n"
        f"<b>Состав:</b>\n"
        f"{roster_block}\n\n"
        f"Создан: {created_str} / Изменён: {updated_str}"
    )

    photo_id = await db.get_group_flag_photo(flag_name)
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
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

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


# ==========================================
# АДМИН-ЗАГРУЗКА КАРТИНОК ФЛАГОВ (/set_flag)
# ==========================================

@router.message(Command("set_flag"))
async def cmd_set_flag_photo(message: Message, db, settings):
    """
    Позволяет админу привязать картинку к названию флага.
    Использование: отправить фото с подписью '/set_flag Пираты'
    """
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    raw_caption = (message.caption or message.text or "").strip()
    parts = raw_caption.split(maxsplit=1)
    if len(parts) < 2 or not message.photo:
        flags_map = await db.get_all_group_flags()
        loaded_list = ", ".join(flags_map.keys()) if flags_map else "пока не загружены"
        await message.answer(
            "🏳️ <b>Привязка картинки к флагу</b>\n\n"
            "Отправьте <b>фотографию флага</b> одним сообщением с подписью:\n"
            "<code>/set_flag Пираты</code>\n\n"
            f"Доступные базовые названия: <code>{', '.join(DEFAULT_DAYZ_FLAGS[:-1])}</code>\n"
            f"Уже загружены фото для: <i>{html.escape(loaded_list)}</i>",
            parse_mode="HTML",
        )
        return

    flag_name = parts[1].strip()
    photo_file_id = message.photo[-1].file_id
    await db.set_group_flag_photo(flag_name, photo_file_id)

    await message.answer(
        f"✅ Картинка для флага <b>«{html.escape(flag_name)}»</b> сохранена!\n"
        "Теперь при выборе этого флага карточка группировки в теме будет публиковаться с этой картинкой.",
        parse_mode="HTML",
    )
# ==========================================
# ГЛАВНОЕ МЕНЮ ГРУППИРОВКИ И СПИСОК КЛАНОВ
# ==========================================

@router.callback_query(F.data == "group")
async def group_menu(callback: CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

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
        rows.append([InlineKeyboardButton(text="💥 Распустить группировку", callback_data="group:dissolve_ask")])
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
           ORDER BY members_count DESC, g.name ASC"""
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

    custom_flags = await db.get_all_group_flags()
    flag_names: list[str] = list(DEFAULT_DAYZ_FLAGS)
    for cf in custom_flags.keys():
        if cf.lower() not in {f.lower() for f in flag_names}:
            flag_names.insert(-1, cf)

    await state.update_data(available_flags=flag_names)

    rows = []
    pair: list[InlineKeyboardButton] = []
    for idx, fname in enumerate(flag_names):
        has_pic = "🖼 " if fname in custom_flags else "🏳️ "
        pair.append(InlineKeyboardButton(text=f"{has_pic}{fname}", callback_data=f"gflag:{idx}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="gflag:cancel")])

    await message.answer(
        "🏳️ <b>Выберите флаг вашей группировки:</b>\n"
        "<i>(Значок 🖼 означает, что для флага прикреплена картинка)</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )


@router.callback_query(CreateGroup.flag, F.data.startswith("gflag:"))
async def create_choose_flag(callback: CallbackQuery, state: FSMContext, db, bot: Bot, settings):
    choice = callback.data.split(":", 1)[1]
    if choice == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Создание группировки отменено.")
        await callback.answer()
        return

    data = await state.get_data()
    available_flags: list[str] = data.get("available_flags") or DEFAULT_DAYZ_FLAGS
    try:
        flag_name = available_flags[int(choice)]
    except (ValueError, IndexError):
        flag_name = "Без флага"

    user = await db.get_user(callback.from_user.id)
    if not user or user["group_id"]:
        await state.clear()
        await callback.message.edit_text("❌ Вы уже состоите в группировке.")
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
        await callback.message.edit_text("❌ Не удалось создать группировку. Возможно, название или тег уже заняты.")
        await callback.answer()
        return

    await state.clear()
    await sync_group_topic_post(bot, db, settings, group_id)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Открыть меню группировки", callback_data="group")]
    ])
    await callback.message.edit_text(
        f"🎉 Группировка <b>{html.escape(data['name'])} [{html.escape(data['tag'])}]</b> создана!\n"
        f"🏳️ Выбранный флаг: <b>{html.escape(flag_name)}</b>\n"
        f"📢 Информация о группировке опубликована в теме сервера.",
        reply_markup=markup,
        parse_mode="HTML",
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
        await state.set_state(None)
        await message.answer("Приглашение отменено.")
        return

    actor = await db.get_user(message.from_user.id)
    if not actor or not actor["group_id"] or actor["role"] not in ("leader", "officer"):
        await state.set_state(None)
        return

    target = await db.get_user_by_nick(nick)
    if not target:
        await message.answer("❌ Игрок с таким ником не зарегистрирован в боте. Попробуйте снова или /cancel:")
        return
    if target["id"] == actor["id"]:
        await message.answer("❌ Нельзя пригласить самого себя.")
        return
    if target["group_id"]:
        await state.set_state(None)
        await message.answer(f"❌ Игрок <code>{html.escape(target['game_nick'])}</code> уже состоит в группировке.", parse_mode="HTML")
        return

    group = await db.fetchone("SELECT * FROM groups WHERE id = ?", (actor["group_id"],))
    await state.set_state(None)

    invite_markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Вступить", callback_data=f"ginv:accept:{group['id']}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ginv:decline:{group['id']}"),
    ]])

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
            parse_mode="HTML",
        )
    except Exception:
        await message.answer("❌ Не удалось отправить приглашение (возможно, игрок заблокировал бота).")


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
        await state.set_state(None)
        await message.answer("Исключение отменено.")
        return

    actor = await db.get_user(message.from_user.id)
    if not actor or not actor["group_id"] or actor["role"] not in ("leader", "officer"):
        await state.set_state(None)
        return

    target = await db.get_user_by_nick(nick)
    if not target or target["group_id"] != actor["group_id"]:
        await message.answer("❌ Игрок не найден среди участников вашей группировки. Попробуйте снова или /cancel:")
        return
    if target["id"] == actor["id"]:
        await message.answer("❌ Нельзя исключить самого себя.")
        return
    if target["role"] == "leader" or (actor["role"] == "officer" and target["role"] == "officer"):
        await state.set_state(None)
        await message.answer("❌ Недостаточно прав для исключения этого участника.")
        return

    group_id = actor["group_id"]
    await db.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE id = ?", (target["id"],))
    await state.set_state(None)
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
        await state.set_state(None)
        await message.answer("Смена должности отменена.")
        return

    actor = await db.get_user(message.from_user.id)
    if not actor or not actor["group_id"] or actor["role"] != "leader":
        await state.set_state(None)
        return

    target = await db.get_user_by_nick(nick)
    if not target or target["group_id"] != actor["group_id"]:
        await message.answer("❌ Игрок не найден среди участников вашей группировки. Попробуйте снова или /cancel:")
        return
    if target["id"] == actor["id"]:
        await message.answer("❌ Нельзя изменить должность самому себе.")
        return

    await state.set_state(None)
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
# СМЕНА ОПИСАНИЯ, ВЫХОД И РОСПУСК
# ==========================================

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
        await state.set_state(None)
        await message.answer("Изменение описания отменено.")
        return

    user = await db.get_user(message.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await state.set_state(None)
        return

    await db.execute("UPDATE groups SET description = ? WHERE id = ?", (raw[:500], user["group_id"]))
    await state.set_state(None)
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
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    await callback.message.edit_text("💥 Группировка распущена.")
    await callback.answer()
