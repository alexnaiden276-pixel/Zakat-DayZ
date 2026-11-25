import contextlib
import html
import math
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

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
    "eco_online_reward": ("⏱ Доход за онлайн", "монет за интервал начисления"),
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
    add_photo = State()
    edit_value = State()
    edit_photo = State()
    new_subcat_name = State()


class AdminFinance(StatesGroup):
    wait_for_nick = State()
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

async def _safe_render_menu(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
    photo_id: str | None = None,
) -> None:
    """Безопасно переключает сообщение между текстом и фото без ошибок Telegram API."""
    msg = callback.message
    had_photo = bool(msg.photo)

    try:
        if photo_id and had_photo:
            await msg.edit_media(
                media=InputMediaPhoto(media=photo_id, caption=text, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        if not photo_id and not had_photo:
            await msg.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
            return
    except TelegramAPIError as e:
        if "message is not modified" in str(e).lower():
            return

    with contextlib.suppress(Exception):
        await msg.delete()

    if photo_id:
        await msg.answer_photo(photo=photo_id, caption=text, reply_markup=markup, parse_mode="HTML")
    else:
        await msg.answer(text=text, reply_markup=markup, parse_mode="HTML")


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
        sub_title = html.escape(str(subcat["name"])) if subcat else "Группа"
        items = await db.get_shop_items(key, subcategory_id=sub_id)
        header = f"<b>{CAT_NAMES[key]} → 📁 {sub_title}</b>"

    has_content = bool(rows or items)

    for item in items:
        in_cart = cart.get(str(item["id"]), 0)
        badge = f" [в корзине: {in_cart}]" if in_cart else ""
        pic_icon = "🖼 " if item["photo_file_id"] else ""
        sub_suffix = str(sub_id) if sub_id is not None else "0"
        rows.append([
            InlineKeyboardButton(
                text=f"{pic_icon}{item['display_name']} — {item['price']} 🪙{badge}",
                callback_data=f"item_view:{item['id']}:{key}:{sub_suffix}",
            )
        ])

    total_in_cart = sum(cart.values())
    rows.append([InlineKeyboardButton(text=f"🛒 Перейти в корзину ({total_in_cart})", callback_data="cart:view")])

    if sub_id is not None:
        rows.append([InlineKeyboardButton(text=f"⬅️ Назад в «{CAT_NAMES[key]}»", callback_data=f"cat:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Все категории", callback_data="terminal")])

    if has_content:
        text = f"{header}\nВыберите группу или товар для просмотра и покупки:"
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
        await _safe_render_menu(callback, text, markup)
        await callback.answer()


@router.callback_query(F.data.startswith("cat:"))
async def open_category(callback: CallbackQuery, db, state: FSMContext):
    _, key = callback.data.split(":", 1)
    if key not in CAT_NAMES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=None)
    await _safe_render_menu(callback, text, markup)
    await callback.answer()
@router.callback_query(F.data.startswith("subcat:"))
async def open_subcategory(callback: CallbackQuery, db, state: FSMContext):
    _, key, sub_id_str = callback.data.split(":", 2)
    if key not in CAT_NAMES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=int(sub_id_str))
    await _safe_render_menu(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("item_view:"))
