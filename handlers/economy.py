import html
import re
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

from middlewares.registration import IsVerifiedPlayer

router = Router()
router.message.filter(IsVerifiedPlayer())
router.callback_query.filter(IsVerifiedPlayer())

CAT_NAMES = {
    "weapon": "🔫 Оружие",
    "ammo": "🧨 Патроны",
    "medical": "💊 Медицина",
    "food": "🥫 Еда",
    "resources": "🪵 Ресурсы",
    "gear": "🎒 Снаряжение",
}


class CheckoutOrder(StatesGroup):
    wait_coordinates = State()


class Transfer(StatesGroup):
    nick = State()
    amount = State()
    confirm = State()


class SellLoot(StatesGroup):
    confirm_pickup = State()
    confirm_sale = State()


class AdminShop(StatesGroup):
    add_category = State()
    add_display_name = State()
    add_classname = State()
    add_price = State()
    edit_value = State()


class AdminFinance(StatesGroup):
    wait_for_balance = State()


def _is_admin(user_row, settings) -> bool:
    admin_ids = getattr(settings, "admin_ids", [])
    return bool(user_row and (user_row["role"] == "admin" or user_row["telegram_id"] in admin_ids))


# ==========================================
# 1. ТЕРМИНАЛ И КОРЗИНА ПОКУПАТЕЛЯ
# ==========================================

