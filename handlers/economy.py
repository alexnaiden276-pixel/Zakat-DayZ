from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from keyboards.menus import back_button

router = Router()

CATALOG = {
    "weapon": [("Пистолет", 500), ("Охотничья винтовка", 1200)],
    "ammo": [("Патроны 9x19", 50), ("Патроны 7.62x39", 100)],
    "medical": [("Бинт", 25), ("Аптечка", 150)],
    "food": [("Консервы", 40), ("Фляга воды", 60)],
    "resources": [("Доски", 80), ("Металлолом", 100)],
    "gear": [("Рюкзак", 300), ("Каска", 250)],
}

CAT_NAMES = {
    "weapon": "Оружие",
    "ammo": "Патроны",
    "medical": "Медицина",
    "food": "Еда",
    "resources": "Ресурсы",
    "gear": "Снаряжение",
}


class Transfer(StatesGroup):
    nick = State()
    amount = State()
    confirm = State()


class SellLoot(StatesGroup):
    confirm_pickup = State()
    confirm_sale = State()


@router.callback_query(F.data == "terminal")
async def terminal(callback: CallbackQuery):
    rows = []
    for key, title in CAT_NAMES.items():
        from aiogram.types import InlineKeyboardButton
        rows.append([InlineKeyboardButton(text=title, callback_data=f"cat:{key}")])
    rows.append([__import__("aiogram").types.InlineKeyboardButton(
        text="⬅️ Назад", callback_data="menu"
    )])
    from aiogram.types import InlineKeyboardMarkup
    await callback.message.edit_text(
        "🖥 Выберите категорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat:"))
async def category(callback: CallbackQuery):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    key = callback.data.split(":", 1)[1]
    if key not in CATALOG:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    rows = [
        [InlineKeyboardButton(
            text=f"{name} — {price} 🪙",
            callback_data=f"buy:{key}:{index}",
        )]
        for index, (name, price) in enumerate(CATALOG[key])
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="terminal")])
    await callback.message.edit_text(
        f"📦 {CAT_NAMES[key]}\nВыберите предмет для покупки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_item(callback: CallbackQuery, db, game_api):
    try:
        _, category_key, index_raw = callback.data.split(":")
        item_name, price = CATALOG[category_key][int(index_raw)]
    except (ValueError, KeyError, IndexError):
        await callback.answer("Не удалось распознать предмет.", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    if user["balance"] < price:
        await callback.answer("Недостаточно средств.", show_alert=True)
        return

    # Сначала выдаём предмет, чтобы не списывать средства при ошибке API.
    if not await game_api.give_item(user["game_nick"], item_name):
        await callback.answer("Игровой сервер не подтвердил выдачу.", show_alert=True)
        return

    async with await db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute(
            "SELECT balance FROM users WHERE id = ?", (user["id"],)
        )).fetchone()
        if row["balance"] < price:
            await conn.rollback()
            await callback.answer("Недостаточно средств.", show_alert=True)
            return
        await conn.execute(
            "UPDATE users SET balance = balance - ? WHERE id = ?",
            (price, user["id"]),
        )
        await conn.execute(
            """INSERT INTO transactions(sender_id, amount, kind)
               VALUES (?, ?, 'purchase')""",
            (user["id"], price),
        )
        await conn.commit()

    await callback.answer(f"Предмет «{item_name}» выдан.", show_alert=True)
@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery, db):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    user = await db.get_user(callback.from_user.id)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Передать валюту", callback_data="transfer")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    await callback.message.edit_text(
        f"💰 Ваш баланс: {user['balance']} 🪙",
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(F.data == "transfer")
async def transfer_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Transfer.nick)
    await callback.message.answer("Введите игровой ник получателя.")
    await callback.answer()


@router.message(Transfer.nick)
async def transfer_nick(message: Message, state: FSMContext, db):
    nick = (message.text or "").strip()
    receiver = await db.get_user_by_nick(nick)
    if not receiver:
        await message.answer("Игрок не найден. Введите другой ник или отмените перевод командой /cancel.")
        return
    await state.update_data(receiver_nick=receiver["game_nick"])
    await state.set_state(Transfer.amount)
    await message.answer("Введите сумму перевода целым числом.")