async def view_shop_item(callback: CallbackQuery, db, state: FSMContext):
    _, item_id_str, key, sub_suffix = callback.data.split(":", 3)
    item_id = int(item_id_str)
    item = await db.get_shop_item(item_id)
    if not item:
        await callback.answer("Товар не найден", show_alert=True)
        return

    data = await state.get_data()
    cart = data.get("cart", {})
    qty = int(cart.get(str(item_id), 0))

    text = (
        f"📦 <b>{html.escape(str(item['display_name']))}</b>\n\n"
        f"💰 Цена за 1 шт.: <b>{item['price']} 🪙</b>\n"
        f"🛒 В вашей корзине: <b>{qty} шт.</b>"
    )

    back_cb = f"subcat:{key}:{sub_suffix}" if sub_suffix != "0" else f"cat:{key}"
    rows = [
        [
            InlineKeyboardButton(text="➖ 1", callback_data=f"cart:dec:{item_id}:{key}:{sub_suffix}"),
            InlineKeyboardButton(text=f"{qty} шт.", callback_data="cart:noop"),
            InlineKeyboardButton(text="➕ 1", callback_data=f"cart:inc:{item_id}:{key}:{sub_suffix}"),
        ],
        [InlineKeyboardButton(text="🛒 Перейти в корзину", callback_data="cart:view")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=back_cb)],
    ]
    await _safe_render_menu(
        callback,
        text,
        InlineKeyboardMarkup(inline_keyboard=rows),
        photo_id=item["photo_file_id"],
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cart:"))
async def cart_actions(callback: CallbackQuery, db, state: FSMContext):
    parts = callback.data.split(":")
    action = parts[1]

    if action == "noop":
        await callback.answer()
        return

    data = await state.get_data()
    cart: dict[str, int] = dict(data.get("cart", {}))

    if action in ("inc", "dec"):
        item_id_str, key, sub_suffix = parts[2], parts[3], parts[4]
        cur_qty = int(cart.get(item_id_str, 0))
        if action == "inc":
            cart[item_id_str] = cur_qty + 1
        elif cur_qty > 1:
            cart[item_id_str] = cur_qty - 1
        else:
            cart.pop(item_id_str, None)

        await state.update_data(cart=cart)
        fake_cb = callback.model_copy(update={"data": f"item_view:{item_id_str}:{key}:{sub_suffix}"})
        await view_shop_item(fake_cb, db, state)
        return

    if action == "clear":
        await state.update_data(cart={})
        await callback.answer("🗑 Корзина очищена")
        cart = {}

    if action in ("view", "clear"):
        if not cart:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🖥 В терминал", callback_data="terminal")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
            ])
            await _safe_render_menu(callback, "🛒 <b>Ваша корзина пуста.</b>", kb)
            if action == "view":
                await callback.answer()
            return

        lines = ["🛒 <b>Ваша корзина:</b>\n"]
        total_sum = 0
        valid_cart: dict[str, int] = {}

        for str_id, qty in cart.items():
            item = await db.get_shop_item(int(str_id))
            if not item:
                continue
            q = int(qty)
            subtotal = int(item["price"]) * q
            total_sum += subtotal
            valid_cart[str_id] = q
            lines.append(f"• {html.escape(str(item['display_name']))} × {q} = <b>{subtotal} 🪙</b>")

        if valid_cart != cart:
            await state.update_data(cart=valid_cart)

        lines.append(f"\n💰 <b>Итого к оплате: {total_sum} 🪙</b>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout:start")],
            [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="cart:clear")],
            [InlineKeyboardButton(text="⬅️ Продолжить покупки", callback_data="terminal")],
        ])
        await _safe_render_menu(callback, "\n".join(lines), kb)
        if action == "view":
            await callback.answer()
