import html
import logging
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()
logger = logging.getLogger(__name__)


class CreateGroup(StatesGroup):
    name = State()
    tag = State()
    description = State()


class GroupManage(StatesGroup):
    wait_invite_nick = State()
    wait_kick_nick = State()
    wait_promote_nick = State()
    wait_new_desc = State()


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
    safe_desc = html.escape(group["description"] or "Описание не задано.")

    lines = [
        f"👥 <b>{safe_name} [{safe_tag}]</b>",
        f"<i>{safe_desc}</i>",
        "",
        f"<b>Состав ({len(members)} чел.):</b>",
    ]
    for member in members:
        role_badge = {
            "leader": "👑 Лидер",
            "officer": "🎖 Офицер",
            "player": "👤 Рядовой",
        }.get(member["role"], html.escape(member["role"]))
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
        """SELECT g.id, g.name, g.tag, u.game_nick AS leader_nick,
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
            lines.append(
                f"{idx}. <b>{html.escape(g['name'])} [{html.escape(g['tag'])}]</b> — "
                f"{g['members_count']} чел. (Лидер: <code>{leader}</code>)"
            )
        text = "\n".join(lines)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="group")]
    ])
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()
# ==========================================
# СОЗДАНИЕ ГРУППИРОВКИ
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
        "Введите <b>тег группировки</b> (2–8 латинских/русских букв или цифр, например: <code>RUSH</code>):",
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

    description = raw[:500]
    user = await db.get_user(message.from_user.id)
    if not user or user["group_id"]:
        await state.clear()
        await message.answer("❌ Вы уже состоите в группировке.")
        return

    data = await state.get_data()
    try:
        async with db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                """INSERT INTO groups(name, tag, description, leader_id)
                   VALUES (?, ?, ?, ?)""",
                (data["name"], data["tag"], description, user["id"]),
            )
            group_id = cursor.lastrowid
            await conn.execute(
                "UPDATE users SET group_id = ?, role = 'leader' WHERE id = ?",
                (group_id, user["id"]),
            )
            await conn.commit()
    except Exception:
        await state.clear()
        await message.answer("❌ Не удалось создать группировку. Возможно, такое название или тег уже заняты.")
        return

    await state.clear()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Открыть меню группировки", callback_data="group")]
    ])
    await message.answer(
        f"🎉 Группировка <b>{html.escape(data['name'])} [{html.escape(data['tag'])}]</b> создана! Вы назначены лидером.",
        reply_markup=markup,
        parse_mode="HTML",
    )
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
async def group_invite_response(callback: CallbackQuery, db):
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
async def group_remove_process(message: Message, state: FSMContext, db):
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

    await db.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE id = ?", (target["id"],))
    await state.set_state(None)
    await message.answer(f"🗑 Игрок <code>{html.escape(target['game_nick'])}</code> исключён из группировки.", parse_mode="HTML")
@router.callback_query(F.data == "group:promote")
async def group_promote_prompt(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await callback.answer("Назначать должности может только лидер.", show_alert=True)
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
        [InlineKeyboardButton(text="🎖 Офицер", callback_data=f"role:officer:{target['id']}")],
        [InlineKeyboardButton(text="👤 Рядовой", callback_data=f"role:player:{target['id']}")],
    ])
    await message.answer(
        f"Выберите новую должность для <code>{html.escape(target['game_nick'])}</code>:",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("role:"))
async def set_group_role(callback: CallbackQuery, db):
    _, role, target_id_str = callback.data.split(":", 2)
    actor = await db.get_user(callback.from_user.id)
    target_id = int(target_id_str)

    if not actor or actor["role"] != "leader":
        await callback.answer("Только лидер может назначать должности.", show_alert=True)
        return

    target = await db.fetchone("SELECT * FROM users WHERE id = ?", (target_id,))
    if not target or target["group_id"] != actor["group_id"] or role not in ("officer", "player"):
        await callback.answer("Некорректный участник или должность.", show_alert=True)
        return

    await db.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))
    role_name = "Офицер" if role == "officer" else "Рядовой"
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
        await callback.answer("Только лидер может менять описание.", show_alert=True)
        return

    await state.set_state(GroupManage.wait_new_desc)
    await callback.message.answer("Введите новое <b>описание группировки</b> (до 500 символов) или /cancel:", parse_mode="HTML")
    await callback.answer()


@router.message(GroupManage.wait_new_desc)
async def group_edit_desc_save(message: Message, state: FSMContext, db):
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
    await message.answer("✅ Описание группировки обновлено!")


@router.callback_query(F.data == "group:leave_ask")
async def group_leave_confirm(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] == "leader":
        await callback.answer("Лидер не может покинуть клан — только распустить его.", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, выйти", callback_data="group:leave_yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="group"),
    ]])
    await callback.message.edit_text("🚪 Вы уверены, что хотите покинуть группировку?", reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "group:leave_yes")
async def group_leave_execute(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] == "leader":
        return

    await db.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE id = ?", (user["id"],))
    await callback.message.edit_text("🚪 Вы покинули группировку.")
    await callback.answer()


@router.callback_query(F.data == "group:dissolve_ask")
async def group_dissolve_confirm(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        await callback.answer("Распустить клан может только лидер.", show_alert=True)
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
async def group_dissolve_execute(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user or not user["group_id"] or user["role"] != "leader":
        return

    group_id = user["group_id"]
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("UPDATE users SET group_id = NULL, role = 'player' WHERE group_id = ?", (group_id,))
        await conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        await conn.commit()

    await callback.message.edit_text("💥 Группировка распущена.")
    await callback.answer()
