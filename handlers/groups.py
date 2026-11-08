from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

router = Router()

class CreateGroup(StatesGroup):
    name = State()
    tag = State()
    description = State()

@router.callback_query(F.data == "group")
async def group_menu(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)

    if not user["group_id"]:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать группировку", callback_data="group:create")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ])
        text = "Вы не состоите в группировке."
        
        # ИСПРАВЛЕНО: Проверка на фейковый клик из текстового меню
        if callback.id == "0":
            await callback.message.answer(text, reply_markup=markup)
        else:
            await callback.message.edit_text(text, reply_markup=markup)
            await callback.answer()
        return

    group = await db.fetchone(
        "SELECT * FROM groups WHERE id = ?", (user["group_id"],)
    )
    members = await db.get_group_members(user["group_id"])
    lines = [f"👥 <b>{group['name']} [{group['tag']}]</b>", group["description"], ""]
    for member in members:
        position = {
            "leader": "Лидер",
            "officer": "Офицер",
            "player": "Рядовой",
        }.get(member["role"], member["role"])
        lines.append(f"• {member['game_nick']} — {position}")

    rows = []
    if user["role"] in ("leader", "officer"):
        rows += [
            [InlineKeyboardButton(text="➕ Добавить участника", callback_data="group:add")],
            [InlineKeyboardButton(text="🗑 Удалить участника", callback_data="group:remove")],
        ]
    if user["role"] == "leader":
        rows.append([InlineKeyboardButton(text="🎖 Назначить должность", callback_data="group:promote")])
        rows.append([InlineKeyboardButton(text="💥 Распустить группировку", callback_data="group:dissolve")])
    else:
        rows.append([InlineKeyboardButton(text="🚪 Покинуть группировку", callback_data="group:leave")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])

    text_content = "\n".join(lines)
    reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)

    # ИСПРАВЛЕНО: Проверка на фейковый клик из текстового меню
    if callback.id == "0":
        await callback.message.answer(text_content, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text_content, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()

@router.callback_query(F.data == "group:create")
async def create_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CreateGroup.name)
    await callback.message.answer("Введите название группировки.")
    if callback.id != "0":
        await callback.answer()

@router.message(CreateGroup.name)
async def create_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not 3 <= len(name) <= 40:
        await message.answer("Название должно содержать от 3 до 40 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(CreateGroup.tag)
    await message.answer("Введите тег группировки (2–8 символов).")

@router.message(CreateGroup.tag)
async def create_tag(message: Message, state: FSMContext):
    tag = (message.text or "").strip().upper()
    if not 2 <= len(tag) <= 8 or not tag.isalnum():
        await message.answer("Тег должен содержать 2–8 букв или цифр.")
        return
    await state.update_data(tag=tag)
    await state.set_state(CreateGroup.description)
    await message.answer("Введите описание группировки.")

@router.message(CreateGroup.description)
async def create_description(message: Message, state: FSMContext, db):
    description = (message.text or "").strip()[:500]
    user = await db.get_user(message.from_user.id)
    if user["group_id"]:
        await state.clear()
        await message.answer("Вы уже состоите в группировке.")
        return

    data = await state.get_data()
    try:
        async with await db.connect() as conn:
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
        await message.answer("Не удалось создать группировку. Возможно, название или тег заняты.")
        return

    await state.clear()
    await message.answer("Группировка создана. Вы назначены лидером.")

@router.callback_query(F.data.in_({"group:add", "group:remove", "group:promote"}))
async def group_action_prompt(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if user["role"] not in ("leader", "officer"):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    action = callback.data.split(":")[1]
    if action == "promote" and user["role"] != "leader":
        await callback.answer("Назначать должности может только лидер.", show_alert=True)
        return

    await state.update_data(group_action=action)
    await callback.message.answer("Введите игровой ник участника.")
    await callback.answer()

@router.message(F.text, ~F.text.startswith("/"))
async def group_member_action(message: Message, state: FSMContext, db):
    data = await state.get_data()
    action = data.get("group_action")
    if not action:
        return

    actor = await db.get_user(message.from_user.id)
    if not actor or not actor["group_id"]:
        await state.update_data(group_action=None)
        return

    target = await db.get_user_by_nick((message.text or "").strip())
    if not target or target["group_id"] != actor["group_id"]:
        await message.answer("Игрок не найден среди участников этой группировки.")
        return
    if target["id"] == actor["id"]:
        await message.answer("Нельзя выполнить это действие над собой.")
        return

    if action == "add":
        if target["group_id"]:
            await message.answer("Игрок уже состоит в группировке.")
        else:
            await db.execute(
                "UPDATE users SET group_id = ?, role = 'player' WHERE id = ?",
                (actor["group_id"], target["id"]),
            )
            await message.answer(f"{target['game_nick']} добавлен в группировку.")
    elif action == "remove":
        await db.execute(
            "UPDATE users SET group_id = NULL, role = 'player' WHERE id = ?",
            (target["id"],),
        )
        await message.answer(f"{target['game_nick']} удалён из группировки.")
    elif action == "promote":
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Офицер", callback_data=f"role:officer:{target['id']}")],
            [InlineKeyboardButton(text="Рядовой", callback_data=f"role:player:{target['id']}")],
        ])
        await message.answer("Выберите должность:", reply_markup=markup)

    await state.update_data(group_action=None)

@router.callback_query(F.data.startswith("role:"))
async def set_group_role(callback: CallbackQuery, db):
    _, role, target_id = callback.data.split(":")
    actor = await db.get_user(callback.from_user.id)
    target_id = int(target_id)

    if not actor or actor["role"] != "leader":
        await callback.answer("Только лидер может назначать должности.", show_alert=True)
        return

    target = await db.fetchone("SELECT * FROM users WHERE id = ?", (target_id,))
    if not target or target["group_id"] != actor["group_id"] or role not in ("officer", "player"):
        await callback.answer("Невозможно изменить должность.", show_alert=True)
        return

    await db.execute("UPDATE users SET role = ? WHERE id = ?", (role, target_id))
    await callback.message.edit_text("Должность участника обновлена.")
    await callback.answer()

@router.callback_query(F.data.in_({"group:leave", "group:dissolve"}))
async def leave_or_dissolve(callback: CallbackQuery, db):
    # Конец оригинальной логики (оборван в примере)
    if callback.id != "0":
        await callback.answer()