@router.callback_query(F.data == "checkout:start")
async def checkout_start(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    cart: dict[str, int] = data.get("cart", {})
    if not cart:
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return

    total_sum = 0
    for str_id, qty in cart.items():
        item = await db.get_shop_item(int(str_id))
        if item:
            total_sum += int(item["price"]) * int(qty)

    if int(user["balance"] or 0) < total_sum:
        await callback.answer(
            f"Недостаточно средств! Нужно {total_sum} 🪙, на балансе {user['balance']} 🪙.",
            show_alert=True,
        )
        return

    await state.set_state(CheckoutOrder.wait_coordinates)
    await callback.message.answer(
        f"📍 <b>Оформление заказа ({total_sum} 🪙)</b>\n\n"
        "Введите координаты точки спавна <b>X</b> и <b>Z</b> с карты iZurvive.\n"
        "Например: <code>6422 / 6186</code> или <code>4512.5 10234.8</code>\n\n"
        "Для отмены отправьте <code>/cancel</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CheckoutOrder.wait_coordinates)
async def process_checkout_coordinates(message: Message, db, nitrado_api, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Оформление заказа отменено. Товары остались в корзине.")
        return

    nums = re.findall(r"-?\d+(?:[.,]\d+)?", raw)
    if len(nums) != 2:
        await message.answer(
            "❌ Введите ровно два числа — <b>X</b> и <b>Z</b> (например: <code>6422 / 6186</code> или <code>4512.5 10234.8</code>):",
            parse_mode="HTML",
        )
        return

    try:
        x = float(nums.replace(",", "."))
        z = float(nums.replace(",", "."))
        if not (math.isfinite(x) and math.isfinite(z)):
            raise ValueError("Non-finite coordinates")
    except ValueError:
        await message.answer("❌ Координаты должны быть числами. Попробуйте снова или введите /cancel.")
        return

    if not (0 <= x <= 15500 and 0 <= z <= 15500):
        await message.answer(
            "❌ Координаты выходят за пределы карты (0 – 15500). Проверьте значения на iZurvive и введите снова:",
            parse_mode="HTML",
        )
        return

    user = await db.get_user(message.from_user.id)
    if not user:
        await state.clear()
        await message.answer("❌ Профиль не найден. Введите /start.")
        return

    data = await state.get_data()
    cart: dict[str, int] = data.get("cart", {})
    if not cart:
        await state.set_state(None)
        await message.answer("❌ Корзина пуста.")
        return

    order_items = []
    summary_parts = []
    total_sum = 0
    for str_id, qty in cart.items():
        item = await db.get_shop_item(int(str_id))
        if not item:
            continue
        q = int(qty)
        p = int(item["price"])
        total_sum += p * q
        summary_parts.append(f"{item['display_name']} x{q}")
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

    if int(user["balance"] or 0) < total_sum:
        await state.set_state(None)
        await message.answer(
            f"❌ Недостаточно средств! Нужно {total_sum} 🪙, на балансе {user['balance']} 🪙."
        )
        return

    # Снимаем состояние на время выполнения транзакции и FTP-записи, чтобы избежать дублей
    await state.set_state(None)

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute("SELECT balance FROM users WHERE id = ?", (int(user["id"]),))
            row = await cur.fetchone()
            current_bal = int(row["balance"] or 0) if row else 0
            if current_bal < total_sum:
                await conn.rollback()
                await message.answer(
                    f"❌ Недостаточно средств! Нужно {total_sum} 🪙, на балансе {current_bal} 🪙."
                )
                return

            await conn.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (total_sum, int(user["id"])))
            await conn.commit()
        except Exception:
            await conn.rollback()
            await state.set_state(CheckoutOrder.wait_coordinates)
            raise

    for it in order_items:
        ok_ev = await nitrado_api.ensure_shop_event_exists(it["game_classname"])
        if not ok_ev:
            await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (total_sum, int(user["id"])))
            await state.set_state(CheckoutOrder.wait_coordinates)
            await message.answer(
                f"❌ Не удалось подготовить XML-эвент для предмета <code>{html.escape(str(it['game_classname']))}</code>. "
                "Монеты возвращены.\nПопробуйте позже или введите <code>/cancel</code>.",
                parse_mode="HTML",
            )
            return

    ok_spawn = await nitrado_api.append_event_spawn_orders(order_items, x=x, z=z, radius=3.0)
    if not ok_spawn:
        await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (total_sum, int(user["id"])))
        await state.set_state(CheckoutOrder.wait_coordinates)
        await message.answer(
            "❌ Не удалось записать заказ в файлы спавна сервера. Монеты возвращены.\n"
            "Попробуйте снова или введите <code>/cancel</code>.",
            parse_mode="HTML",
        )
        return

    await db.execute(
        "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, NULL, ?, 'shop_purchase')",
        (int(user["id"]), total_sum),
    )
    await db.create_shop_order(
        user_id=int(user["id"]),
        telegram_id=int(user["telegram_id"]),
        items_summary=", ".join(summary_parts),
        x=x,
        z=z,
    )
    await state.update_data(cart={})

    new_bal = current_bal - total_sum
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
    streak = int(user["killstreak"] or 0)
    balance = int(user["balance"] or 0)

    text = (
        f"💰 <b>Ваш кошелёк</b>\n\n"
        f"👤 Игрок: <b>{html.escape(str(user['game_nick']))}</b>\n"
        f"💵 Баланс: <b>{balance} 🪙</b>\n"
        f"🔥 Серия убийств: <b>{streak}</b>\n"
        f"⏱ Награда за онлайн: <b>+{online_reward} 🪙</b>"
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
    ok, msg = await db.claim_daily_bonus(callback.from_user.id)
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
        await state.set_state(None)
        await message.answer("❌ Перевод отменён.")
        return

    target = await db.get_user_by_nick(raw)
    if not target:
        await message.answer("❌ Игрок с таким ником не найден. Проверьте написание или введите /cancel:")
        return

    if int(target["telegram_id"]) == message.from_user.id:
        await message.answer("❌ Нельзя переводить монеты самому себе. Введите ник другого игрока:")
        return

    await state.update_data(target_nick=str(target["game_nick"]))
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
        await state.set_state(None)
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
    if not sender or int(sender["balance"] or 0) < amount:
        bal = int(sender["balance"] or 0) if sender else 0
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
    await state.set_state(None)
    await callback.message.edit_text("❌ Перевод отменён.")
    await callback.answer()


@router.callback_query(Transfer.confirm, F.data == "transfer:yes")
async def transfer_yes(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    await state.set_state(None)

    sender = await db.get_user(callback.from_user.id)
    target_nick = str(data.get("target_nick", ""))
    amount = int(data.get("amount", 0))

    if not sender or not target_nick or amount <= 0:
        await callback.message.edit_text("❌ Ошибка выполнения перевода (данные устарели).")
        await callback.answer()
        return

    _, msg = await db.transfer(int(sender["id"]), target_nick, amount)
    await callback.message.edit_text(html.escape(msg))
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
        await _safe_render_menu(event, text, kb)
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
    _, cat = callback.data.split(":", 1)
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
        await state.set_state(None)
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
    _, sub_id_str = callback.data.split(":", 1)
    sub_id = int(sub_id_str)
    await state.update_data(subcategory_id=sub_id if sub_id > 0 else None)
    await state.set_state(AdminShop.add_display_name)
    await callback.message.edit_text(
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.add_display_name)
async def ashop_display_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Добавление товара отменено.")
        return

    await state.update_data(display_name=name)
    await state.set_state(AdminShop.add_classname)
    await message.answer(
        "Введите игровой <b>classname</b> предмета из DayZ (латиница, цифры, подчёркивание).\n"
        "Например: <code>AK74</code>",
        parse_mode="HTML",
    )
@router.message(AdminShop.add_classname)
async def ashop_classname(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Добавление товара отменено.")
        return

    if not CLASSNAME_RE.match(raw):
        await message.answer(
            "❌ Некорректный classname. Используйте только <b>A-Z, a-z, 0-9, _</b> (без пробелов).",
            parse_mode="HTML",
        )
        return

    await state.update_data(game_classname=raw)
    await state.set_state(AdminShop.add_price)
    await message.answer("Введите цену товара в монетах (целое число больше 0):")


@router.message(AdminShop.add_price)
async def ashop_price(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Добавление товара отменено.")
        return

    try:
        price = int(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Цена должна быть положительным целым числом. Попробуйте снова:")
        return

    await state.update_data(price=price)
    await state.set_state(AdminShop.add_photo)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Создать без фото", callback_data="ashop:add_skip_photo")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ashop:add_cancel")],
    ])
    await message.answer(
        "🖼 Отправьте <b>фотографию товара</b> (как фото, не файлом) или нажмите кнопку ниже:",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _finish_create_item(target: Message | CallbackQuery, db, state: FSMContext, photo_file_id: str | None) -> None:
    data = await state.get_data()
    await state.set_state(None)

    item_id = await db.add_shop_item(
        category=data["category"],
        display_name=data["display_name"],
        game_classname=data["game_classname"],
        price=int(data["price"]),
        subcategory_id=data.get("subcategory_id"),
        photo_file_id=photo_file_id,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё товар", callback_data="ashop:add")],
        [InlineKeyboardButton(text="🛠 В управление магазином", callback_data="admin_shop")],
    ])
    photo_note = "Прикреплено ✅" if photo_file_id else "Нет"
    text = (
        f"✅ Товар добавлен (ID: <code>{item_id}</code>)\n"
        f"• Название: <b>{html.escape(str(data['display_name']))}</b>\n"
        f"• Classname: <code>{html.escape(str(data['game_classname']))}</code>\n"
        f"• Цена: <b>{data['price']} 🪙</b>\n"
        f"• Фото: <b>{photo_note}</b>"
    )
    if isinstance(target, Message):
        await target.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await target.answer()


@router.callback_query(AdminShop.add_photo, F.data == "ashop:add_skip_photo")
async def ashop_add_skip_photo(callback: CallbackQuery, db, state: FSMContext):
    await _finish_create_item(callback, db, state, photo_file_id=None)


@router.callback_query(AdminShop.add_photo, F.data == "ashop:add_cancel")
async def ashop_add_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.message.edit_text("❌ Добавление товара отменено.")
    await callback.answer()


@router.message(AdminShop.add_photo)
async def ashop_add_photo_save(message: Message, db, state: FSMContext):
    if (message.text or "").strip().lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Добавление товара отменено.")
        return

    if not message.photo:
        await message.answer(
            "❌ Пожалуйста, отправьте именно <b>фотографию</b> товара (или нажмите «Создать без фото»):",
            parse_mode="HTML",
        )
        return

    photo_id = message.photo[-1].file_id
    await _finish_create_item(message, db, state, photo_file_id=photo_id)
async def _render_ashop_list(callback: CallbackQuery, db, cat: str) -> None:
    if cat not in CAT_NAMES:
        cat = "weapon"

    items = await db.get_shop_items(cat, subcategory_id=None)
    subcats = await db.get_subcategories(cat)
    sub_items = []
    for sc in subcats:
        sub_items.extend(await db.get_shop_items(cat, subcategory_id=int(sc["id"])))

    all_cat_items = items + sub_items
    rows = []
    for it in all_cat_items:
        pic_icon = "🖼 " if it["photo_file_id"] else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{pic_icon}{it['display_name']} — {it['price']} 🪙 ({it['game_classname']})",
                callback_data=f"ashop:edit:{it['id']}",
            )
        ])

    cat_tabs = [
        InlineKeyboardButton(
            text=("• " if k == cat else "") + title.split(" ", 1)[1],
            callback_data=f"ashop:list:{k}",
        )
        for k, title in CAT_NAMES.items()
    ]
    rows.append(cat_tabs[:3])
    rows.append(cat_tabs[3:])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_shop")])

    text = f"📋 <b>Товары — {CAT_NAMES[cat]}</b>\nНажмите на товар для изменения или удаления:"
    if not all_cat_items:
        text += "\n\n<i>В этой категории пока нет товаров.</i>"

    await _safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("ashop:list:"))