@router.message(Transfer.amount)
async def transfer_amount(message: Message, state: FSMContext):
    try:
        amount = int((message.text or "").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Сумма должна быть положительным целым числом.")
        return

    await state.update_data(amount=amount)
    data = await state.get_data()
    await state.set_state(Transfer.confirm)
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="transfer:yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="transfer:no"),
    ]])
    await message.answer(
        f"Перевести {amount} 🪙 игроку {data['receiver_nick']}?",
        reply_markup=markup,
    )


@router.callback_query(Transfer.confirm, F.data.startswith("transfer:"))
async def transfer_confirm(callback: CallbackQuery, state: FSMContext, db):
    if callback.data.endswith(":no"):
        await state.clear()
        await callback.message.edit_text("Перевод отменён.")
        await callback.answer()
        return

    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    ok, text = await db.transfer(user["id"], data["receiver_nick"], data["amount"])
    await state.clear()
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "sell")
async def sell_start(callback: CallbackQuery, state: FSMContext, settings):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я готов", callback_data="sell:ready"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="sell:cancel"),
    ]])
    await state.set_state(SellLoot.confirm_pickup)
    await callback.message.edit_text(
        f"📦 Сбросьте предметы в точке сдачи:\n"
        f"{settings.sell_location}\nКоординаты: {settings.sell_coordinates}\n\n"
        "После этого нажмите «Я готов».",
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(SellLoot.confirm_pickup, F.data == "sell:cancel")
async def sell_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Сдача лута отменена.")
    await callback.answer()


@router.callback_query(SellLoot.confirm_pickup, F.data == "sell:ready")
async def sell_ready(callback: CallbackQuery, state: FSMContext, game_api):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    items = await game_api.get_items_at_drop_zone()
    
    if not items:
        await state.clear()
        await callback.message.edit_text("В зоне сдачи предметы не обнаружены.")
        await callback.answer()
        return

    total = sum(item.quantity * item.price for item in items)
    await state.update_data(
        items=[{"name": x.name, "quantity": x.quantity, "price": x.price} for x in items],
        total=total,
    )
    await state.set_state(SellLoot.confirm_sale)

    lines = [f"• {x.name} × {x.quantity} — {x.quantity * x.price} 🪙" for x in items]
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Продать", callback_data="sell:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="sell:cancel"),
    ]])
    await callback.message.edit_text(
        "Найденные предметы:\n" + "\n".join(lines) + f"\n\nИтого: {total} 🪙",
        reply_markup=markup,
    )
    await callback.answer()


@router.callback_query(SellLoot.confirm_sale, F.data == "sell:cancel")
async def sell_cancel_confirm(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Сдача лута отменена.")
    await callback.answer()


@router.callback_query(SellLoot.confirm_sale, F.data == "sell:confirm")
async def sell_confirm(callback: CallbackQuery, state: FSMContext, db, game_api):
    data = await state.get_data()
    items = data["items"]
    from services.game_api import Item

    api_items = [Item(x["name"], x["quantity"], x["price"]) for x in items]
    if not await game_api.remove_items_from_drop_zone(api_items):
        await state.clear()
        await callback.message.edit_text("Не удалось удалить предметы. Обратитесь в поддержку.")
        await callback.answer()
        return

    user = await db.get_user(callback.from_user.id)
    await db.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?",
        (data["total"], user["id"]),
    )
    await db.execute(
        """INSERT INTO transactions(receiver_id, amount, kind)
           VALUES (?, ?, 'loot_sale')""",
        (user["id"], data["total"]),
    )
    await state.clear()
    await callback.message.edit_text(f"Предметы сданы. Начислено {data['total']} 🪙.")
    await callback.answer()


@router.message(F.text == "/cancel")
async def cancel_fsm(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Текущий сценарий отменён.")