@router.callback_query(F.data == "terminal")
async def terminal(callback: CallbackQuery, state: FSMContext | None = None):
    total_count = 0
    if state:
        await state.set_state(None)
        data = await state.get_data()
        cart = data.get("cart", {})
        total_count = sum(cart.values())

    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"cat:{key}")]
        for key, title in CAT_NAMES.items()
    ]
    rows.append([InlineKeyboardButton(text=f"🛒 Корзина ({total_count} шт.)", callback_data="cart:view")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "🖥 <b>Терминал снабжения</b>\nВыберите категорию товаров:"

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Передать валюту", callback_data="transfer")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    text = f"💰 Ваш баланс: <b>{user['balance']} 🪙</b>"

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data.startswith("cart_add:"))
async def cart_add(callback: CallbackQuery, db, state: FSMContext):
    _, item_id_str, cat_key = callback.data.split(":")
    item = await db.get_shop_item(int(item_id_str))
    if not item:
        await callback.answer("Товар больше не доступен.", show_alert=True)
        return

    data = await state.get_data()
    cart = data.get("cart", {})
    cart[item_id_str] = cart.get(item_id_str, 0) + 1
    await state.update_data(cart=cart)

    await callback.answer(f"➕ {item['display_name']} добавлен в корзину!")
    callback.data = f"cat:{cat_key}"
    await category(callback, db, state)


@router.callback_query(F.data == "cart:view")
async def cart_view(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})

    if not cart:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ В каталог", callback_data="terminal")]
        ])
        await callback.message.edit_text("🛒 <b>Ваша корзина пуста.</b>", reply_markup=markup, parse_mode="HTML")
        await callback.answer()
        return

    lines = ["🛒 <b>Ваша корзина:</b>\n"]
    total_price = 0
    rows = []

    for item_id_str, qty in list(cart.items()):
        item = await db.get_shop_item(int(item_id_str))
        if not item:
            del cart[item_id_str]
            continue
        subtotal = item["price"] * qty
        total_price += subtotal
        lines.append(f"• {html.escape(item['display_name'])} × {qty} шт. = <b>{subtotal} 🪙</b>")
        rows.append([
            InlineKeyboardButton(text=f"➖ {item['display_name']}", callback_data=f"cart_dec:{item_id_str}"),
            InlineKeyboardButton(text=f"➕ ({qty})", callback_data=f"cart_inc:{item_id_str}"),
        ])

    await state.update_data(cart=cart)
    lines.append(f"\n💰 <b>Итого к оплате: {total_price} 🪙</b>")

    rows.append([InlineKeyboardButton(text="✅ Оформить заказ (ввести координаты)", callback_data="cart:checkout")])
    rows.append([InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="cart:clear")])
    rows.append([InlineKeyboardButton(text="⬅️ Продолжить покупки", callback_data="terminal")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cart_inc:"))
async def cart_inc(callback: CallbackQuery, db, state: FSMContext):
    item_id_str = callback.data.split(":")[1]
    data = await state.get_data()
    cart = data.get("cart", {})
    cart[item_id_str] = cart.get(item_id_str, 0) + 1
    await state.update_data(cart=cart)
    await cart_view(callback, db, state)


@router.callback_query(F.data.startswith("cart_dec:"))
async def cart_dec(callback: CallbackQuery, db, state: FSMContext):
    item_id_str = callback.data.split(":")[1]
    data = await state.get_data()
    cart = data.get("cart", {})
    if item_id_str in cart:
        if cart[item_id_str] > 1:
            cart[item_id_str] -= 1
        else:
            del cart[item_id_str]
    await state.update_data(cart=cart)
    await cart_view(callback, db, state)


@router.callback_query(F.data == "cart:clear")
async def cart_clear(callback: CallbackQuery, db, state: FSMContext):
    await state.update_data(cart={})
    await callback.answer("Корзина очищена.")
    await cart_view(callback, db, state)


# ==========================================
# 2. ОФОРМЛЕНИЕ И КООРДИНАТЫ iZurvive
# ==========================================

@router.callback_query(F.data == "cart:checkout")
async def cart_checkout(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    if not cart:
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    total_price = 0
    for item_id_str, qty in cart.items():
        item = await db.get_shop_item(int(item_id_str))
        if item:
            total_price += item["price"] * qty

    if user["balance"] < total_price:
        await callback.answer(
            f"Недостаточно средств! Нужно {total_price} 🪙, у вас {user['balance']} 🪙.",
            show_alert=True
        )
        return

    await state.set_state(CheckoutOrder.wait_coordinates)
    await callback.message.edit_text(
        "📍 <b>Введите координаты доставки с карты iZurvive</b>\n\n"
        "Укажите координаты <b>X</b> и <b>Z</b> через пробел или слэш.\n"
        "Пример: <code>4512.5 10234.0</code> или <code>4512 / 10234</code>\n\n"
        "⚠️ <i>Лут появится на указанной точке. Для отмены введите /cancel</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CheckoutOrder.wait_coordinates)
async def process_coordinates(message: Message, state: FSMContext, db, nitrado_api):
    raw_text = (message.text or "").strip()
    if raw_text.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Оформление заказа отменено. Товары сохранены в корзине.")
        return

    coords = re.findall(r"\d+(?:\.\d+)?", raw_text.replace(",", "."))
    if len(coords) < 2:
        await message.answer(
            "❌ Не удалось распознать координаты.\n"
            "Введите два числа (X и Z) с карты iZurvive, например: <code>6540 8120</code>",
            parse_mode="HTML",
        )
        return

    x_coord = float(coords[0])
    z_coord = float(coords[1])

    if not (0 <= x_coord <= 15360 and 0 <= z_coord <= 15360):
        await message.answer("❌ Координаты выходят за пределы карты (0 – 15360). Проверьте данные iZurvive.")
        return

    data = await state.get_data()
    cart = data.get("cart", {})
    if not cart:
        await state.clear()
        await message.answer("Корзина пуста.")
        return

    order_items = []
    total_price = 0
    for item_id_str, qty in cart.items():
        item = await db.get_shop_item(int(item_id_str))
        if item:
            order_items.append({
                "display_name": item["display_name"],
                "game_classname": item["game_classname"],
                "qty": qty,
            })
            total_price += item["price"] * qty

    user = await db.get_user(message.from_user.id)

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute("SELECT balance FROM users WHERE id = ?", (user["id"],))
        row = await cursor.fetchone()
        if not row or row["balance"] < total_price:
            await conn.rollback()
            await state.set_state(None)
            await message.answer("❌ Недостаточно средств для оплаты заказа.")
            return

        await conn.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (total_price, user["id"]))
        await conn.execute(
            "INSERT INTO transactions (sender_id, amount, kind) VALUES (?, ?, 'purchase')",
            (user["id"], total_price),
        )
        await conn.commit()

    wait_msg = await message.answer("⏳ Передаем координаты на сервер Nitrado...")
    success = await nitrado_api.append_spawn_orders(order_items, x_coord, z_coord)

    if not success:
        async with db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (total_price, user["id"]))
            await conn.execute(
                "INSERT INTO transactions (receiver_id, amount, kind) VALUES (?, ?, 'refund')",
                (user["id"], total_price),
            )
            await conn.commit()

        await state.set_state(None)
        await wait_msg.edit_text("❌ Ошибка связи с сервером Nitrado. Средства возвращены на ваш баланс.")
        return

    await state.clear()
    items_summary = "\n".join(f"• {html.escape(i['display_name'])} × {i['qty']} шт." for i in order_items)
    await wait_msg.edit_text(
        f"✅ <b>Заказ успешно оформлен!</b>\n\n"
        f"📍 <b>Координаты iZurvive:</b> <code>X: {x_coord} | Z: {z_coord}</code>\n"
        f"💰 <b>Списано:</b> {total_price} 🪙\n\n"
        f"📦 <b>Состав заказа:</b>\n{items_summary}\n\n"
        f"<i>Ожидайте прибытия груза на указанной точке.</i>",
        parse_mode="HTML",
    )


# ==========================================
# 3. КОШЕЛЕК И ПЕРЕВОДЫ
# ==========================================

@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery, db, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Передать валюту", callback_data="transfer")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    text = f"💰 Ваш баланс: <b>{user['balance']} 🪙</b>"

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data == "transfer")
async def transfer_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Transfer.nick)
    await callback.message.answer("Введите игровой ник получателя:")
    if callback.id != "0":
        await callback.answer()