async def ashop_list(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    _, _, cat = callback.data.split(":", 2)
    await _render_ashop_list(callback, db, cat)
    await callback.answer()


async def _render_admin_item_detail(callback: CallbackQuery, db, item_id: int) -> bool:
    item = await db.get_shop_item(item_id)
    if not item:
        await callback.answer("Товар не найден", show_alert=True)
        return False

    sub_name = "Без группы"
    if item["subcategory_id"]:
        sub = await db.get_subcategory(int(item["subcategory_id"]))
        if sub:
            sub_name = str(sub["name"])

    photo_status = "Прикреплено ✅" if item["photo_file_id"] else "Нет ❌"
    text = (
        f"✏️ <b>Редактирование товара #{item_id}</b>\n\n"
        f"• Категория: <b>{CAT_NAMES.get(item['category'], item['category'])}</b>\n"
        f"• Группа: <b>{html.escape(sub_name)}</b>\n"
        f"• Название: <b>{html.escape(str(item['display_name']))}</b>\n"
        f"• Classname: <code>{html.escape(str(item['game_classname']))}</code>\n"
        f"• Цена: <b>{item['price']} 🪙</b>\n"
        f"• Фото: <b>{photo_status}</b>"
    )
    rows = [
        [
            InlineKeyboardButton(text="✏️ Название", callback_data=f"ashop_fld:display_name:{item_id}"),
            InlineKeyboardButton(text="🏷 Classname", callback_data=f"ashop_fld:game_classname:{item_id}"),
        ],
        [
            InlineKeyboardButton(text="💰 Цена", callback_data=f"ashop_fld:price:{item_id}"),
            InlineKeyboardButton(text="🖼 Загрузить / заменить фото", callback_data=f"ashop_photo:{item_id}"),
        ],
    ]
    if item["photo_file_id"]:
        rows.append([
            InlineKeyboardButton(text="🧹 Удалить фото товара", callback_data=f"ashop_del_photo:{item_id}")
        ])
    rows.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ashop_del:{item_id}:{item['category']}"),
        InlineKeyboardButton(text="⬅️ К списку", callback_data=f"ashop:list:{item['category']}"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await _safe_render_menu(callback, text, kb, photo_id=item["photo_file_id"])
    return True


@router.callback_query(F.data.startswith("ashop:edit:"))
async def ashop_edit_item(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, _, item_id_str = callback.data.split(":", 2)
    if await _render_admin_item_detail(callback, db, int(item_id_str)):
        await callback.answer()
@router.callback_query(F.data.startswith("ashop_fld:"))
async def ashop_field_prompt(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, field, item_id_str = callback.data.split(":", 2)
    prompts = {
        "display_name": "Введите новое отображаемое название товара:",
        "game_classname": "Введите новый игровой classname (латиница, цифры, _):",
        "price": "Введите новую цену товара (целое число > 0):",
    }
    if field not in prompts:
        return

    await state.update_data(edit_field=field, edit_item_id=int(item_id_str))
    await state.set_state(AdminShop.edit_value)
    await callback.message.answer(f"{prompts[field]}\nДля отмены введите <code>/cancel</code>.", parse_mode="HTML")
    await callback.answer()


@router.message(AdminShop.edit_value)
async def ashop_field_save(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Редактирование отменено.")
        return

    data = await state.get_data()
    field = data.get("edit_field")
    item_id = int(data.get("edit_item_id", 0))

    if field == "price":
        try:
            val: str | int = int(raw)
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Цена должна быть целым числом больше 0:")
            return
    elif field == "game_classname":
        if not CLASSNAME_RE.match(raw):
            await message.answer("❌ Некорректный classname (разрешены A-Z, a-z, 0-9, _):")
            return
        val = raw
    else:
        val = raw

    await db.update_shop_item_field(item_id, str(field), val)
    await state.set_state(None)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к товару", callback_data=f"ashop:edit:{item_id}")]
    ])
    await message.answer("✅ Параметр товара обновлён!", reply_markup=kb)


@router.callback_query(F.data.startswith("ashop_photo:"))
async def ashop_photo_prompt(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str = callback.data.split(":", 1)
    await state.update_data(edit_item_id=int(item_id_str))
    await state.set_state(AdminShop.edit_photo)
    await callback.message.answer(
        "🖼 Отправьте <b>новую фотографию</b> для этого товара (или <code>/cancel</code> для отмены):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.edit_photo)
async def ashop_photo_save(message: Message, db, state: FSMContext):
    if (message.text or "").strip().lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Загрузка фото отменена.")
        return

    if not message.photo:
        await message.answer(
            "❌ Пожалуйста, отправьте именно <b>фотографию</b> (не документом/файлом) или введите <code>/cancel</code>:",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    item_id = int(data.get("edit_item_id", 0))
    await state.set_state(None)

    photo_id = message.photo[-1].file_id
    await db.update_shop_item_field(item_id, "photo_file_id", photo_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к товару", callback_data=f"ashop:edit:{item_id}")]
    ])
    await message.answer("✅ Фотография товара обновлена!", reply_markup=kb)


@router.callback_query(F.data.startswith("ashop_del_photo:"))
async def ashop_delete_photo(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str = callback.data.split(":", 1)
    item_id = int(item_id_str)
    await db.update_shop_item_field(item_id, "photo_file_id", None)
    await callback.answer("🧹 Фотография удалена с товара", show_alert=True)
    await _render_admin_item_detail(callback, db, item_id)


@router.callback_query(F.data.startswith("ashop_del:"))
async def ashop_delete_item(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str, cat = callback.data.split(":", 2)
    await db.delete_shop_item(int(item_id_str))
    await callback.answer("🗑 Товар удалён", show_alert=True)
    await _render_ashop_list(callback, db, cat)
# ==========================================
# 4. АДМИН-МЕНЮ ФИНАНСОВ (РУЧНОЙ ВВОД НИКА И ОПАСНЫЕ ЗОНЫ)
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

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Изменить баланс игрока (ввести ник)", callback_data="afin:by_nick")],
        [InlineKeyboardButton(text="⚙️ Настройки экономики (бонусы, киллы, штрафы)", callback_data="aeco:menu")],
        [InlineKeyboardButton(text="☢️ Опасные зоны (Буст x2/x3)", callback_data="hzadm:menu")],
    ])
    text = (
        "💳 <b>Управление балансами и экономикой</b>\n"
        "Выберите действие:"
    )

    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "afin:by_nick")
async def afin_ask_nick(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    await state.set_state(AdminFinance.wait_for_nick)
    await callback.message.answer(
        "👤 <b>Изменение баланса игрока</b>\n"
        "Введите игровой ник игрока (регистр букв не важен) или <code>/cancel</code> для отмены:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminFinance.wait_for_nick)
async def afin_process_nick(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Изменение баланса отменено.")
        return

    target = await db.get_user_by_nick(raw)
    if not target:
        await message.answer(
            f"❌ Игрок с ником <b>{html.escape(raw)}</b> не найден в базе.\n"
            "Проверьте правильность ника и введите снова (или <code>/cancel</code>):",
            parse_mode="HTML",
        )
        return

    await state.update_data(
        afin_target_id=int(target["id"]),
        afin_target_nick=str(target["game_nick"]),
    )
    await state.set_state(AdminFinance.wait_for_balance)
    await message.answer(
        f"✅ Найден игрок: <b>{html.escape(str(target['game_nick']))}</b>\n"
        f"💰 Текущий баланс: <b>{target['balance']} 🪙</b>\n\n"
        "Введите сумму для начисления (например: <code>500</code>) или списания (например: <code>-200</code>).\n"
        "Для отмены введите <code>/cancel</code>:",
        parse_mode="HTML",
    )


@router.message(AdminFinance.wait_for_balance)
async def afin_apply_balance(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
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
    await state.set_state(None)

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
        [InlineKeyboardButton(text="💳 Изменить баланс ещё кому-то", callback_data="afin:by_nick")],
        [InlineKeyboardButton(text="⬅️ В управление финансами", callback_data="admin_finance")],
    ])
    await message.answer(
        f"✅ Баланс игрока <b>{html.escape(str(target_nick))}</b> изменён на <b>{sign}{delta} 🪙</b>.\n"
        f"💵 Новый баланс: <b>{new_bal} 🪙</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )
# ==========================================
# 5. НАСТРОЙКИ ЭКОНОМИКИ (БОНУСЫ, КИЛЛЫ, ШТРАФЫ)
# ==========================================

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
        with contextlib.suppress(TelegramAPIError):
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "aeco:menu")
async def aeco_menu(callback: CallbackQuery, db, settings, state: FSMContext):
    if state:
        await state.set_state(None)

    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    await _render_eco_settings_menu(callback, db)
    await callback.answer()


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
        await state.set_state(None)
        await message.answer("❌ Изменение параметра отменено.")
        return

    data = await state.get_data()
    key = data.get("eco_edit_key")
    if not key or key not in ECO_LABELS:
        await state.set_state(None)
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
    await state.set_state(None)

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
