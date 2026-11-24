import html
import re
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import DEFAULT_ECONOMY_CONFIG

router = Router()

CAT_NAMES = {
    "weapon": "🔫 Оружие",
    "ammo": "🧨 Патроны",
    "medical": "💊 Медицина",
    "food": "🥫 Еда",
    "resources": "🪵 Ресурсы",
    "gear": "🎒 Снаряжение",
}

ECO_LABELS = {
    "eco_start_bonus": ("🎁 Стартовый бонус", "монет при регистрации (≥ 0)"),
    "eco_online_reward": ("⏱ Доход за 15 мин онлайна", "монет раз в 15 мин (×4 = в час)"),
    "eco_daily_bonus": ("📅 Ежедневный бонус", "монет раз в сутки"),
    "eco_death_penalty": ("💀 Штраф за смерть", "монет списывается при смерти (число ≥ 0)"),
    "eco_debt_floor": ("📉 Лимит минуса (долга)", "отрицательное число или 0 (например: -300)"),
    "eco_kill_tier1": ("🥉 Килл: Обычный (0–2)", "монет за новичка"),
    "eco_kill_tier2": ("🥈 Килл: Опытный (3–5 / Топ 11–20)", "монет за опытного"),
    "eco_kill_tier3": ("🥇 Килл: Ветеран (6–9 / Топ 4–10)", "монет за ветерана"),
    "eco_kill_tier4": ("🎯 Килл: Цель №1 (10+ / Топ 1–3)", "монет за лидера топа"),
    "eco_kill_cooldown_hrs": ("⏳ Кулдаун на 1 цель (часов)", "часов защиты от перелива монет"),
}

CLASSNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class CheckoutOrder(StatesGroup):
    wait_coordinates = State()


class Transfer(StatesGroup):
    nick = State()
    amount = State()
    confirm = State()


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


class AdminEcoSettings(StatesGroup):
    wait_value = State()
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
    _, item_id_str, cat_key = parts[:3]
    sub_part = next(iter(parts[3:]), "0")
    sub_id = int(sub_part) if sub_part != "0" else None

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


def _empty_cart_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В каталог", callback_data="terminal")]
    ])


@router.callback_query(F.data == "cart:view")
async def cart_view(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})

    if not cart:
        await callback.message.edit_text("🛒 <b>Ваша корзина пуста.</b>", reply_markup=_empty_cart_markup(), parse_mode="HTML")
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

    if not cart:
        await callback.message.edit_text("🛒 <b>Ваша корзина пуста.</b>", reply_markup=_empty_cart_markup(), parse_mode="HTML")
        await callback.answer()
        return

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

    it = iter(coords)
    x_coord = float(next(it))
    if len(coords) >= 3:
        next(it)
    z_coord = float(next(it))

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
# 3. КОШЕЛЕК, ЕЖЕДНЕВНЫЙ БОНУС И ПЕРЕВОДЫ
# ==========================================

@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    eco = await db.get_economy_config()
    streak = int(user["killstreak"] or 0) if "killstreak" in user.keys() else 0
    daily_amt = eco["eco_daily_bonus"]

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎁 Ежедневный бонус (+{daily_amt} 🪙)", callback_data="daily_bonus")],
        [InlineKeyboardButton(text="💸 Передать валюту", callback_data="transfer")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    text = (
        f"💰 Ваш баланс: <b>{user['balance']} 🪙</b>\n"
        f"🔥 Текущая серия киллов: <b>{streak}</b>"
    )

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data == "daily_bonus")
async def daily_bonus_handler(callback: CallbackQuery, db, state: FSMContext):
    ok, msg = await db.claim_daily_bonus(callback.from_user.id)
    clean_alert = msg.replace("<b>", "").replace("</b>", "")
    await callback.answer(clean_alert, show_alert=True)
    if ok:
        await wallet(callback, db, state)


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
    receiver_nick = data.get("receiver_nick")
    if not receiver_nick:
        await state.set_state(None)
        await message.answer("❌ Сессия перевода устарела. Начните заново.")
        return

    await state.set_state(Transfer.confirm)
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="transfer:yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="transfer:no"),
    ]])
    await message.answer(
        f"Перевести <b>{amount} 🪙</b> игроку <code>{html.escape(receiver_nick)}</code>?",
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

    receiver_nick = data.get("receiver_nick")
    amount = data.get("amount")
    if not receiver_nick or not amount:
        await callback.message.edit_text("❌ Сессия перевода устарела. Начните заново.")
        await callback.answer()
        return

    user = await db.get_user(callback.from_user.id)
    ok, text = await db.transfer(user["id"], receiver_nick, amount)
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


async def _render_subcat_list(callback: CallbackQuery, db, cat_key: str) -> None:
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


@router.callback_query(F.data.startswith("ashop_grp_cat:"))
async def ashop_groups_list(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, cat_key = callback.data.split(":", 1)
    await _render_subcat_list(callback, db, cat_key)
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
    cat_key = data.get("grp_cat_key")
    await state.set_state(None)
    if not cat_key:
        await message.answer("❌ Сессия устарела. Откройте админ-панель магазина заново.")
        return

    try:
        await db.add_subcategory(cat_key, name)
    except Exception:
        await message.answer("❌ Такая группа уже существует в этом разделе.")
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К группам раздела", callback_data=f"ashop_grp_cat:{cat_key}")]
    ])
    await message.answer(f"✅ Группа <b>«{html.escape(name)}»</b> создана!", reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("ashop_grp_del:"))