@router.message(Transfer.nick)
async def transfer_nick(message: Message, state: FSMContext, db):
    nick = (message.text or "").strip()
    if nick.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Перевод отменён.")
        return

    receiver = await db.get_user_by_nick(nick)
    if not receiver:
        await message.answer("Игрок не найден. Введите другой ник или отмените перевод командой /cancel.")
        return

    await state.update_data(receiver_nick=receiver["game_nick"])
    await state.set_state(Transfer.amount)
    await message.answer("Введите сумму перевода целым числом:")


@router.message(Transfer.amount)
async def transfer_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Перевод отменён.")
        return

    try:
        amount = int(raw)
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
        f"Перевести <b>{amount} 🪙</b> игроку <code>{html.escape(data['receiver_nick'])}</code>?",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(Transfer.confirm, F.data.startswith("transfer:"))
async def transfer_confirm(callback: CallbackQuery, state: FSMContext, db):
    if callback.data.endswith(":no"):
        await state.set_state(None)
        await callback.message.edit_text("Перевод отменён.")
        await callback.answer()
        return

    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    ok, text = await db.transfer(user["id"], data["receiver_nick"], data["amount"])
    await state.set_state(None)
    await callback.message.edit_text(text)
    await callback.answer()


# ==========================================
# 4. АДМИН-ПАНЕЛЬ УПРАВЛЕНИЯ МАГАЗИНОМ
# ==========================================

