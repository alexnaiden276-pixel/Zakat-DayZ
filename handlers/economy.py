import html
import json
import re
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()

CAT_NAMES = {
    "weapon": "🔫 Оружие",
    "ammo": "🧨 Патроны",
    "medical": "💊 Медицина",
    "food": "🥫 Еда",
    "resources": "🪵 Ресурсы",
    "gear": "🎒 Снаряжение",
}

CLASSNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class CheckoutOrder(StatesGroup):
    wait_coordinates = State()


class Transfer(StatesGroup):
    nick = State()
    amount = State()
    confirm = State()


class AdminSell(StatesGroup):
    set_zone_coords = State()
    set_zone_radius = State()
    new_group_name = State()
    rename_group_name = State()
    add_group = State()
    add_display_name = State()
    add_classname = State()
    add_price = State()
    edit_value = State()
    edit_group = State()


class AdminShop(StatesGroup):
    add_category = State()
    add_subcategory = State()
    add_display_name = State()
    add_classname = State()
    add_price = State()
    edit_value = State()
    new_subcat_name = State()


class AdminFinance(StatesGroup):
    wait_for_balance = State()


def _is_admin(user_row, settings) -> bool:
    if not user_row:
        return False
    if user_row["role"] == "admin":
        return True
    tg_id = user_row["telegram_id"]
    admin_ids = getattr(settings, "admin_ids", None) or getattr(settings, "admins", None) or []
    if isinstance(admin_ids, (list, tuple, set)) and tg_id in admin_ids:
        return True
    single_admin = getattr(settings, "admin_id", None)
    return tg_id == single_admin
# ==========================================
# 1. ТЕРМИНАЛ, ПОДКАТЕГОРИИ И КОРЗИНА ПОКУПАТЕЛЯ
# ==========================================

async def _build_category_view(
    db,
    state: FSMContext,
    key: str,
    sub_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    data = await state.get_data()
    cart = data.get("cart", {})
    rows = []

    if sub_id is None:
        subcats = await db.get_subcategories(key)
        for sc in subcats:
            rows.append([
                InlineKeyboardButton(text=f"📁 {sc['name']}", callback_data=f"subcat:{key}:{sc['id']}")
            ])
        items = await db.get_shop_items(key, subcategory_id=None)
        header = f"<b>{CAT_NAMES[key]}</b>"
    else:
        subcat = await db.get_subcategory(sub_id)
        sub_title = html.escape(subcat["name"]) if subcat else "Группа"
        items = await db.get_shop_items(key, subcategory_id=sub_id)
        header = f"<b>{CAT_NAMES[key]} → 📁 {sub_title}</b>"

    has_content = bool(rows or items)

    for item in items:
        in_cart = cart.get(str(item["id"]), 0)
        badge = f" [в корзине: {in_cart}]" if in_cart else ""
        sub_suffix = str(sub_id) if sub_id is not None else "0"
        rows.append([
            InlineKeyboardButton(
                text=f"{item['display_name']} — {item['price']} 🪙{badge}",
                callback_data=f"cart_add:{item['id']}:{key}:{sub_suffix}",
            )
        ])

    total_in_cart = sum(cart.values())
    rows.append([InlineKeyboardButton(text=f"🛒 Перейти в корзину ({total_in_cart})", callback_data="cart:view")])

    if sub_id is not None:
        rows.append([InlineKeyboardButton(text=f"⬅️ Назад в «{CAT_NAMES[key]}»", callback_data=f"cat:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Все категории", callback_data="terminal")])

    if has_content:
        text = f"{header}\nВыберите группу или нажмите на товар, чтобы добавить в корзину:"
    else:
        text = f"{header}\n\n<i>В этом разделе пока пусто.</i>"

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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


@router.callback_query(F.data.startswith("cat:"))
async def category(callback: CallbackQuery, db, state: FSMContext):
    _, key = callback.data.split(":", 1)
    if key not in CAT_NAMES:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=None)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("subcat:"))