async def ashop_grp_delete(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, cat_key, sub_id_str = callback.data.split(":", 2)
    await db.delete_subcategory(int(sub_id_str))
    await callback.answer("🗑 Группа удалена (товары перенесены в общий раздел).", show_alert=True)
    await _render_subcat_list(callback, db, cat_key)


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
    await state.set_state(None)
    classname = data.get("game_classname")
    category = data.get("category")
    display_name = data.get("display_name")
    if not classname or not category or not display_name:
        await message.answer("❌ Сессия добавления товара устарела. Начните заново.")
        return

    await db.add_shop_item(
        category=category,
        display_name=display_name,
        game_classname=classname,
        price=price,
        subcategory_id=data.get("subcategory_id"),
    )
    event_synced = await nitrado_api.ensure_shop_event_exists(classname)

    sync_note = (
        f"\n🧬 Событие <code>{html.escape(classname)}</code> синхронизировано с сервером."
        if event_synced
        else "\n⚠️ <i>Не удалось автоматически записать событие в shop_events.xml — проверьте FTP.</i>"
    )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель магазина", callback_data="admin_shop")]
    ])
    await message.answer(
        f"✅ Товар <b>{html.escape(display_name)}</b> (<code>{html.escape(classname)}</code>) за {price} 🪙 добавлен!{sync_note}",
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
    prompts = {
        "display_name": "Введите новое <b>отображаемое название</b> товара (или /cancel):",
        "game_classname": "Введите новый <b>игровой Classname</b> предмета (или /cancel):",
        "price": "Введите новую <b>цену</b> целым числом (или /cancel):",
    }
    if field not in prompts:
        await callback.answer("Неизвестное поле.", show_alert=True)
        return

    await state.update_data(edit_item_id=int(item_id_str), edit_field=field)
    await state.set_state(AdminShop.edit_value)
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
    item_id = data.get("edit_item_id")
    field = data.get("edit_field")
    if not item_id or not field:
        await state.set_state(None)
        await message.answer("❌ Сессия редактирования устарела. Откройте товар заново.")
        return

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
# 5. АДМИН-ФИНАНСЫ (РУЧНОЕ ИЗМЕНЕНИЕ БАЛАНСА И НАСТРОЙКИ ЭКОНОМИКИ)
# ==========================================

@router.message(F.text == "💳 Управление балансами")
@router.callback_query(F.data == "admin_finance")
async def admin_finance_menu(event: Message | CallbackQuery, db, settings, state: FSMContext):
    if state:
        await state.set_state(None)
    user = await db.get_user(event.from_user.id)
    if not _is_admin(user, settings):
        return

    users = await db.fetchall("SELECT id, game_nick, balance FROM users ORDER BY game_nick ASC")
    rows = [
        [InlineKeyboardButton(text="⚙️ Настройки экономики (бонусы, киллы, штрафы)", callback_data="aeco:menu")]
    ]
    for u in users:
        rows.append([
            InlineKeyboardButton(
                text=f"{u['game_nick']} — {u['balance']} 🪙",
                callback_data=f"afin_user:{u['id']}",
            )
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = (
        "💳 <b>Управление балансами и экономикой</b>\n"
        "Выберите игрока для изменения баланса или откройте настройки экономики:"
    )

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data.startswith("afin_user:"))
async def afin_select_user(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, target_id_str = callback.data.split(":", 1)
    target_id = int(target_id_str)
    target = await db.fetchone("SELECT id, game_nick, balance FROM users WHERE id = ?", (target_id,))
    if not target:
        await callback.answer("Игрок не найден.", show_alert=True)
        return

    await state.update_data(afin_target_id=target_id)
    await state.set_state(AdminFinance.wait_for_balance)
    await callback.message.edit_text(
        f"Игрок: <b>{html.escape(target['game_nick'])}</b>\n"
        f"Текущий баланс: <b>{target['balance']} 🪙</b>\n\n"
        "Введите сумму для начисления (например: <code>500</code>) или списания (например: <code>-200</code>).\n"
        "Для отмены введите /cancel.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminFinance.wait_for_balance)
async def afin_apply_balance(message: Message, state: FSMContext, db):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Изменение баланса отменено.")
        return

    try:
        delta = int(raw)
        if delta == 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите ненулевое целое число (например: <code>250</code> или <code>-100</code>).", parse_mode="HTML")
        return

    data = await state.get_data()
    target_id = data.get("afin_target_id")
    await state.set_state(None)
    if not target_id:
        await message.answer("❌ Сессия устарела. Выберите игрока заново.")
        return

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cur = await conn.execute("SELECT game_nick, balance FROM users WHERE id = ?", (target_id,))
        row = await cur.fetchone()
        if not row:
            await conn.rollback()
            await message.answer("❌ Игрок не найден.")
            return

        new_balance = row["balance"] + delta
        await conn.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, target_id))
        await conn.execute(
            "INSERT INTO transactions (receiver_id, amount, kind) VALUES (?, ?, 'admin_adjust')",
            (target_id, delta),
        )
        await conn.commit()

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К списку игроков", callback_data="admin_finance")]
    ])
    sign = "+" if delta > 0 else ""
    await message.answer(
        f"✅ Баланс игрока <b>{html.escape(row['game_nick'])}</b> изменён на <b>{sign}{delta} 🪙</b>.\n"
        f"Новый баланс: <b>{new_balance} 🪙</b>",
        reply_markup=markup,
        parse_mode="HTML",
    )