@router.message(F.text == "🛠 Управление магазином")
@router.callback_query(F.data == "admin_shop")
async def admin_shop_menu(event: Message | CallbackQuery, db, settings, state: FSMContext):
    if state:
        await state.set_state(None)
    user = await db.get_user(event.from_user.id)
    if not _is_admin(user, settings):
        return

    items = await db.get_shop_items()
    rows = [[InlineKeyboardButton(text="➕ Добавить новый товар", callback_data="ashop:add")]]

    for item in items:
        rows.append([
            InlineKeyboardButton(
                text=f"{item['display_name']} ({item['game_classname']}) — {item['price']} 🪙",
                callback_data=f"ashop:item:{item['id']}",
            )
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "🛠 <b>Админ-панель магазина</b>\nВыберите товар для редактирования или добавьте новый:"

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "ashop:add")
async def ashop_add_start(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"ashop_cat:{key}")]
        for key, title in CAT_NAMES.items()
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")])
    await state.set_state(AdminShop.add_category)
    await callback.message.edit_text(
        "Выберите категорию для нового товара:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(AdminShop.add_category, F.data.startswith("ashop_cat:"))
async def ashop_add_cat(callback: CallbackQuery, state: FSMContext):
    cat_key = callback.data.split(":")[1]
    await state.update_data(category=cat_key)
    await state.set_state(AdminShop.add_display_name)
    await callback.message.edit_text(
        "Введите <b>название товара для игроков</b> (как оно будет видно в боте):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.add_display_name)
async def ashop_add_display_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return

    await state.update_data(display_name=name)
    await state.set_state(AdminShop.add_classname)
    await message.answer(
        "Введите <b>игровой Classname предмета</b> из файлов DayZ (например: <code>AKM</code>, <code>Mosin9130</code>):",
        parse_mode="HTML",
    )


@router.message(AdminShop.add_classname)
async def ashop_add_classname(message: Message, state: FSMContext):
    classname = (message.text or "").strip()
    if not classname or " " in classname:
        await message.answer("❌ Classname не должен содержать пробелов. Попробуйте снова:")
        return

    await state.update_data(game_classname=classname)
    await state.set_state(AdminShop.add_price)
    await message.answer("Введите <b>цену товара</b> целым числом:", parse_mode="HTML")


@router.message(AdminShop.add_price)
async def ashop_add_price(message: Message, state: FSMContext, db):
    try:
        price = int((message.text or "").strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Цена должна быть положительным целым числом.")
        return

        data = await state.get_data()
    await db.add_shop_item(
        category=data["category"],
        display_name=data["display_name"],
        game_classname=data["game_classname"],
        price=price,
    )
    await state.set_state(None)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель магазина", callback_data="admin_shop")]
    ])
    await message.answer(
        f"✅ Товар <b>{html.escape(data['display_name'])}</b> (<code>{html.escape(data['game_classname'])}</code>) за {price} 🪙 добавлен!",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("ashop:item:"))
async def ashop_item_detail(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    item_id = int(callback.data.split(":")[2])
    item = await db.get_shop_item(item_id)
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    text = (
        f"📦 <b>Редактирование товара</b>\n\n"
        f"Категория: {CAT_NAMES.get(item['category'], item['category'])}\n"
        f"Название в боте: <b>{html.escape(item['display_name'])}</b>\n"
        f"Classname в игре: <code>{html.escape(item['game_classname'])}</code>\n"
        f"Цена: <b>{item['price']} 🪙</b>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название в боте", callback_data=f"ashop_edit:{item_id}:display_name")],
        [InlineKeyboardButton(text="🧬 Изменить Classname в игре", callback_data=f"ashop_edit:{item_id}:game_classname")],
        [InlineKeyboardButton(text="💰 Изменить цену", callback_data=f"ashop_edit:{item_id}:price")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data=f"ashop_del:{item_id}")],
        [InlineKeyboardButton(text="⬅️ К списку товаров", callback_data="admin_shop")],
    ])
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_edit:"))
async def ashop_edit_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str, field = callback.data.split(":")
    await state.update_data(edit_item_id=int(item_id_str), edit_field=field)
    await state.set_state(AdminShop.edit_value)

    prompts = {
        "display_name": "Введите новое <b>отображаемое название</b> товара:",
        "game_classname": "Введите новый <b>игровой Classname</b> предмета (без пробелов):",
        "price": "Введите новую <b>цену</b> целым числом:",
    }
    await callback.message.edit_text(prompts[field], parse_mode="HTML")
    await callback.answer()


@router.message(AdminShop.edit_value)
async def ashop_edit_save(message: Message, state: FSMContext, db):
    data = await state.get_data()
    item_id = data["edit_item_id"]
    field = data["edit_field"]
    raw_val = (message.text or "").strip()

    if field == "price":
        try:
            val = int(raw_val)
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Цена должна быть положительным целым числом.")
            return
    elif field == "game_classname":
        if not raw_val or " " in raw_val:
            await message.answer("❌ Classname не должен содержать пробелов.")
            return
        val = raw_val
    else:
        if not raw_val:
            await message.answer("❌ Значение не может быть пустым.")
            return
        val = raw_val

    await db.update_shop_item_field(item_id, field, val)
    await state.set_state(None)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К товару", callback_data=f"ashop:item:{item_id}")]
    ])
    await message.answer("✅ Изменения успешно сохранены!", reply_markup=markup)


@router.callback_query(F.data.startswith("ashop_del:"))
async def ashop_delete(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    item_id = int(callback.data.split(":")[1])
    await db.delete_shop_item(item_id)
    await callback.answer("🗑 Товар удален.", show_alert=True)
    await admin_shop_menu(callback, db, settings, state)
    # ==========================================
# 5. СДАЧА ЛУТА (SELL_START)
# ==========================================

@router.callback_query(F.data == "sell")
async def sell_start(callback: CallbackQuery, state: FSMContext, settings):
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я готов", callback_data="sell:ready"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="sell:cancel"),
    ]])
    await state.set_state(SellLoot.confirm_pickup)
    text = (
        f"📦 Сбросьте предметы в точке сдачи:\n"
        f"{getattr(settings, 'sell_location', 'Зона сдачи')}\n"
        f"Координаты: {getattr(settings, 'sell_coordinates', 'Уточните у администрации')}\n\n"
        "После этого нажмите «Я готов»."
    )

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup)
    else:
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()


