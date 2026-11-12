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
async def terminal(callback: CallbackQuery, state: FSMContext):
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


@router.callback_query(F.data.startswith("cat:"))
async def category(callback: CallbackQuery, db, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in CAT_NAMES:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    items = await db.get_shop_items(key)
    data = await state.get_data()
    cart = data.get("cart", {})

    rows = []
    for item in items:
        in_cart = cart.get(str(item["id"]), 0)
        badge = f" [в корзине: {in_cart}]" if in_cart else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{item['display_name']} — {item['price']} 🪙{badge}",
                callback_data=f"cart_add:{item['id']}:{key}",
            )
        ])

    rows.append([InlineKeyboardButton(text=f"🛒 Перейти в корзину ({sum(cart.values())})", callback_data="cart:view")])
    rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="terminal")])

    text = f"<b>{CAT_NAMES[key]}</b>\nНажмите на товар, чтобы добавить его в корзину:"
    if not items:
        text += "\n\n<i>В этой категории пока нет товаров.</i>"

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
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
f"✅ Заказ успешно оформлен!\n\n"
f"📍 Координаты iZurvive: X: {x_coord} | Z: {z_coord}\n"
f"💰 Списано: {total_price} 🪙\n\n"
f"📦 Состав заказа:\n{items_summary}\n\n"
f"Ожидайте прибытия груза на указанной точке.",
parse_mode="HTML",
)

==========================================


3. КОШЕЛЕК И ПЕРЕВОДЫ


==========================================

@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery, db, state: FSMContext):
await state.set_state(None)
user = await db.get_user(callback.from_user.id)
markup = InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text="💸 Передать валюту", callback_data="transfer")],
[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
])
text = f"💰 Ваш баланс: {user['balance']} 🪙"
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
f"Перевести {amount} 🪙 игроку {html.escape(data['receiver_nick'])}?",
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

==========================================


4. АДМИН-ПАНЕЛЬ УПРАВЛЕНИЯ МАГАЗИНОМ


==========================================

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
text = "🛠 Админ-панель магазина\nВыберите товар для редактирования или добавьте новый:"
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
"Введите название товара для игроков (как оно будет видно в боте):",
parse_mode="HTML"
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
"Введите игровой Classname предмета из файлов DayZ (например: AKM, Mosin9130):",
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
await message.answer("Введите цену товара целым числом:", parse_mode="HTML")
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
f"✅ Товар {html.escape(data['display_name'])} ({html.escape(data['game_classname'])}) за {price} 🪙 добавлен!",
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
f"📦 Редактирование товара\n\n"
f"Категория: {CAT_NAMES.get(item['category'], item['category'])}\n"
f"Название в боте: {html.escape(item['display_name'])}\n"
f"Classname в игре: {html.escape(item['game_classname'])}\n"
f"Цена: {item['price']} 🪙"
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
"display_name": "Введите новое отображаемое название товара:",
"game_classname": "Введите новый игровой Classname предмета (без пробелов):",
"price": "Введите новую цену целым числом:",
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

*(Если у вас в `handlers/economy.py` в самом конце также были хэндлеры для кнопки «📦 Сдать лут» (`sell_start` / `sell_ready`) и «💸 Финансы (Админ)», добавьте их в конец этого файла — состояния `SellLoot` и `AdminFinance` вверху уже объявлены.)*

<FollowUp>
Нужна ли помощь с проверкой или доработкой оставшихся хэндлеров (**сдача лута** / **финансы админа** / **рестарт сервера**) или тестированием первого заказа через корзину?
</FollowUp>