async def subcategory_view(callback: CallbackQuery, db, state: FSMContext):
    _, key, sub_id_str = callback.data.split(":", 2)
    if key not in CAT_NAMES:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=int(sub_id_str))
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("cart_add:"))
async def cart_add(callback: CallbackQuery, db, state: FSMContext):
    parts = callback.data.split(":")
    item_id_str, cat_key = parts, parts
    sub_id = int(parts) if len(parts) > 3 and parts != "0" else None

    item = await db.get_shop_item(int(item_id_str))
    if not item:
        await callback.answer("Товар больше не доступен.", show_alert=True)
        return

    data = await state.get_data()
    cart = data.get("cart", {})
    cart[item_id_str] = cart.get(item_id_str, 0) + 1
    await state.update_data(cart=cart)

    text, markup = await _build_category_view(db, state, cat_key, sub_id=sub_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer(f"➕ {item['display_name']} добавлен в корзину!")
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
    _, item_id_str = callback.data.split(":", 1)
    data = await state.get_data()
    cart = data.get("cart", {})
    if item_id_str not in cart:
        await callback.answer("Товар не найден в корзине.", show_alert=True)
        return
    cart[item_id_str] += 1
    await state.update_data(cart=cart)
    await cart_view(callback, db, state)


@router.callback_query(F.data.startswith("cart_dec:"))
async def cart_dec(callback: CallbackQuery, db, state: FSMContext):
    _, item_id_str = callback.data.split(":", 1)
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
            show_alert=True,
        )
        return

    await state.set_state(CheckoutOrder.wait_coordinates)
    await callback.message.edit_text(
        "📍 <b>Введите координаты доставки с карты iZurvive</b>\n\n"
        "Укажите координаты <b>X</b> и <b>Z</b> через пробел или слэш.\n"
        "Пример: <code>4512.5 10234.0</code> или <code>4512 / 10234</code>\n\n"
        "⚠️ <i>Лут появится на указанной точке после ближайшего рестарта. Для отмены введите /cancel</i>",
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

    if len(coords) >= 3:
        x_coord = float(coords)
        z_coord = float(coords)
    else:
        x_coord = float(coords)
        z_coord = float(coords)

    if not (0 <= x_coord <= 15360 and 0 <= z_coord <= 15360):
        await message.answer("❌ Координаты выходят за пределы карты (0 – 15360). Проверьте данные iZurvive.")
        return

    data = await state.get_data()
    cart = dict(data.get("cart") or {})
    if not cart:
        await state.set_state(None)
        await message.answer("🛒 Корзина пуста.")
        return

    await state.set_state(None)

    server_data = await nitrado_api.get_server_status()
    if server_data:
        srv_status = str(server_data.get("status", "")).lower()
        if srv_status in {"restarting", "stopping", "stopped"}:
            await state.set_state(CheckoutOrder.wait_coordinates)
            await message.answer(
                "⚠️ <b>Сервер сейчас перезагружается или остановлен.</b>\n"
                "Подождите 1–2 минуты до запуска сервера и отправьте координаты снова, чтобы заказ не потерялся.",
                parse_mode="HTML",
            )
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

    if not order_items:
        await state.update_data(cart={})
        await message.answer("❌ Выбранные товары больше не доступны.")
        return

    user = await db.get_user(message.from_user.id)

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute("SELECT balance FROM users WHERE id = ?", (user["id"],))
        row = await cursor.fetchone()
        if not row or row["balance"] < total_price:
            await conn.rollback()
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

        await wait_msg.edit_text("❌ Ошибка связи с сервером Nitrado. Средства возвращены на ваш баланс.")
        return

    items_summary = "\n".join(f"• {html.escape(i['display_name'])} × {i['qty']} шт." for i in order_items)
    await db.create_shop_order(user["id"], message.from_user.id, items_summary, x_coord, z_coord)
    await state.update_data(cart={})
    await wait_msg.edit_text(
        f"✅ <b>Заказ успешно оформлен!</b>\n\n"
        f"📍 <b>Координаты iZurvive:</b> <code>X: {x_coord} | Z: {z_coord}</code>\n"
        f"💰 <b>Списано:</b> {total_price} 🪙\n\n"
        f"📦 <b>Состав заказа:</b>\n{items_summary}\n\n"
        f"🔄 <i>Груз появится на указанной точке сразу после ближайшего рестарта сервера. "
        f"Мы пришлём вам уведомление, как только он будет доставлен!</i>",
        parse_mode="HTML",
    )
# ==========================================
# 3. КОШЕЛЕК И ПЕРЕВОДЫ
# ==========================================

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


@router.callback_query(F.data == "transfer")
async def transfer_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Transfer.nick)
    await callback.message.answer("Введите игровой ник получателя (или /cancel для отмены):")
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

    if receiver["telegram_id"] == message.from_user.id:
        await message.answer("❌ Нельзя перевести валюту самому себе. Введите ник другого игрока:")
        return

    await state.update_data(receiver_nick=receiver["game_nick"])
    await state.set_state(Transfer.amount)
    await message.answer("Введите сумму перевода целым числом:")


@router.message(Transfer.amount)
async def transfer_amount(message: Message, state: FSMContext, db):
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

    user = await db.get_user(message.from_user.id)
    if not user or user["balance"] < amount:
        await message.answer(
            f"❌ Недостаточно средств. Ваш баланс: <b>{user['balance'] if user else 0} 🪙</b>",
            parse_mode="HTML",
        )
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
    data = await state.get_data()
    await state.set_state(None)

    if callback.data.endswith(":no"):
        await callback.message.edit_text("Перевод отменён.")
        await callback.answer()
        return

    user = await db.get_user(callback.from_user.id)
    ok, text = await db.transfer(user["id"], data["receiver_nick"], data["amount"])
    await callback.message.edit_text(text)
    await callback.answer()


# ==========================================
# 4. АДМИН-ПАНЕЛЬ УПРАВЛЕНИЯ МАГАЗИНОМ И ГРУППАМИ
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
    rows = [
        [InlineKeyboardButton(text="➕ Добавить новый товар", callback_data="ashop:add")],
        [InlineKeyboardButton(text="📁 Управление группами каталога", callback_data="ashop:groups")],
    ]

    for item in items:
        rows.append([
            InlineKeyboardButton(
                text=f"{item['display_name']} ({item['game_classname']}) — {item['price']} 🪙",
                callback_data=f"ashop:item:{item['id']}",
            )
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "🛠 <b>Админ-панель магазина</b>\nУправляйте группами каталога, добавляйте или редактируйте товары:"

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


# --- УПРАВЛЕНИЕ ГРУППАМИ КАТАЛОГА (ПОДКАТЕГОРИЯМИ) ---

@router.callback_query(F.data == "ashop:groups")
async def ashop_groups_cats(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"ashop_grp_cat:{key}")]
        for key, title in CAT_NAMES.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ В админ-панель магазина", callback_data="admin_shop")])
    await callback.message.edit_text(
        "📁 <b>Управление группами каталога</b>\nВыберите основной раздел:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_grp_cat:"))
async def ashop_groups_list(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, cat_key = callback.data.split(":", 1)
    subcats = await db.get_subcategories(cat_key)

    rows = [[InlineKeyboardButton(text="➕ Создать новую группу", callback_data=f"ashop_grp_add:{cat_key}")]]
    for sc in subcats:
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 Удалить группу «{sc['name']}»",
                callback_data=f"ashop_grp_del:{cat_key}:{sc['id']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К выбору раздела", callback_data="ashop:groups")])

    text = f"📁 <b>Группы в разделе {CAT_NAMES[cat_key]}</b>\n"
    text += "Нажмите на группу, чтобы удалить её, или создайте новую:" if subcats else "\n<i>Групп пока нет.</i>"

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_grp_add:"))
async def ashop_grp_add_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, cat_key = callback.data.split(":", 1)
    await state.update_data(grp_cat_key=cat_key)
    await state.set_state(AdminShop.new_subcat_name)
    await callback.message.edit_text(
        f"Введите название новой группы для раздела <b>{CAT_NAMES[cat_key]}</b>\n"
        "(например: <code>Автоматы</code>, <code>Прицелы</code>, <code>Броня</code>) или /cancel:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.new_subcat_name)
async def ashop_grp_add_save(message: Message, state: FSMContext, db):
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Создание группы отменено.")
        return
    if not name or len(name) > 32:
        await message.answer("❌ Название должно быть от 1 до 32 символов.")
        return

    data = await state.get_data()
    cat_key = data["grp_cat_key"]
    try:
        await db.add_subcategory(cat_key, name)
    except Exception:
        await message.answer("❌ Такая группа уже существует в этом разделе.")
        return

    await state.set_state(None)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К группам раздела", callback_data=f"ashop_grp_cat:{cat_key}")]
    ])
    await message.answer(f"✅ Группа <b>«{html.escape(name)}»</b> создана!", reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("ashop_grp_del:"))