@router.callback_query(SellLoot.confirm_pickup, F.data == "sell:cancel")
async def sell_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.message.edit_text("Сдача лута отменена.")
    await callback.answer()


@router.callback_query(SellLoot.confirm_pickup, F.data == "sell:ready")
async def sell_ready(callback: CallbackQuery, state: FSMContext, game_api):
    items = await game_api.get_items_at_pickup()
    if not items:
        await state.set_state(None)
        await callback.message.edit_text("Предметы в точке сдачи не обнаружены.")
        await callback.answer()
        return

    total_reward = sum(item.get("price", 0) for item in items)
    await state.update_data(sell_items=items, sell_reward=total_reward)
    await state.set_state(SellLoot.confirm_sale)

    lines = ["📦 <b>Обнаруженные предметы:</b>\n"]
    for item in items:
        lines.append(f"• {html.escape(str(item.get('name', 'Предмет')))} — {item.get('price', 0)} 🪙")
    lines.append(f"\n💰 <b>Итоговая выплата: {total_reward} 🪙</b>")

    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Продать", callback_data="sell:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="sell:cancel"),
    ]])
    await callback.message.edit_text("\n".join(lines), reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@def _is_admin(user_row, settings) -> bool:
    if not user_row:
        return False
    if user_row["role"] == "admin":
        return True
    tg_id = user_row["telegram_id"]
    # Проверяем разные возможные варианты хранения ID админов в settings
    admin_ids = getattr(settings, "admin_ids", None) or getattr(settings, "admins", None) or []
    if isinstance(admin_ids, (list, tuple, set)) and tg_id in admin_ids:
        return True
    single_admin = getattr(settings, "admin_id", None)
    return tg_id == single_admin


# ==========================================
# 6. АДМИН-ФИНАНСЫ (ADMIN_FINANCE_MENU)
# ==========================================

@router.message(F.text == "💸 Финансы (Админ)")
@router.callback_query(F.data == "admin_finance")
async def admin_finance_menu(event: Message | CallbackQuery, db, settings, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(event.from_user.id)
    if not _is_admin(user, settings):
        if isinstance(event, Message):
            await event.answer("⛔ У вас нет прав доступа к этому разделу.")
        else:
            await event.answer("⛔ Нет доступа.", show_alert=True)
        return

    if state:
        await state.set_state(AdminFinance.wait_for_balance)

    text = (
        "💸 <b>Управление балансом игроков</b>\n\n"
        "Введите игровой ник и сумму изменения через пробел.\n"
        "Примеры:\n"
        "• Начислить: <code>PlayerNick 500</code>\n"
        "• Списать: <code>PlayerNick -500</code>\n\n"
        "Для отмены введите /cancel"
    )

    if isinstance(event, Message):
        await event.answer(text, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, parse_mode="HTML")
    else:
        await event.message.edit_text(text, parse_mode="HTML")
        await event.answer()


@router.message(AdminFinance.wait_for_balance)
async def admin_finance_process(message: Message, state: FSMContext, db, settings):
    user = await db.get_user(message.from_user.id)
    if not _is_admin(user, settings):
        await state.set_state(None)
        return

    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Управление финансами отменено.")
        return

    parts = raw.rsplit(maxsplit=1)
    if len(parts) != 2:
        await message.answer("❌ Неверный формат. Введите: <code>Ник Сумма</code>", parse_mode="HTML")
        return

    nick, amount_str = parts
    try:
        delta = int(amount_str)
        if delta == 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Сумма должна быть ненулевым целым числом.")
        return

    target = await db.get_user_by_nick(nick)
    if not target:
        await message.answer(f"❌ Игрок <code>{html.escape(nick)}</code> не найден.", parse_mode="HTML")
        return

    new_balance = max(0, target["balance"] + delta)
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, target["id"]))
        await conn.execute(
            "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, ?, ?, 'admin_adjust')",
            (user["id"], target["id"], delta),
        )
        await conn.commit()

    await state.set_state(None)
    await message.answer(
        f"✅ Баланс игрока <code>{html.escape(target['game_nick'])}</code> обновлён.\n"
        f"Новый баланс: <b>{new_balance} 🪙</b>",
        parse_mode="HTML",
    )



