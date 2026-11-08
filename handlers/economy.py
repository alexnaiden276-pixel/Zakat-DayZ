from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.menus import back_button, main_menu
from middlewares.registration import IsVerifiedPlayer

router = Router()
router.message.filter(IsVerifiedPlayer())
router.callback_query.filter(IsVerifiedPlayer())

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


# Класс состояний для админки финансов
class AdminFinance(StatesGroup):
    wait_for_balance = State()


@router.callback_query(F.data == "terminal")
async def terminal(callback: CallbackQuery):
    rows = []
    for key, title in CAT_NAMES.items():
        rows.append([InlineKeyboardButton(text=title, callback_data=f"cat:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "🖥 Выберите категорию:"

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup)
    else:
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()


@router.callback_query(F.data.startswith("cat:"))
async def category(callback: CallbackQuery):
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
    user = await db.get_user(callback.from_user.id)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Передать валюту", callback_data="transfer")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    text = f"💰 Ваш баланс: {user['balance']} 🪙"

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup)
    else:
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()


@router.callback_query(F.data == "transfer")
async def transfer_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Transfer.nick)
    await callback.message.answer("Введите игровой ник получателя.")
    if callback.id != "0":
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
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я готов", callback_data="sell:ready"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="sell:cancel"),
    ]])
    await state.set_state(SellLoot.confirm_pickup)
    text = (f"📦 Сбросьте предметы в точке сдачи:\n"
            f"{settings.sell_location}\nКоординаты: {settings.sell_coordinates}\n\n"
            "После этого нажмите «Я готов».")

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup)
    else:
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()


@router.callback_query(SellLoot.confirm_pickup, F.data == "sell:cancel")
async def sell_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Сдача лута отменена.")
    await callback.answer()


@router.callback_query(SellLoot.confirm_pickup, F.data == "sell:ready")
async def sell_ready(callback: CallbackQuery, state: FSMContext, game_api):
    items = await game_api.get_items_at_pickup()
    await callback.answer("Проверка лута завершена.", show_alert=True)


# --- 1. СТАРТОВОЕ МЕНЮ ФИНАНСОВ ---
@router.callback_query(F.data == "admin_finance")
async def admin_finance_menu(callback: CallbackQuery):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Изменить свой баланс", callback_data="admin_finance:edit")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])
    text = "💸 **Панель управления финансами сервера:**\n\nВы можете выдать или изменить баланс монет на своем аккаунте."

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup)
    else:
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()


# --- 2. ОБРАБОТЧИК КЛИКА НА КНОПКУ "Изменить баланс" ---
@router.callback_query(F.data == "admin_finance:edit")
async def admin_finance_click(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFinance.wait_for_balance)
    await callback.message.answer("✍️ Введите новую сумму баланса для вашего аккаунта (целое число):")
    await callback.answer()


# --- 3. ИСПРАВЛЕННЫЙ ХЭНДЛЕР СОХРАНЕНИЯ ЧИСЛА (БЕЗ СИНТАКСИЧЕСКИХ ОШИБОК) ---
@router.message(AdminFinance.wait_for_balance)
async def admin_finance_save(message: Message, state: FSMContext, db, settings):
    # Проверка прав администратора
    if message.from_user.id not in settings.admin_ids:
        await state.clear()
        return

    text = (message.text or "").strip()
    
    # Валидация на число
    if not text.isdigit():
        await message.answer(
            "❌ Ошибка! Баланс должен быть целым положительным числом.\n"
            "Попробуйте еще раз или вернитесь назад через текстовое меню."
        )
        return

    new_balance = int(text)