async def ashop_grp_delete(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, cat_key, sub_id_str = callback.data.split(":", 2)
    await db.delete_subcategory(int(sub_id_str))
    await callback.answer("🗑 Группа удалена (товары перенесены в общий раздел).", show_alert=True)

    callback.data = f"ashop_grp_cat:{cat_key}"
    await ashop_groups_list(callback, db, settings, state)
# --- ДОБАВЛЕНИЕ И РЕДАКТИРОВАНИЕ ТОВАРОВ МАГАЗИНА ---

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
        "Выберите основной раздел для нового товара:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(AdminShop.add_category, F.data.startswith("ashop_cat:"))
async def ashop_add_cat(callback: CallbackQuery, state: FSMContext, db):
    _, cat_key = callback.data.split(":", 1)
    await state.update_data(category=cat_key)

    subcats = await db.get_subcategories(cat_key)
    if subcats:
        rows = [
            [InlineKeyboardButton(text=f"📁 {sc['name']}", callback_data=f"ashop_sub:{sc['id']}")]
            for sc in subcats
        ]
        rows.append([InlineKeyboardButton(text="📄 Без группы (в корень раздела)", callback_data="ashop_sub:0")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")])
        await state.set_state(AdminShop.add_subcategory)
        await callback.message.edit_text(
            "Выберите <b>группу каталога</b> для товара:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.update_data(subcategory_id=None)
    await state.set_state(AdminShop.add_display_name)
    await callback.message.edit_text(
        "Введите <b>название товара для игроков</b> (или /cancel для отмены):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AdminShop.add_subcategory, F.data.startswith("ashop_sub:"))
async def ashop_add_subcat(callback: CallbackQuery, state: FSMContext):
    _, sub_id_str = callback.data.split(":", 1)
    sub_id = int(sub_id_str) if sub_id_str != "0" else None
    await state.update_data(subcategory_id=sub_id)
    await state.set_state(AdminShop.add_display_name)
    await callback.message.edit_text(
        "Введите <b>название товара для игроков</b> (или /cancel для отмены):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.add_display_name)
async def ashop_add_display_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление товара отменено.")
        return
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
    if classname.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление товара отменено.")
        return
    if not CLASSNAME_RE.match(classname):
        await message.answer(
            "❌ Classname должен содержать только латинские буквы, цифры и <code>_</code> (без пробелов):",
            parse_mode="HTML",
        )
        return

    await state.update_data(game_classname=classname)
    await state.set_state(AdminShop.add_price)
    await message.answer("Введите <b>цену товара</b> целым числом:", parse_mode="HTML")


@router.message(AdminShop.add_price)
async def ashop_add_price(message: Message, state: FSMContext, db, nitrado_api):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление товара отменено.")
        return

    try:
        price = int(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Цена должна быть положительным целым числом.")
        return

    data = await state.get_data()
    classname = data["game_classname"]

    await db.add_shop_item(
        category=data["category"],
        display_name=data["display_name"],
        game_classname=classname,
        price=price,
        subcategory_id=data.get("subcategory_id"),
    )
    event_synced = await nitrado_api.ensure_shop_event_exists(classname)
    await state.set_state(None)

    sync_note = (
        f"\n🧬 Событие <code>{html.escape(classname)}</code> синхронизировано с сервером."
        if event_synced
        else "\n⚠️ <i>Не удалось автоматически записать событие в shop_events.xml — проверьте FTP.</i>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель магазина", callback_data="admin_shop")]
    ])
    await message.answer(
        f"✅ Товар <b>{html.escape(data['display_name'])}</b> (<code>{html.escape(classname)}</code>) за {price} 🪙 добавлен!{sync_note}",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("ashop:item:"))
async def ashop_item_detail(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, item_id_str = callback.data.split(":", 2)
    item_id = int(item_id_str)
    item = await db.get_shop_item(item_id)
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    sub_label = "Без группы"
    if item["subcategory_id"]:
        sub = await db.get_subcategory(item["subcategory_id"])
        if sub:
            sub_label = html.escape(sub["name"])

    text = (
        f"📦 <b>Редактирование товара</b>\n\n"
        f"Раздел: {CAT_NAMES.get(item['category'], item['category'])}\n"
        f"Группа: <b>{sub_label}</b>\n"
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

    _, item_id_str, field = callback.data.split(":", 2)
    await state.update_data(edit_item_id=int(item_id_str), edit_field=field)
    await state.set_state(AdminShop.edit_value)

    prompts = {
        "display_name": "Введите новое <b>отображаемое название</b> товара (или /cancel):",
        "game_classname": "Введите новый <b>игровой Classname</b> предмета (или /cancel):",
        "price": "Введите новую <b>цену</b> целым числом (или /cancel):",
    }
    await callback.message.edit_text(prompts[field], parse_mode="HTML")
    await callback.answer()


@router.message(AdminShop.edit_value)
async def ashop_edit_save(message: Message, state: FSMContext, db, nitrado_api):
    raw_val = (message.text or "").strip()
    if raw_val.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Редактирование отменено.")
        return

    data = await state.get_data()
    item_id = data["edit_item_id"]
    field = data["edit_field"]

    if field == "price":
        try:
            val = int(raw_val)
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Цена должна быть положительным целым числом.")
            return
    elif field == "game_classname":
        if not CLASSNAME_RE.match(raw_val):
            await message.answer("❌ Classname должен содержать только латинские буквы, цифры и <code>_</code>:", parse_mode="HTML")
            return
        val = raw_val
    else:
        if not raw_val:
            await message.answer("❌ Значение не может быть пустым.")
            return
        val = raw_val

    await db.update_shop_item_field(item_id, field, val)
    if field == "game_classname":
        await nitrado_api.ensure_shop_event_exists(val)

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

    _, item_id_str = callback.data.split(":", 1)
    item_id = int(item_id_str)
    await db.delete_shop_item(item_id)
    await callback.answer("🗑 Товар удален.", show_alert=True)
    await admin_shop_menu(callback, db, settings, state)
# ==========================================
# 5. СДАЧА ЛУТА: КОРЗИНА, ОЧЕРЕДЬ, ПРОВЕРКА ПОЗИЦИИ И КУЛДАУН
# ==========================================

async def _render_active_sell_session(
    event: Message | CallbackQuery,
    db,
    session_row,
) -> None:
    """Отображает текущий статус активной заявки игрока (очередь, проверка позиции, сброс или фон)."""
    x, z, r = await db.get_sell_zone()
    status = session_row["status"]
    items_dict: dict = json.loads(session_row["items_json"])
    paid = int(session_row["paid_amount"] or 0)

    rem_lines = []
    expected_left = 0
    for v in items_dict.values():
        qty = int(v.get("qty", 0))
        if qty > 0:
            sub = int(v["price"]) * qty
            expected_left += sub
            rem_lines.append(f"• {html.escape(v['display_name'])} × {qty} шт. ({sub} 🪙)")

    items_block = "\n".join(rem_lines) if rem_lines else "<i>Все предметы засчитаны!</i>"
    rows = []

    if status == "queued":
        pos = await db.get_sell_queue_position(session_row["id"])
        status_text = (
            f"⏳ <b>Статус: Вы в очереди на сдачу (Место №{pos})</b>\n"
            f"Сейчас точку сдачи занимает другой игрок. Как только вышка освободится, бот уведомит вас "
            f"и проверит ваши координаты."
        )
        rows.append([InlineKeyboardButton(text="🔄 Обновить статус", callback_data="sell")])
        rows.append([InlineKeyboardButton(text="❌ Отменить заявку и выйти из очереди", callback_data="sell_sess:cancel")])

    elif status == "checking_pos":
        status_text = (
            f"📍 <b>Статус: Ваша очередь подошла! Проверка координат...</b>\n"
            f"Подойдите в радиус <b>{r} м</b> от точки <code>X: {x} | Z: {z}</code>.\n"
            f"Бот автоматически сверит ваш ник <code>{html.escape(session_row['game_nick'])}</code> по логам сервера "
            f"(проверка каждые 1–2 мин, обновление логов Nitrado раз в 5 мин) и разрешит сброс."
        )
        rows.append([InlineKeyboardButton(text="🔄 Обновить статус", callback_data="sell")])
        rows.append([InlineKeyboardButton(text="❌ Отменить заявку", callback_data="sell_sess:cancel")])

    elif status == "ready_to_drop":
        status_text = (
            f"✅ <b>Статус: Позиция подтверждена! Можно сбрасывать лут!</b>\n"
            f"Сбросьте заявленные предметы в яму на точке <code>X: {x} | Z: {z}</code>.\n"
            f"Выплата начисляется <b>по мере исчезновения каждого предмета</b> (сначала за разбитые, потом за целые).\n\n"
            f"Когда сбросите все вещи, нажмите <b>«🏁 Я всё сбросил»</b>, чтобы освободить вышку для других."
        )
        rows.append([InlineKeyboardButton(text="🏁 Я всё сбросил (освободить вышку)", callback_data="sell_sess:done_drop")])
        rows.append([InlineKeyboardButton(text="🔄 Обновить статус", callback_data="sell")])
        if paid == 0:
            rows.append([InlineKeyboardButton(text="❌ Отменить заявку", callback_data="sell_sess:cancel")])

    else:  # waiting_cleanup
        status_text = (
            f"♻️ <b>Статус: Вышка освобождена — фоновое ожидание деспавна</b>\n"
            f"Оставшиеся целые предметы продолжают отслеживаться в яме. Как только сервер удалит их, "
            f"остаток выплаты придёт автоматически."
        )
        rows.append([InlineKeyboardButton(text="🔄 Обновить статус", callback_data="sell")])

    rows.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu")])

    text = (
        f"📦 <b>Активная заявка на сдачу лута #{session_row['id']}</b>\n\n"
        f"{status_text}\n\n"
        f"💵 <b>Уже выплачено:</b> {paid} 🪙\n"
        f"⏳ <b>Ожидает исчезновения ({expected_left} 🪙):</b>\n{items_block}"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
async def _build_sell_catalog_view(
    db,
    state: FSMContext,
    group_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    data = await state.get_data()
    sell_cart: dict = data.get("sell_cart", {})
    x, z, r = await db.get_sell_zone()
    rows = []

    if group_id is None:
        groups = await db.get_sell_groups()
        for grp in groups:
            rows.append([
                InlineKeyboardButton(text=f"📁 {grp['name']}", callback_data=f"sell_grp:{grp['id']}")
            ])
        items = await db.get_sell_items(group_id=None)
        header = (
            f"♻️ <b>Сдача лута — сбор списка</b>\n"
            f"📍 Точка: <code>X: {x} | Z: {z}</code> (радиус <code>{r} м</code>)"
        )
    else:
        grp = await db.get_sell_group(group_id)
        grp_title = html.escape(grp["name"]) if grp else "Группа"
        items = await db.get_sell_items(group_id=group_id)
        header = f"♻️ <b>Сдача лута → 📁 {grp_title}</b>"

    for item in items:
        in_cart = sell_cart.get(str(item["id"]), 0)
        badge = f" [в списке: {in_cart}]" if in_cart else ""
        grp_suffix = str(group_id) if group_id is not None else "0"
        rows.append([
            InlineKeyboardButton(
                text=f"➕ {item['display_name']} — {item['price']} 🪙{badge}",
                callback_data=f"scart_add:{item['id']}:{grp_suffix}",
            )
        ])

    total_qty = sum(sell_cart.values())
    rows.append([
        InlineKeyboardButton(text=f"📋 Мой список сдачи ({total_qty} шт.)", callback_data="scart:view")
    ])

    if group_id is not None:
        rows.append([InlineKeyboardButton(text="⬅️ Назад к группам сдачи", callback_data="sell")])
    rows.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu")])

    text = f"{header}\n\nВыберите группу или нажмите на предмет, чтобы добавить его в заявку на сдачу:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "sell")
async def sell_start(callback: CallbackQuery, state: FSMContext, db):
    if state:
        await state.set_state(None)

    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    # 1. Если у игрока уже есть незавершённая заявка — показываем её статус
    active_sess = await db.get_user_active_sell_session(user["id"])
    if active_sess:
        await _render_active_sell_session(callback, db, active_sess)
        return

    # 2. Проверяем 10-минутный кулдаун после прошлой успешной сдачи
    cd_left = await db.get_sell_cooldown_remaining(user["id"], cooldown_seconds=600)
    if cd_left > 0:
        mins, secs = divmod(cd_left, 60)
        cd_text = (
            f"⏳ <b>Повторная сдача лута временно недоступна.</b>\n\n"
            f"Следующую заявку можно будет оформить через <b>{mins} мин. {secs} сек.</b>"
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить снова", callback_data="sell")],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu")],
        ])
        if callback.id == "0":
            await callback.message.answer(cd_text, reply_markup=markup, parse_mode="HTML")
        else:
            await callback.message.edit_text(cd_text, reply_markup=markup, parse_mode="HTML")
            await callback.answer()
        return

    # 3. Открываем каталог для сборки списка сдачи
    text, markup = await _build_sell_catalog_view(db, state, group_id=None)
    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data.startswith("sell_grp:"))
async def sell_group_view(callback: CallbackQuery, db, state: FSMContext):
    _, grp_id_str = callback.data.split(":", 1)
    text, markup = await _build_sell_catalog_view(db, state, group_id=int(grp_id_str))
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("scart_add:"))
async def scart_add(callback: CallbackQuery, db, state: FSMContext):
    _, item_id_str, grp_suffix = callback.data.split(":", 2)
    item = await db.get_sell_item(int(item_id_str))
    if not item:
        await callback.answer("Предмет больше не принимается.", show_alert=True)
        return

    data = await state.get_data()
    sell_cart = data.get("sell_cart", {})
    sell_cart[item_id_str] = sell_cart.get(item_id_str, 0) + 1
    await state.update_data(sell_cart=sell_cart)

    grp_id = int(grp_suffix) if grp_suffix != "0" else None
    text, markup = await _build_sell_catalog_view(db, state, group_id=grp_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer(f"➕ {item['display_name']} добавлен в список!")
@router.callback_query(F.data == "scart:view")
async def scart_view(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    sell_cart = data.get("sell_cart", {})

    if not sell_cart:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К выбору предметов", callback_data="sell")]
        ])
        await callback.message.edit_text("📋 <b>Ваш список сдачи пуст.</b>", reply_markup=markup, parse_mode="HTML")
        await callback.answer()
        return

    lines = ["📋 <b>Ваш список предметов на сдачу:</b>\n"]
    total_payout = 0
    rows = []

    for item_id_str, qty in list(sell_cart.items()):
        item = await db.get_sell_item(int(item_id_str))
        if not item:
            del sell_cart[item_id_str]
            continue
        subtotal = int(item["price"]) * qty
        total_payout += subtotal
        lines.append(f"• {html.escape(item['display_name'])} × {qty} шт. = <b>{subtotal} 🪙</b>")
        rows.append([
            InlineKeyboardButton(text=f"➖ {item['display_name']}", callback_data=f"scart_dec:{item_id_str}"),
            InlineKeyboardButton(text=f"➕ ({qty})", callback_data=f"scart_inc:{item_id_str}"),
        ])

    await state.update_data(sell_cart=sell_cart)
    x, z, r = await db.get_sell_zone()
    lines.append(f"\n💰 <b>Ожидаемая выплата: {total_payout} 🪙</b>")
    lines.append(f"📍 <b>Точка сдачи:</b> <code>X: {x} | Z: {z}</code> (радиус <code>{r} м</code>)")

    rows.append([InlineKeyboardButton(text="✅ Подтвердить список и встать в очередь", callback_data="scart:submit")])
    rows.append([InlineKeyboardButton(text="🗑 Очистить список", callback_data="scart:clear")])
    rows.append([InlineKeyboardButton(text="⬅️ Добавить ещё предметы", callback_data="sell")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("scart_inc:"))
async def scart_inc(callback: CallbackQuery, db, state: FSMContext):
    _, item_id_str = callback.data.split(":", 1)
    data = await state.get_data()
    sell_cart = data.get("sell_cart", {})
    if item_id_str in sell_cart:
        sell_cart[item_id_str] += 1
        await state.update_data(sell_cart=sell_cart)
    await scart_view(callback, db, state)


@router.callback_query(F.data.startswith("scart_dec:"))
async def scart_dec(callback: CallbackQuery, db, state: FSMContext):
    _, item_id_str = callback.data.split(":", 1)
    data = await state.get_data()
    sell_cart = data.get("sell_cart", {})
    if item_id_str in sell_cart:
        if sell_cart[item_id_str] > 1:
            sell_cart[item_id_str] -= 1
        else:
            del sell_cart[item_id_str]
        await state.update_data(sell_cart=sell_cart)
    await scart_view(callback, db, state)


@router.callback_query(F.data == "scart:clear")
async def scart_clear(callback: CallbackQuery, db, state: FSMContext):
    await state.update_data(sell_cart={})
    await callback.answer("Список сдачи очищен.")
    await scart_view(callback, db, state)


@router.callback_query(F.data == "scart:submit")
async def scart_submit(callback: CallbackQuery, db, state: FSMContext, nitrado_api):
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    if await db.get_user_active_sell_session(user["id"]):
        await callback.answer("У вас уже есть активная заявка!", show_alert=True)
        await sell_start(callback, state, db)
        return

    cd_left = await db.get_sell_cooldown_remaining(user["id"], cooldown_seconds=600)
    if cd_left > 0:
        await callback.answer(f"Подождите окончания кулдауна ({cd_left} сек).", show_alert=True)
        return

    data = await state.get_data()
    sell_cart = data.get("sell_cart", {})
    if not sell_cart:
        await callback.answer("Список сдачи пуст!", show_alert=True)
        return

    session_items = {}
    for item_id_str, qty in sell_cart.items():
        item = await db.get_sell_item(int(item_id_str))
        if item and qty > 0:
            session_items[item_id_str] = {
                "display_name": item["display_name"],
                "classname": item["game_classname"],
                "price": int(item["price"]),
                "qty": int(qty),
            }

    if not session_items:
        await state.update_data(sell_cart={})
        await callback.answer("Выбранные предметы больше не принимаются.", show_alert=True)
        return

    await callback.answer("⏳ Проверяем ваши координаты на точке сдачи...")
    drop_x, drop_z, drop_r = await db.get_sell_zone()

    is_at_zone = await nitrado_api.is_player_at_sell_zone(
        game_nick=user["game_nick"],
        center_x=drop_x,
        center_z=drop_z,
        radius=drop_r,
    )

    if not is_at_zone:
        await db.set_setting(f"sell_pos_watch:{user['id']}", str(callback.from_user.id))

        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить координаты снова", callback_data="scart:submit")],
            [InlineKeyboardButton(text="⬅️ Вернуться к списку сдачи", callback_data="scart:view")],
        ])
        await callback.message.edit_text(
            f"📍 <b>Вы пока не обнаружены в зоне сдачи лута!</b>\n\n"
            f"Подойдите в радиус <b>{drop_r} м</b> от точки <code>X: {drop_x} | Z: {drop_z}</code>.\n"
            f"Бот автоматически проверит ваши координаты по нику <code>{html.escape(user['game_nick'])}</code> "
            f"(логи Nitrado обновляются раз в 5 минут) и пришлёт уведомление, как только разрешит оформить сдачу.\n\n"
            f"⚠️ <i>Заявка не создана и место в очереди не занято, пока ваше присутствие на точке не подтверждено.</i>",
            reply_markup=markup,
            parse_mode="HTML",
        )
        return

    await db.execute("DELETE FROM settings WHERE key = ?", (f"sell_pos_watch:{user['id']}",))

    sess_id = await db.create_sell_session(
        user_id=user["id"],
        telegram_id=callback.from_user.id,
        game_nick=user["game_nick"],
        items_json=json.dumps(session_items, ensure_ascii=False),
    )
    await state.update_data(sell_cart={})

    sess_row = await db.fetchone("SELECT * FROM sell_sessions WHERE id = ?", (sess_id,))
    await _render_active_sell_session(callback, db, sess_row)


@router.callback_query(F.data == "sell_sess:done_drop")
async def sell_sess_done_drop(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    sess = await db.get_user_active_sell_session(user["id"]) if user else None
    if not sess or sess["status"] != "ready_to_drop":
        await callback.answer("Заявка не находится в режиме сброса.", show_alert=True)
        return

    items_dict = json.loads(sess["items_json"])
    has_remaining = any(int(v.get("qty", 0)) > 0 for v in items_dict.values())

    if has_remaining:
        await db.execute(
            """UPDATE sell_sessions
               SET status = 'waiting_cleanup', finished_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (sess["id"],),
        )
        await callback.answer("Вышка освобождена! Ожидаем исчезновения оставшихся вещей в фоне.", show_alert=True)
        updated = await db.fetchone("SELECT * FROM sell_sessions WHERE id = ?", (sess["id"],))
        await _render_active_sell_session(callback, db, updated)
    else:
        await db.execute(
            """UPDATE sell_sessions
               SET status = 'completed', finished_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (sess["id"],),
        )
        await callback.answer("Сдача лута полностью завершена!", show_alert=True)
        await sell_start(callback, state, db)


@router.callback_query(F.data == "sell_sess:cancel")
async def sell_sess_cancel(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    sess = await db.get_user_active_sell_session(user["id"]) if user else None
    if not sess or sess["status"] not in {"queued", "checking_pos", "ready_to_drop"}:
        await callback.answer("Заявку уже нельзя отменить.", show_alert=True)
        return

    paid = int(sess["paid_amount"] or 0)
    final_status = "completed" if paid > 0 else "cancelled"

    await db.execute(
        """UPDATE sell_sessions
           SET status = ?, finished_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (final_status, sess["id"]),
    )
    await callback.answer("❌ Заявка отменена, вышка освобождена.", show_alert=True)
    await sell_start(callback, state, db)
# ==========================================
# 5.1 АДМИН-ПАНЕЛЬ УПРАВЛЕНИЯ СДАЧЕЙ ЛУТА
# ==========================================

@router.callback_query(F.data == "admin_sell")
async def admin_sell_menu(event: Message | CallbackQuery, db, settings, state: FSMContext):
    if state:
        await state.set_state(None)
    user = await db.get_user(event.from_user.id)
    if not _is_admin(user, settings):
        return

    x, z, r = await db.get_sell_zone()
    items = await db.get_sell_items(all_items=True)

    rows = [
        [InlineKeyboardButton(text="📍 Изменить координаты (X / Z)", callback_data="asell:zone")],
        [InlineKeyboardButton(text="⭕ Изменить радиус зоны", callback_data="asell:radius")],
        [InlineKeyboardButton(text="➕ Добавить товар в приемку", callback_data="asell:add")],
        [InlineKeyboardButton(text="📁 Управление группами сдачи", callback_data="asell:groups")],
    ]

    for item in items:
        grp_tag = f"[{item['group_name']}] " if item["group_name"] else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{grp_tag}{item['display_name']} ({item['game_classname']}) — {item['price']} 🪙",
                callback_data=f"asell:item:{item['id']}",
            )
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = (
        f"♻️ <b>Админ-панель сдачи лута</b>\n\n"
        f"📍 Точка сдачи: <code>X: {x} | Z: {z}</code>\n"
        f"⭕ Радиус зоны: <code>{r} м</code>\n\n"
        f"Управляйте координатами, группами и принимаемыми предметами:"
    )

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


# --- НАСТРОЙКА КООРДИНАТ И РАДИУСА ---

@router.callback_query(F.data == "asell:zone")
async def asell_zone_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    await state.set_state(AdminSell.set_zone_coords)
    await callback.message.edit_text(
        "📍 Введите новые координаты центра зоны сдачи <b>X</b> и <b>Z</b> через пробел или слэш\n"
        "(например: <code>4512.5 8234.0</code>) или /cancel:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminSell.set_zone_coords)
async def asell_zone_save(message: Message, state: FSMContext, db):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Изменение координат отменено.")
        return

    coords = re.findall(r"\d+(?:\.\d+)?", raw.replace(",", "."))
    if len(coords) < 2:
        await message.answer("❌ Введите два числа (X и Z), например: <code>4512.5 8234.0</code>", parse_mode="HTML")
        return

    x = float(coords)
    z = float(coords) if len(coords) >= 3 else float(coords)
    if not (0 <= x <= 15360 and 0 <= z <= 15360):
        await message.answer("❌ Координаты должны быть в пределах 0 – 15360.")
        return

    await db.set_sell_zone(x, z)
    await state.set_state(None)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель сдачи", callback_data="admin_sell")]
    ])
    await message.answer(
        f"✅ Координаты зоны сдачи обновлены: <code>X: {round(x, 2)} | Z: {round(z, 2)}</code>",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "asell:radius")
async def asell_radius_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    await state.set_state(AdminSell.set_zone_radius)
    await callback.message.edit_text(
        "⭕ Введите новый <b>радиус зоны сдачи в метрах</b> (например: <code>15</code>) или /cancel:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminSell.set_zone_radius)
async def asell_radius_save(message: Message, state: FSMContext, db):
    raw = (message.text or "").strip().replace(",", ".")
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Изменение радиуса отменено.")
        return

    try:
        radius = float(raw)
        if not (1 <= radius <= 200):
            raise ValueError
    except ValueError:
        await message.answer("❌ Радиус должен быть числом от 1 до 200 метров.")
        return

    x, z, _ = await db.get_sell_zone()
    await db.set_sell_zone(x, z, radius)
    await state.set_state(None)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель сдачи", callback_data="admin_sell")]
    ])
    await message.answer(
        f"✅ Радиус зоны сдачи установлен: <b>{round(radius, 1)} м</b>",
        reply_markup=markup,
        parse_mode="HTML",
    )
# --- УПРАВЛЕНИЕ ГРУППАМИ СДАЧИ (СОЗДАНИЕ, ПЕРЕИМЕНОВАНИЕ, УДАЛЕНИЕ) ---

@router.callback_query(F.data == "asell:groups")
async def asell_groups_list(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    groups = await db.get_sell_groups()
    rows = [[InlineKeyboardButton(text="➕ Создать новую группу", callback_data="asell_grp:add")]]
    for grp in groups:
        rows.append([
            InlineKeyboardButton(text=f"📁 {grp['name']}", callback_data=f"asell_grp:detail:{grp['id']}")
        ])
    rows.append([InlineKeyboardButton(text="⬅️ В админ-панель сдачи", callback_data="admin_sell")])

    text = "📁 <b>Управление группами сдачи лута</b>\n"
    text += "Выберите группу для редактирования или создайте новую:" if groups else "\n<i>Групп пока нет.</i>"

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "asell_grp:add")
async def asell_grp_add_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    await state.set_state(AdminSell.new_group_name)
    await callback.message.edit_text(
        "Введите название новой группы сдачи лута (например: <code>Оружие</code>, <code>Трофеи</code>) или /cancel:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminSell.new_group_name)
async def asell_grp_add_save(message: Message, state: FSMContext, db):
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Создание группы отменено.")
        return
    if not name or len(name) > 32:
        await message.answer("❌ Название должно быть от 1 до 32 символов.")
        return

    try:
        await db.add_sell_group(name)
    except Exception:
        await message.answer("❌ Группа с таким названием уже существует.")
        return

    await state.set_state(None)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку групп", callback_data="asell:groups")]
    ])
    await message.answer(f"✅ Группа <b>«{html.escape(name)}»</b> создана!", reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("asell_grp:detail:"))
async def asell_grp_detail(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, grp_id_str = callback.data.split(":", 2)
    grp_id = int(grp_id_str)
    grp = await db.get_sell_group(grp_id)
    if not grp:
        await callback.answer("Группа не найдена.", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название группы", callback_data=f"asell_grp:rename:{grp_id}")],
        [InlineKeyboardButton(text="🗑 Удалить группу", callback_data=f"asell_grp:del:{grp_id}")],
        [InlineKeyboardButton(text="⬅️ К списку групп", callback_data="asell:groups")],
    ])
    await callback.message.edit_text(
        f"📁 <b>Управление группой: «{html.escape(grp['name'])}»</b>\nВыберите действие:",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asell_grp:rename:"))
async def asell_grp_rename_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, grp_id_str = callback.data.split(":", 2)
    await state.update_data(rename_sell_grp_id=int(grp_id_str))
    await state.set_state(AdminSell.rename_group_name)
    await callback.message.edit_text("Введите новое <b>название группы</b> (или /cancel):", parse_mode="HTML")
    await callback.answer()


@router.message(AdminSell.rename_group_name)
async def asell_grp_rename_save(message: Message, state: FSMContext, db):
    new_name = (message.text or "").strip()
    if new_name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Переименование отменено.")
        return
    if not new_name or len(new_name) > 32:
        await message.answer("❌ Название должно быть от 1 до 32 символов.")
        return

    data = await state.get_data()
    grp_id = data["rename_sell_grp_id"]
    try:
        await db.rename_sell_group(grp_id, new_name)
    except Exception:
        await message.answer("❌ Группа с таким названием уже существует.")
        return

    await state.set_state(None)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К группе", callback_data=f"asell_grp:detail:{grp_id}")]
    ])
    await message.answer(f"✅ Название группы изменено на <b>«{html.escape(new_name)}»</b>!", reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("asell_grp:del:"))
async def asell_grp_delete(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, grp_id_str = callback.data.split(":", 2)
    await db.delete_sell_group(int(grp_id_str))
    await callback.answer("🗑 Группа удалена (товары перенесены в общий список).", show_alert=True)
    await asell_groups_list(callback, db, settings, state)
# --- ДОБАВЛЕНИЕ, РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ ТОВАРОВ СДАЧИ ---

@router.callback_query(F.data == "asell:add")
async def asell_add_start(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    groups = await db.get_sell_groups()
    if groups:
        rows = [
            [InlineKeyboardButton(text=f"📁 {g['name']}", callback_data=f"asell_add_grp:{g['id']}")]
            for g in groups
        ]
        rows.append([InlineKeyboardButton(text="📄 Без группы (в общий список)", callback_data="asell_add_grp:0")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_sell")])
        await state.set_state(AdminSell.add_group)
        await callback.message.edit_text(
            "Выберите <b>группу</b> для нового принимаемого товара:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.update_data(sell_group_id=None)
    await state.set_state(AdminSell.add_display_name)
    await callback.message.edit_text(
        "Введите <b>название товара для игроков</b> (или /cancel):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AdminSell.add_group, F.data.startswith("asell_add_grp:"))
async def asell_add_choose_group(callback: CallbackQuery, state: FSMContext):
    _, grp_id_str = callback.data.split(":", 1)
    grp_id = int(grp_id_str) if grp_id_str != "0" else None
    await state.update_data(sell_group_id=grp_id)
    await state.set_state(AdminSell.add_display_name)
    await callback.message.edit_text(
        "Введите <b>название товара для игроков</b> (или /cancel):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminSell.add_display_name)
async def asell_add_display_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление товара отменено.")
        return
    if not name:
        await message.answer("❌ Название не может быть пустым.")
        return

    await state.update_data(sell_display_name=name)
    await state.set_state(AdminSell.add_classname)
    await message.answer(
        "Введите <b>игровой Classname предмета</b> из DayZ (например: <code>M4A1</code>, <code>GoldBar</code>):",
        parse_mode="HTML",
    )


@router.message(AdminSell.add_classname)
async def asell_add_classname(message: Message, state: FSMContext):
    classname = (message.text or "").strip()
    if classname.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление товара отменено.")
        return
    if not CLASSNAME_RE.match(classname):
        await message.answer(
            "❌ Classname должен содержать только латинские буквы, цифры и <code>_</code>:",
            parse_mode="HTML",
        )
        return

    await state.update_data(sell_classname=classname)
    await state.set_state(AdminSell.add_price)
    await message.answer("Введите <b>сумму выплаты игроку</b> за сдачу этого предмета (целым числом):", parse_mode="HTML")


@router.message(AdminSell.add_price)
async def asell_add_price(message: Message, state: FSMContext, db):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Добавление товара отменено.")
        return

    try:
        price = int(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Сумма выплаты должна быть положительным целым числом.")
        return

    data = await state.get_data()
    await db.add_sell_item(
        display_name=data["sell_display_name"],
        game_classname=data["sell_classname"],
        price=price,
        group_id=data.get("sell_group_id"),
    )
    await state.set_state(None)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель сдачи", callback_data="admin_sell")]
    ])
    await message.answer(
        f"✅ Товар <b>{html.escape(data['sell_display_name'])}</b> (<code>{html.escape(data['sell_classname'])}</code>) "
        f"с выплатой <b>{price} 🪙</b> добавлен в приемку!",
        reply_markup=markup,
        parse_mode="HTML",
    )
@router.callback_query(F.data.startswith("asell:item:"))
async def asell_item_detail(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, item_id_str = callback.data.split(":", 2)
    item_id = int(item_id_str)
    item = await db.get_sell_item(item_id)
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    grp_label = "Без группы"
    if item["group_id"]:
        grp = await db.get_sell_group(item["group_id"])
        if grp:
            grp_label = html.escape(grp["name"])

    text = (
        f"📦 <b>Редактирование товара приемки</b>\n\n"
        f"Группа: <b>{grp_label}</b>\n"
        f"Название в боте: <b>{html.escape(item['display_name'])}</b>\n"
        f"Classname в игре: <code>{html.escape(item['game_classname'])}</code>\n"
        f"Выплата при сдаче: <b>{item['price']} 🪙</b>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название в боте", callback_data=f"asell_edit:{item_id}:display_name")],
        [InlineKeyboardButton(text="🧬 Изменить Classname в игре", callback_data=f"asell_edit:{item_id}:game_classname")],
        [InlineKeyboardButton(text="💰 Изменить сумму выплаты", callback_data=f"asell_edit:{item_id}:price")],
        [InlineKeyboardButton(text="📁 Изменить группу", callback_data=f"asell_edit_grp:{item_id}")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data=f"asell_del:{item_id}")],
        [InlineKeyboardButton(text="⬅️ К списку товаров", callback_data="admin_sell")],
    ])
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("asell_edit:"))
async def asell_edit_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str, field = callback.data.split(":", 2)
    await state.update_data(asell_edit_id=int(item_id_str), asell_edit_field=field)
    await state.set_state(AdminSell.edit_value)

    prompts = {
        "display_name": "Введите новое <b>отображаемое название</b> товара (или /cancel):",
        "game_classname": "Введите новый <b>игровой Classname</b> предмета (или /cancel):",
        "price": "Введите новую <b>сумму выплаты</b> целым числом (или /cancel):",
    }
    await callback.message.edit_text(prompts[field], parse_mode="HTML")
    await callback.answer()


@router.message(AdminSell.edit_value)
async def asell_edit_save(message: Message, state: FSMContext, db):
    raw_val = (message.text or "").strip()
    if raw_val.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Редактирование отменено.")
        return

    data = await state.get_data()
    item_id = data["asell_edit_id"]
    field = data["asell_edit_field"]

    if field == "price":
        try:
            val = int(raw_val)
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Сумма должна быть положительным целым числом.")
            return
    elif field == "game_classname":
        if not CLASSNAME_RE.match(raw_val):
            await message.answer("❌ Classname должен содержать только латинские буквы, цифры и <code>_</code>:", parse_mode="HTML")
            return
        val = raw_val
    else:
        if not raw_val:
            await message.answer("❌ Значение не может быть пустым.")
            return
        val = raw_val

    await db.update_sell_item_field(item_id, field, val)
    await state.set_state(None)

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К товару", callback_data=f"asell:item:{item_id}")]
    ])
    await message.answer("✅ Изменения успешно сохранены!", reply_markup=markup)


@router.callback_query(F.data.startswith("asell_edit_grp:"))
async def asell_edit_group_menu(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str = callback.data.split(":", 1)
    item_id = int(item_id_str)
    groups = await db.get_sell_groups()

    rows = [
        [InlineKeyboardButton(text=f"📁 {g['name']}", callback_data=f"asell_set_grp:{item_id}:{g['id']}")]
        for g in groups
    ]
    rows.append([InlineKeyboardButton(text="📄 Без группы", callback_data=f"asell_set_grp:{item_id}:0")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к товару", callback_data=f"asell:item:{item_id}")])

    await callback.message.edit_text(
        "Выберите новую группу для товара:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asell_set_grp:"))
async def asell_set_group_save(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str, grp_id_str = callback.data.split(":", 2)
    item_id = int(item_id_str)
    grp_id = int(grp_id_str) if grp_id_str != "0" else None

    await db.update_sell_item_field(item_id, "group_id", grp_id)
    await callback.answer("✅ Группа обновлена!", show_alert=True)

    callback.data = f"asell:item:{item_id}"
    await asell_item_detail(callback, db, settings)


@router.callback_query(F.data.startswith("asell_del:"))
async def asell_delete(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str = callback.data.split(":", 1)
    await db.delete_sell_item(int(item_id_str))
    await callback.answer("🗑 Товар удален.", show_alert=True)
    await admin_sell_menu(callback, db, settings, state)


# ==========================================
# 6. АДМИН-ФИНАНСЫ (ADMIN_FINANCE_MENU)
# ==========================================

@router.message(F.text == "💸 Финансы (Админ)")
@router.callback_query(F.data == "admin_finance")
async def admin_finance_menu(
    event: Message | CallbackQuery,
    db=None,
    settings=None,
    state: FSMContext | None = None,
):
    if state:
        await state.set_state(None)

    if db and settings:
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

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "UPDATE users SET balance = MAX(0, balance + ?) WHERE id = ?",
            (delta, target["id"]),
        )
        cursor = await conn.execute("SELECT balance FROM users WHERE id = ?", (target["id"],))
        updated = await cursor.fetchone()
        new_balance = updated["balance"] if updated else 0
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