# --- НАСТРОЙКИ ЭКОНОМИКИ ДЛЯ АДМИНИСТРАТОРА ---

async def _render_eco_menu(event: Message | CallbackQuery, db, state: FSMContext) -> None:
    await state.set_state(None)
    eco = await db.get_economy_config()

    lines = ["⚙️ <b>Настройки экономики сервера</b>\nНажмите на параметр, чтобы изменить его значение:\n"]
    rows = []

    for key, (title, hint) in ECO_LABELS.items():
        val = eco[key]
        lines.append(f"• {title}: <b>{val}</b> — <i>{hint}</i>")
        rows.append([
            InlineKeyboardButton(
                text=f"{title}: {val}",
                callback_data=f"aeco_set:{key}",
            )
        ])

    rows.append([InlineKeyboardButton(text="🔄 Сбросить по умолчанию", callback_data="aeco:reset")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к балансам", callback_data="admin_finance")])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "\n".join(lines)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "aeco:menu")
async def aeco_menu(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    await _render_eco_menu(callback, db, state)


@router.callback_query(F.data.startswith("aeco_set:"))
async def aeco_set_prompt(callback: CallbackQuery, state: FSMContext, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, key = callback.data.split(":", 1)
    if key not in ECO_LABELS:
        await callback.answer("Неизвестный параметр.", show_alert=True)
        return

    eco = await db.get_economy_config()
    title, hint = ECO_LABELS[key]
    default_val = DEFAULT_ECONOMY_CONFIG[key]

    await state.update_data(aeco_key=key)
    await state.set_state(AdminEcoSettings.wait_value)
    await callback.message.edit_text(
        f"⚙️ <b>{title}</b>\n"
        f"Текущее значение: <b>{eco[key]}</b> (по умолчанию: <code>{default_val}</code>)\n"
        f"Требование: <i>{hint}</i>\n\n"
        "Введите новое целое число или /cancel для отмены:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminEcoSettings.wait_value)
async def aeco_set_save(message: Message, state: FSMContext, db):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("Изменение параметра отменено.")
        return

    data = await state.get_data()
    key = data.get("aeco_key")
    if not key or key not in ECO_LABELS:
        await state.set_state(None)
        await message.answer("❌ Сессия настройки устарела. Откройте меню заново.")
        return

    try:
        val = int(raw)
    except ValueError:
        await message.answer("❌ Введите корректное целое число.")
        return

    if key == "eco_debt_floor":
        if val > 0:
            await message.answer("❌ Лимит долга должен быть отрицательным числом или 0 (например: <code>-300</code>).", parse_mode="HTML")
            return
    else:
        if val < 0:
            await message.answer("❌ Значение этого параметра не может быть отрицательным (введите число ≥ 0).")
            return

    await db.update_economy_setting(key, val)
    await state.set_state(None)

    title, _ = ECO_LABELS[key]
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К настройкам экономики", callback_data="aeco:menu")]
    ])
    await message.answer(
        f"✅ Параметр <b>{title}</b> обновлён: <b>{val}</b>",
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "aeco:reset")
async def aeco_reset_defaults(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    for key, default_val in DEFAULT_ECONOMY_CONFIG.items():
        await db.update_economy_setting(key, default_val)

    await callback.answer("🔄 Настройки экономики сброшены к значениям по умолчанию.", show_alert=True)
    await _render_eco_menu(callback, db, state)
