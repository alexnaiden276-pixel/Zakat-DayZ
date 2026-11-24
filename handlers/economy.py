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
    "eco_online_reward": ("⏱ Доход за 1 час онлайна", "монет раз в час"),
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

    if getattr(callback, "id", None) == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await callback.answer()
@router.callback_query(F.data.startswith("cat:"))
async def open_category(callback: CallbackQuery, db, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key not in CAT_NAMES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=None)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("subcat:"))
async def open_subcategory(callback: CallbackQuery, db, state: FSMContext):
    _, key, sub_id_str = callback.data.split(":", 2)
    if key not in CAT_NAMES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    try:
        sub_id = int(sub_id_str)
    except ValueError:
        await callback.answer("Неверная подкатегория", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=sub_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("cart_add:"))
async def add_to_cart(callback: CallbackQuery, db, state: FSMContext):
    parts = callback.data.split(":")
    item_id = int(parts[1])
    key = parts[2] if len(parts) > 2 else None
    sub_id = int(parts[3]) if len(parts) > 3 and parts[3] not in ("", "0") else None

    item = await db.fetchone(
        "SELECT id, display_name, price, is_active FROM shop_items WHERE id = ?",
        (item_id,),
    )
    if not item or not item["is_active"]:
        await callback.answer("Товар недоступен", show_alert=True)
        return

    data = await state.get_data()
    cart = dict(data.get("cart", {}))
    str_id = str(item_id)
    cart[str_id] = cart.get(str_id, 0) + 1
    await state.update_data(cart=cart)

    await callback.answer(f"✅ Добавлено: {item['display_name']} (в корзине: {cart[str_id]} шт.)")

    if key and key in CAT_NAMES:
        text, markup = await _build_category_view(db, state, key, sub_id=sub_id)
        try:
            await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass


async def _render_cart(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    cart: dict[str, int] = data.get("cart", {})

    if not cart:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🖥 В терминал", callback_data="terminal")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
        ])
        await callback.message.edit_text(
            "🛒 <b>Ваша корзина пуста</b>\nДобавьте товары из терминала снабжения.",
            reply_markup=kb,
            parse_mode="HTML",
        )
        await callback.answer()
        return

    total_sum = 0
    lines = ["🛒 <b>Ваша корзина</b>\n"]
    rows = []

    for str_id, qty in list(cart.items()):
        item = await db.fetchone(
            "SELECT id, display_name, price, is_active FROM shop_items WHERE id = ?",
            (int(str_id),),
        )
        if not item or not item["is_active"]:
            cart.pop(str_id, None)
            continue
        line_total = int(item["price"]) * int(qty)
        total_sum += line_total
        lines.append(
            f"• {html.escape(str(item['display_name']))} × {qty} = <b>{line_total} 🪙</b>"
        )
        rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"cart_dec:{item['id']}"),
            InlineKeyboardButton(text=f"{qty} шт.", callback_data="cart:noop"),
            InlineKeyboardButton(text="➕", callback_data=f"cart_inc:{item['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"cart_del:{item['id']}"),
        ])

    await state.update_data(cart=cart)
    if not cart:
        await _render_cart(callback, db, state)
        return

    lines.append(f"\n💰 <b>Итого к оплате: {total_sum} 🪙</b>")
    rows.append([InlineKeyboardButton(text=f"✅ Оформить заказ ({total_sum} 🪙)", callback_data="cart:checkout")])
    rows.append([InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="cart:clear")])
    rows.append([InlineKeyboardButton(text="🖥 Продолжить покупки", callback_data="terminal")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()
@router.callback_query(F.data == "cart:view")
async def view_cart(callback: CallbackQuery, db, state: FSMContext):
    await _render_cart(callback, db, state)


@router.callback_query(F.data == "cart:noop")
async def cart_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "cart:clear")
async def clear_cart(callback: CallbackQuery, db, state: FSMContext):
    await state.update_data(cart={})
    await _render_cart(callback, db, state)


@router.callback_query(F.data.startswith("cart_inc:"))
async def cart_inc(callback: CallbackQuery, db, state: FSMContext):
    item_id = callback.data.split(":", 1)
    data = await state.get_data()
    cart = dict(data.get("cart", {}))
    cart[item_id] = cart.get(item_id, 0) + 1
    await state.update_data(cart=cart)
    await _render_cart(callback, db, state)


@router.callback_query(F.data.startswith("cart_dec:"))
async def cart_dec(callback: CallbackQuery, db, state: FSMContext):
    item_id = callback.data.split(":", 1)
    data = await state.get_data()
    cart = dict(data.get("cart", {}))
    cur = cart.get(item_id, 0)
    if cur <= 1:
        cart.pop(item_id, None)
    else:
        cart[item_id] = cur - 1
    await state.update_data(cart=cart)
    await _render_cart(callback, db, state)


@router.callback_query(F.data.startswith("cart_del:"))
async def cart_del(callback: CallbackQuery, db, state: FSMContext):
    item_id = callback.data.split(":", 1)
    data = await state.get_data()
    cart = dict(data.get("cart", {}))
    cart.pop(item_id, None)
    await state.update_data(cart=cart)
    await _render_cart(callback, db, state)


@router.callback_query(F.data == "cart:checkout")
async def start_checkout(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    if not cart:
        await callback.answer("Корзина пуста", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return

    total_sum = 0
    for str_id, qty in cart.items():
        item = await db.fetchone(
            "SELECT price, is_active FROM shop_items WHERE id = ?",
            (int(str_id),),
        )
        if item and item["is_active"]:
            total_sum += int(item["price"]) * int(qty)

    if int(user["balance"]) < total_sum:
        await callback.answer(
            f"Недостаточно средств! Нужно {total_sum} 🪙, на балансе {user['balance']} 🪙.",
            show_alert=True,
        )
        return

    await state.set_state(CheckoutOrder.wait_coordinates)
    await callback.message.answer(
        f"📍 <b>Оформление заказа ({total_sum} 🪙)</b>\n\n"
        "Введите координаты точки спавна <b>X</b> и <b>Z</b> с карты iZurvive через пробел.\n"
        "Например: <code>4512.5 10234.8</code>\n\n"
        "Для отмены отправьте <code>/cancel</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CheckoutOrder.wait_coordinates)
async def process_checkout_coordinates(message: Message, db, nitrado_api, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Оформление заказа отменено.")
        return

    parts = raw.replace(",", ".").split()
    if len(parts) != 2:
        await message.answer(
            "❌ Введите ровно два числа через пробел — <b>X</b> и <b>Z</b> (например: <code>4512.5 10234.8</code>):",
            parse_mode="HTML",
        )
        return

    try:
        x = float(parts)
        z = float(parts)
    except ValueError:
        await message.answer("❌ Координаты должны быть числами. Попробуйте снова или введите /cancel.")
        return
    user = await db.get_user(message.from_user.id)
    if not user:
        await state.clear()
        await message.answer("❌ Профиль не найден. Введите /start.")
        return

    data = await state.get_data()
    cart: dict[str, int] = data.get("cart", {})
    if not cart:
        await state.clear()
        await message.answer("❌ Корзина пуста.")
        return

    order_items = []
    total_sum = 0
    for str_id, qty in cart.items():
        item = await db.fetchone(
            "SELECT id, display_name, game_classname, price, is_active FROM shop_items WHERE id = ?",
            (int(str_id),),
        )
        if not item or not item["is_active"]:
            continue
        q = int(qty)
        p = int(item["price"])
        total_sum += p * q
        order_items.append({
            "item_id": int(item["id"]),
            "display_name": item["display_name"],
            "game_classname": item["game_classname"],
            "qty": q,
            "price": p,
        })

    if not order_items:
        await state.clear()
        await message.answer("❌ Выбранные товары больше недоступны.")
        return

    if int(user["balance"]) < total_sum:
        await state.clear()
        await message.answer(
            f"❌ Недостаточно средств! Нужно {total_sum} 🪙, на балансе {user['balance']} 🪙."
        )
        return

    for it in order_items:
        ok_ev = await nitrado_api.ensure_shop_event_exists(it["game_classname"])
        if not ok_ev:
            await message.answer(
                f"❌ Не удалось подготовить XML-эвент для предмета <code>{html.escape(str(it['game_classname']))}</code>.",
                parse_mode="HTML",
            )
            return

    ok_spawn = await nitrado_api.append_event_spawn_orders(order_items, x=x, z=z, radius=3.0)
    if not ok_spawn:
        await message.answer("❌ Не удалось записать заказ в файлы спавна сервера. Монеты не списаны.")
        return

    await db.create_shop_order(
        user_id=int(user["id"]),
        items=order_items,
        total_price=total_sum,
        coord_x=x,
        coord_z=z,
    )
    await state.clear()

    new_bal = int(user["balance"]) - total_sum
    await message.answer(
        f"✅ <b>Заказ оформлен!</b>\n"
        f"📍 Координаты: <b>X={x}, Z={z}</b>\n"
        f"💸 Списано: <b>{total_sum} 🪙</b> | Остаток: <b>{new_bal} 🪙</b>\n"
        f"🔄 Лут появится после ближайшего рестарта сервера.",
        parse_mode="HTML",
    )


# ==========================================
# 2. КОШЕЛЁК, ЕЖЕДНЕВНЫЙ БОНУС И ПЕРЕВОДЫ
# ==========================================

@router.callback_query(F.data == "wallet")
async def wallet(callback: CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return

    cfg = await db.get_economy_config()
    daily_bonus = int(cfg.get("eco_daily_bonus", DEFAULT_ECONOMY_CONFIG["eco_daily_bonus"]))
    online_reward = int(cfg.get("eco_online_reward", DEFAULT_ECONOMY_CONFIG["eco_online_reward"]))
    streak = int(user["kill_streak"] or 0)
    balance = int(user["balance"] or 0)

    text = (
        f"💰 <b>Ваш кошелёк</b>\n\n"
        f"👤 Игрок: <b>{html.escape(str(user['game_nick']))}</b>\n"
        f"💵 Баланс: <b>{balance} 🪙</b>\n"
        f"🔥 Серия убийств: <b>{streak}</b>\n"
        f"⏱ Награда за онлайн: <b>+{online_reward} 🪙/час</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎁 Ежедневный бонус (+{daily_bonus} 🪙)", callback_data="daily_bonus")],
        [InlineKeyboardButton(text="💸 Перевести другому игроку", callback_data="transfer:start")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu")],
    ])

    if getattr(callback, "id", None) == "0":
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data == "daily_bonus")
async def claim_daily(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    ok, msg = await db.claim_daily_bonus(int(user["id"]))
    clean_msg = re.sub(r"<[^>]+>", "", msg)
    await callback.answer(clean_msg, show_alert=True)
    if ok:
        await wallet(callback, db, state)
@router.callback_query(F.data == "transfer:start")
async def transfer_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Transfer.nick)
    await callback.message.answer(
        "💸 <b>Перевод монет</b>\nВведите игровой ник получателя (или <code>/cancel</code> для отмены):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Transfer.nick)
async def transfer_nick(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Перевод отменён.")
        return

    target = await db.get_user_by_nick(raw)
    if not target:
        await message.answer("❌ Игрок с таким ником не найден. Проверьте написание или введите /cancel:")
        return

    if int(target["telegram_id"]) == message.from_user.id:
        await message.answer("❌ Нельзя переводить монеты самому себе. Введите ник другого игрока:")
        return

    await state.update_data(target_id=int(target["id"]), target_nick=str(target["game_nick"]))
    await state.set_state(Transfer.amount)
    await message.answer(
        f"👤 Получатель: <b>{html.escape(str(target['game_nick']))}</b>\n"
        "Введите сумму перевода целым положительным числом:",
        parse_mode="HTML",
    )


@router.message(Transfer.amount)
async def transfer_amount(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Перевод отменён.")
        return

    try:
        amount = int(raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите положительное целое число (например: <code>100</code>):", parse_mode="HTML")
        return

    sender = await db.get_user(message.from_user.id)
    if not sender or int(sender["balance"]) < amount:
        bal = int(sender["balance"]) if sender else 0
        await message.answer(f"❌ Недостаточно средств. Ваш баланс: <b>{bal} 🪙</b>.", parse_mode="HTML")
        return

    data = await state.get_data()
    await state.update_data(amount=amount)
    await state.set_state(Transfer.confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="transfer:yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="transfer:no"),
        ]
    ])
    await message.answer(
        f"Подтвердите перевод:\n"
        f"👤 Кому: <b>{html.escape(str(data['target_nick']))}</b>\n"
        f"💰 Сумма: <b>{amount} 🪙</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(Transfer.confirm, F.data == "transfer:no")
async def transfer_no(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Перевод отменён.")
    await callback.answer()


@router.callback_query(Transfer.confirm, F.data == "transfer:yes")
async def transfer_yes(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    sender = await db.get_user(callback.from_user.id)
    target_id = data.get("target_id")
    target_nick = data.get("target_nick", "Игрок")
    amount = int(data.get("amount", 0))

    if not sender or not target_id or amount <= 0 or int(sender["balance"]) < amount:
        await callback.message.edit_text("❌ Ошибка выполнения перевода (недостаточно средств или данные устарели).")
        await callback.answer()
        return

    await db.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, int(sender["id"])))
    await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, int(target_id)))
    await db.execute(
        "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, ?, ?, ?)",
        (int(sender["id"]), int(target_id), amount, f"Перевод игроку {target_nick}"),
    )

    await callback.message.edit_text(
        f"✅ Вы успешно перевели <b>{amount} 🪙</b> игроку <b>{html.escape(str(target_nick))}</b>!",
        parse_mode="HTML",
    )
    await callback.answer()
# ==========================================
# 3. АДМИН-МЕНЮ УПРАВЛЕНИЯ МАГАЗИНОМ
# ==========================================

@router.callback_query(F.data == "admin_shop")
async def admin_shop_menu(event: Message | CallbackQuery, db, settings, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(event.from_user.id)
    if not _is_admin(user, settings):
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ запрещён", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="ashop:add")],
        [InlineKeyboardButton(text="📋 Список и редактирование товаров", callback_data="ashop:list:weapon")],
        [InlineKeyboardButton(text="⬅️ Назад в админ-панель", callback_data="menu")],
    ])
    text = "🛠 <b>Управление магазином</b>\nВыберите действие:"

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "ashop:add")
async def ashop_add_start(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
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
async def ashop_add_category(callback: CallbackQuery, db, state: FSMContext):
    cat = callback.data.split(":", 1)
    if cat not in CAT_NAMES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return

    await state.update_data(category=cat)
    subcats = await db.get_subcategories(cat)

    rows = [
        [InlineKeyboardButton(text=f"📁 {sc['name']}", callback_data=f"ashop_sub:{sc['id']}")]
        for sc in subcats
    ]
    rows.append([InlineKeyboardButton(text="➕ Новая подкатегория", callback_data="ashop_sub_new")])
    rows.append([InlineKeyboardButton(text="⏭ Без подкатегории", callback_data="ashop_sub:0")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")])

    await state.set_state(AdminShop.add_subcategory)
    await callback.message.edit_text(
        f"Категория: <b>{CAT_NAMES[cat]}</b>\nВыберите или создайте подкатегорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AdminShop.add_subcategory, F.data == "ashop_sub_new")
async def ashop_sub_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminShop.new_subcat_name)
    await callback.message.edit_text(
        "Введите название новой подкатегории (например: <code>Штурмовые винтовки</code>):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.new_subcat_name)
async def ashop_sub_new_name(message: Message, db, state: FSMContext):
    sub_name = (message.text or "").strip()
    if not sub_name or sub_name.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление товара отменено.")
        return

    data = await state.get_data()
    cat = data["category"]
    sub_id = await db.add_subcategory(cat, sub_name)
    await state.update_data(subcategory_id=sub_id)
    await state.set_state(AdminShop.add_display_name)
    await message.answer(
        f"Подкатегория: <b>{html.escape(sub_name)}</b>\n"
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        parse_mode="HTML",
    )


@router.callback_query(AdminShop.add_subcategory, F.data.startswith("ashop_sub:"))
async def ashop_add_subcategory(callback: CallbackQuery, state: FSMContext):
    sub_id = int(callback.data.split(":", 1))
    await state.update_data(subcategory_id=sub_id if sub_id > 0 else None)
    await state.set_state(AdminShop.add_display_name)
    await callback.message.edit_text(
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        parse_mode="HTML",
    )
    await callback.answer()
@router.message(AdminShop.add_display_name)
async def ashop_add_display_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or name.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление товара отменено.")
        return

    await state.update_data(display_name=name)
    await state.set_state(AdminShop.add_classname)
    await message.answer(
        "Введите игровой <b>Classname</b> предмета (латиницей, например: <code>AK74</code>):",
        parse_mode="HTML",
    )


@router.message(AdminShop.add_classname)
async def ashop_add_classname(message: Message, state: FSMContext):
    cname = (message.text or "").strip()
    if cname.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление товара отменено.")
        return

    if not CLASSNAME_RE.match(cname):
        await message.answer(
            "❌ Некорректный Classname. Разрешены только латинские буквы, цифры и <code>_</code>:",
            parse_mode="HTML",
        )
        return

    await state.update_data(game_classname=cname)
    await state.set_state(AdminShop.add_price)
    await message.answer("Введите цену товара в монетах (целое положительное число):")


@router.message(AdminShop.add_price)
async def ashop_add_price(message: Message, db, nitrado_api, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление товара отменено.")
        return

    try:
        price = int(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Цена должна быть целым числом больше 0:")
        return

    data = await state.get_data()
    await state.clear()

    cat = data["category"]
    sub_id = data.get("subcategory_id")
    dname = data["display_name"]
    cname = data["game_classname"]

    await db.add_shop_item(
        category=cat,
        display_name=dname,
        game_classname=cname,
        price=price,
        subcategory_id=sub_id,
    )
    xml_ok = await nitrado_api.ensure_shop_event_exists(cname)
    xml_note = (
        "✅ Эвент в <code>shop_events.xml</code> создан."
        if xml_ok
        else "⚠️ Не удалось обновить <code>shop_events.xml</code> по FTP."
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё товар", callback_data="ashop:add")],
        [InlineKeyboardButton(text="⬅️ В управление магазином", callback_data="admin_shop")],
    ])
    await message.answer(
        f"✅ Товар <b>{html.escape(dname)}</b> (<code>{cname}</code>) за <b>{price} 🪙</b> добавлен!\n{xml_note}",
        reply_markup=kb,
        parse_mode="HTML",
    )
@router.callback_query(F.data.startswith("ashop:list:"))
async def ashop_list(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    cat = callback.data.split(":", 2)[2]
    items = await db.fetchall(
        "SELECT id, display_name, price, is_active FROM shop_items WHERE category = ? ORDER BY id ASC",
        (cat,),
    )

    cat_tabs = [
        InlineKeyboardButton(
            text=("• " if k == cat else "") + title.split(" ", 1)[-1],
            callback_data=f"ashop:list:{k}",
        )
        for k, title in CAT_NAMES.items()
    ]
    rows = [cat_tabs[:3], cat_tabs[3:]]

    for it in items:
        status_icon = "🟢" if int(it["is_active"]) == 1 else "🔴"
        rows.append([
            InlineKeyboardButton(
                text=f"{status_icon} {it['display_name']} — {it['price']} 🪙",
                callback_data=f"ashop:item:{it['id']}",
            )
        ])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_shop")])
    await callback.message.edit_text(
        f"📋 <b>Товары категории: {CAT_NAMES.get(cat, cat)}</b>\nНажмите на товар для редактирования:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop:item:"))
async def ashop_item_detail(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    item_id = int(callback.data.split(":", 2)[2])
    item = await db.fetchone("SELECT * FROM shop_items WHERE id = ?", (item_id,))
    if not item:
        await callback.answer("Товар не найден", show_alert=True)
        return

    status_text = "🟢 Активен" if int(item["is_active"]) == 1 else "🔴 Скрыт"
    sub_name = "—"
    if item["subcategory_id"]:
        sub_row = await db.get_subcategory(int(item["subcategory_id"]))
        if sub_row:
            sub_name = html.escape(str(sub_row["name"]))

    text = (
        f"📦 <b>Товар #{item['id']}</b>\n\n"
        f"• Категория: <b>{CAT_NAMES.get(item['category'], item['category'])}</b>\n"
        f"• Подкатегория: <b>{sub_name}</b>\n"
        f"• Название: <b>{html.escape(str(item['display_name']))}</b>\n"
        f"• Classname: <code>{html.escape(str(item['game_classname']))}</code>\n"
        f"• Цена: <b>{item['price']} 🪙</b>\n"
        f"• Статус: <b>{status_text}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Название", callback_data=f"ashop:edit:{item_id}:display_name"),
            InlineKeyboardButton(text="💰 Цена", callback_data=f"ashop:edit:{item_id}:price"),
        ],
        [
            InlineKeyboardButton(text="🧬 Classname", callback_data=f"ashop:edit:{item_id}:game_classname"),
            InlineKeyboardButton(text="👁 Вкл/Выкл показ", callback_data=f"ashop:toggle:{item_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ashop:del:{item_id}"),
            InlineKeyboardButton(text="⬅️ К списку", callback_data=f"ashop:list:{item['category']}"),
        ],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("ashop:toggle:"))
async def ashop_item_toggle(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    item_id = int(callback.data.split(":", 2)[2])
    item = await db.fetchone("SELECT is_active FROM shop_items WHERE id = ?", (item_id,))
    if not item:
        return

    new_val = 0 if int(item["is_active"]) == 1 else 1
    await db.execute("UPDATE shop_items SET is_active = ? WHERE id = ?", (new_val, item_id))
    callback.data = f"ashop:item:{item_id}"
    await ashop_item_detail(callback, db, settings)


@router.callback_query(F.data.startswith("ashop:del:"))
async def ashop_item_delete(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    item_id = int(callback.data.split(":", 2)[2])
    item = await db.fetchone("SELECT category FROM shop_items WHERE id = ?", (item_id,))
    cat = item["category"] if item else "weapon"

    await db.delete_shop_item(item_id)
    await callback.answer("🗑 Товар удалён", show_alert=True)
    callback.data = f"ashop:list:{cat}"
    await ashop_list(callback, db, settings)
@router.callback_query(F.data.startswith("ashop:edit:"))
async def ashop_item_edit(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, item_id_str, field = callback.data.split(":", 3)
    prompts = {
        "display_name": "Введите новое отображаемое название товара:",
        "price": "Введите новую цену товара в монетах (целое число > 0):",
        "game_classname": "Введите новый игровой Classname предмета (латиницей):",
    }
    if field not in prompts:
        return

    await state.update_data(edit_item_id=int(item_id_str), edit_field=field)
    await state.set_state(AdminShop.edit_value)
    await callback.message.answer(
        f"{prompts[field]}\nДля отмены введите <code>/cancel</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.edit_value)
async def ashop_item_edit_save(message: Message, db, nitrado_api, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Редактирование отменено.")
        return

    data = await state.get_data()
    item_id = int(data["edit_item_id"])
    field = data["edit_field"]

    if field == "price":
        try:
            val = int(raw)
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Цена должна быть целым числом больше 0:")
            return
    elif field == "game_classname":
        if not CLASSNAME_RE.match(raw):
            await message.answer(
                "❌ Некорректный Classname. Разрешены только латинские буквы, цифры и <code>_</code>:",
                parse_mode="HTML",
            )
            return
        val = raw
    else:
        val = raw

    await db.update_shop_item_field(item_id, field, val)
    if field == "game_classname":
        await nitrado_api.ensure_shop_event_exists(val)

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к товару", callback_data=f"ashop:item:{item_id}")]
    ])
    await message.answer("✅ Параметр товара успешно обновлён!", reply_markup=kb)


# ==========================================
# 4. АДМИН-МЕНЮ ФИНАНСОВ И НАСТРОЕК ЭКОНОМИКИ
# ==========================================

@router.callback_query(F.data == "admin_finance")
async def admin_finance_menu(event: Message | CallbackQuery, db, settings, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(event.from_user.id)
    if not _is_admin(user, settings):
        if isinstance(event, CallbackQuery):
            await event.answer("Доступ запрещён", show_alert=True)
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
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
@router.callback_query(F.data.startswith("afin_user:"))
async def afin_select_user(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, target_id_str = callback.data.split(":", 1)
    target_id = int(target_id_str)
    target = await db.fetchone("SELECT id, game_nick, balance FROM users WHERE id = ?", (target_id,))
    if not target:
        await callback.answer("Игрок не найден", show_alert=True)
        return

    await state.update_data(afin_target_id=target_id, afin_target_nick=str(target["game_nick"]))
    await state.set_state(AdminFinance.wait_for_balance)
    await callback.message.answer(
        f"👤 Игрок: <b>{html.escape(str(target['game_nick']))}</b>\n"
        f"💰 Текущий баланс: <b>{target['balance']} 🪙</b>\n\n"
        "Введите сумму для начисления (например: <code>500</code>) или списания (например: <code>-200</code>).\n"
        "Для отмены введите <code>/cancel</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminFinance.wait_for_balance)
async def afin_apply_balance(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Изменение баланса отменено.")
        return

    try:
        delta = int(raw)
        if delta == 0:
            raise ValueError
    except ValueError:
        await message.answer(
            "❌ Введите ненулевое целое число (например: <code>500</code> или <code>-200</code>):",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    await state.clear()

    target_id = int(data["afin_target_id"])
    target_nick = data.get("afin_target_nick", "Игрок")

    await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (delta, target_id))
    await db.execute(
        "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (NULL, ?, ?, ?)",
        (target_id, delta, "Корректировка баланса администратором"),
    )

    row = await db.fetchone("SELECT balance FROM users WHERE id = ?", (target_id,))
    new_bal = row["balance"] if row else 0
    sign = "+" if delta > 0 else ""

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В управление финансами", callback_data="admin_finance")]
    ])
    await message.answer(
        f"✅ Баланс игрока <b>{html.escape(str(target_nick))}</b> изменён на <b>{sign}{delta} 🪙</b>.\n"
        f"💵 Новый баланс: <b>{new_bal} 🪙</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _render_eco_settings_menu(event: Message | CallbackQuery, db):
    cfg = await db.get_economy_config()
    lines = ["⚙️ <b>Настройки экономики сервера</b>\nНажмите на любой параметр, чтобы изменить его:\n"]
    rows = []

    for key, (label, hint) in ECO_LABELS.items():
        val = cfg.get(key, DEFAULT_ECONOMY_CONFIG[key])
        lines.append(f"• {label}: <b>{val}</b> — <i>{hint}</i>")
        rows.append([
            InlineKeyboardButton(text=f"{label}: {val}", callback_data=f"aeco:edit:{key}")
        ])

    rows.append([InlineKeyboardButton(text="🔄 Сбросить по умолчанию", callback_data="aeco:reset")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в финансы", callback_data="admin_finance")])

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
@router.callback_query(F.data == "aeco:menu")
async def aeco_menu(callback: CallbackQuery, db, settings, state: FSMContext):
    if state:
        await state.set_state(None)

    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    await _render_eco_settings_menu(callback, db)


@router.callback_query(F.data.startswith("aeco:edit:"))
async def aeco_edit_start(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, key = callback.data.split(":", 2)
    if key not in ECO_LABELS:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return

    cfg = await db.get_economy_config()
    label, hint = ECO_LABELS[key]
    current_val = cfg.get(key, DEFAULT_ECONOMY_CONFIG[key])

    await state.update_data(eco_edit_key=key)
    await state.set_state(AdminEcoSettings.wait_value)
    await callback.message.answer(
        f"⚙️ <b>{label}</b>\n"
        f"Текущее значение: <b>{current_val}</b>\n"
        f"Правило: <i>{hint}</i>\n\n"
        "Введите новое целое число или <code>/cancel</code> для отмены:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminEcoSettings.wait_value)
async def aeco_edit_save(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Изменение параметра отменено.")
        return

    data = await state.get_data()
    key = data.get("eco_edit_key")
    if not key or key not in ECO_LABELS:
        await state.clear()
        return

    try:
        val = int(raw)
    except ValueError:
        await message.answer(
            "❌ Введите целое число (например: <code>150</code> или <code>-300</code>):",
            parse_mode="HTML",
        )
        return

    if key == "eco_debt_floor":
        if val > 0:
            await message.answer(
                "❌ Лимит долга должен быть отрицательным числом или 0 (например: <code>-300</code>):",
                parse_mode="HTML",
            )
            return
    else:
        if val < 0:
            await message.answer("❌ Значение этого параметра не может быть меньше 0. Введите число ≥ 0:")
            return

    await db.update_economy_setting(key, val)
    await state.clear()

    label, _ = ECO_LABELS[key]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ К настройкам экономики", callback_data="aeco:menu")]
    ])
    await message.answer(
        f"✅ Параметр <b>{label}</b> обновлён: <b>{val}</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(F.data == "aeco:reset")
async def aeco_reset_defaults(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    for k, default_val in DEFAULT_ECONOMY_CONFIG.items():
        await db.update_economy_setting(k, default_val)

    await callback.answer("🔄 Настройки экономики сброшены к значениям по умолчанию!", show_alert=True)
    await _render_eco_settings_menu(callback, db)
