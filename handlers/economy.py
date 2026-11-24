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
def _is_admin(user: dict | None, settings) -> bool:
    if not user:
        return False
    if int(user.get("telegram_id", 0)) in settings.admin_ids:
        return True
    return str(user.get("role", "")).lower() == "admin"


async def _get_eco_cfg(db) -> dict[str, int]:
    """Возвращает актуальные настройки экономики из БД с дефолтными значениями."""
    cfg = dict(DEFAULT_ECONOMY_CONFIG)
    if hasattr(db, "get_economy_config"):
        db_cfg = await db.get_economy_config()
        cfg.update(db_cfg)
    return cfg


async def _get_cart_items(db, user_id: int) -> list[dict]:
    return await db.fetchall(
        """
        SELECT c.id AS cart_id, c.qty, s.id AS item_id, s.display_name, s.game_classname, s.price, s.category
        FROM cart c
        JOIN shop_items s ON s.id = c.item_id
        WHERE c.user_id = ?
        ORDER BY c.id ASC
        """,
        (user_id,),
    )


async def _render_cart_view(event: Message | CallbackQuery, db, user_id: int, edit: bool = False):
    items = await _get_cart_items(db, user_id)
    if not items:
        text = "🛒 Ваша корзина пуста."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🛍 В магазин", callback_data="shop_cat:weapon")]]
        )
        if edit and isinstance(event, CallbackQuery) and getattr(event, "id", None) != "0":
            await event.message.edit_text(text, reply_markup=kb)
            await event.answer()
        else:
            target = event.message if isinstance(event, CallbackQuery) else event
            await target.answer(text, reply_markup=kb)
        return

    lines = ["🛒 <b>Ваша корзина:</b>\n"]
    total = 0
    kb_rows = []
    for it in items:
        subtotal = int(it["qty"]) * int(it["price"])
        total += subtotal
        lines.append(
            f"• {html.escape(it['display_name'])} — {it['qty']} шт. × {it['price']} 🪙 = <b>{subtotal} 🪙</b>"
        )
        kb_rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"cart_dec:{it['cart_id']}"),
            InlineKeyboardButton(text=f"{it['display_name'][:16]} ({it['qty']})", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"cart_inc:{it['cart_id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"cart_del:{it['cart_id']}"),
        ])

    lines.append(f"\n💰 <b>Итого: {total} 🪙</b>")
    kb_rows.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout_start")])
    kb_rows.append([
        InlineKeyboardButton(text="🧹 Очистить", callback_data="cart_clear"),
        InlineKeyboardButton(text="🛍 В магазин", callback_data="shop_cat:weapon"),
    ])

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    if edit and isinstance(event, CallbackQuery) and getattr(event, "id", None) != "0":
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else:
        target = event.message if isinstance(event, CallbackQuery) else event
        await target.answer(text, reply_markup=kb, parse_mode="HTML")


async def _finalize_checkout(
    event: Message | CallbackQuery,
    db,
    nitrado,
    settings,
    state: FSMContext,
    user: dict,
    x: float,
    z: float,
):
    items = await _get_cart_items(db, user["id"])
    if not items:
        await state.clear()
        await event.answer("🛒 Корзина пуста.")
        return

    total = sum(int(it["qty"]) * int(it["price"]) for it in items)
    current_balance = int(user["balance"])
    if current_balance < total:
        await state.clear()
        await event.answer(f"❌ Недостаточно средств. Нужно {total} 🪙, на балансе {current_balance} 🪙.")
        return

    order_items = [
        {
            "item_id": it["item_id"],
            "display_name": it["display_name"],
            "game_classname": it["game_classname"],
            "qty": int(it["qty"]),
            "price": int(it["price"]),
        }
        for it in items
    ]

    for it in order_items:
        ok_ev = await nitrado.ensure_shop_event_exists(it["game_classname"])
        if not ok_ev:
            await event.answer(
                f"❌ Не удалось подготовить XML-эвент для предмета <code>{html.escape(it['game_classname'])}</code>.",
                parse_mode="HTML",
            )
            return

    ok_spawn = await nitrado.append_event_spawn_orders(order_items, x=x, z=z, radius=3.0)
    if not ok_spawn:
        await event.answer("❌ Не удалось записать заказ в файлы спавна сервера. Оплата не списана.")
        return

    await db.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ?",
        (total, user["id"]),
    )
    await db.execute("DELETE FROM cart WHERE user_id = ?", (user["id"],))
    await db.execute(
        """
        INSERT INTO orders (user_id, items_json, total_price, coord_x, coord_z, status)
        VALUES (?, ?, ?, ?, ?, 'pending_restart')
        """,
        (user["id"], json.dumps(order_items, ensure_ascii=False), total, x, z),
    )
    await state.clear()

    new_bal = current_balance - total
    await event.answer(
        f"✅ <b>Заказ оформлен!</b>\n"
        f"📍 Координаты: <b>X={x}, Z={z}</b> (радиус 3м)\n"
        f"💸 Списано: <b>{total} 🪙</b> | Остаток: <b>{new_bal} 🪙</b>\n"
        f"🔄 Лут появится на сервере после ближайшего рестарта.",
        parse_mode="HTML",
    )
async def _give_daily_bonus(db, user: dict) -> tuple[bool, str]:
    """Проверяет и начисляет ежедневный бонус игроку. Возвращает (успех, текст)."""
    cfg = await _get_eco_cfg(db)
    bonus = int(cfg.get("eco_daily_bonus", 30))
    if bonus <= 0:
        return False, "⏳ Ежедневный бонус временно отключён администратором."

    if hasattr(db, "claim_daily_bonus"):
        return await db.claim_daily_bonus(user["id"], bonus)

    row = await db.fetchone("SELECT last_daily_at FROM users WHERE id = ?", (user["id"],))
    last_raw = row["last_daily_at"] if row else None
    now = datetime.now(timezone.utc)
    if last_raw:
        try:
            last_dt = datetime.fromisoformat(str(last_raw))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now - last_dt).total_seconds() < 86400:
                rem = 86400 - int((now - last_dt).total_seconds())
                hrs, mins = divmod(rem // 60, 60)
                return False, f"⏳ Вы уже забирали бонус. Следующий через {hrs} ч. {mins} мин."
        except ValueError:
            pass

    await db.execute(
        "UPDATE users SET balance = balance + ?, last_daily_at = ? WHERE id = ?",
        (bonus, now.isoformat(), user["id"]),
    )
    return True, f"🎁 Вы получили ежедневный бонус: <b>+{bonus} 🪙</b>!"


@router.message(F.text == "💰 Кошелек")
@router.callback_query(F.data == "wallet")
async def wallet_menu(event: Message | CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(event.from_user.id)
    if not user:
        return

    cfg = await _get_eco_cfg(db)
    daily_amt = int(cfg.get("eco_daily_bonus", 30))
    streak = int(user.get("kill_streak", 0))
    balance = int(user.get("balance", 0))

    text = (
        f"💰 <b>Ваш кошелёк</b>\n\n"
        f"👤 Игрок: <b>{html.escape(user['game_nick'])}</b>\n"
        f"💵 Баланс: <b>{balance} 🪙</b>\n"
        f"🔥 Серия убийств: <b>{streak}</b>\n\n"
        f"⏱ Доход за онлайн: <b>+{cfg.get('eco_online_reward', 60)} 🪙/час</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎁 Ежедневный бонус (+{daily_amt} 🪙)", callback_data="daily_bonus")],
        [InlineKeyboardButton(text="💸 Перевести игроку", callback_data="transfer_start")],
        [InlineKeyboardButton(text="📜 История операций", callback_data="wallet_history")],
    ])

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "daily_bonus")
async def cb_daily_bonus(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Профиль не найден.", show_alert=True)
        return

    ok, msg = await _give_daily_bonus(db, user)
    await callback.answer(re.sub(r"<[^>]+>", "", msg), show_alert=True)
    if ok:
        await wallet_menu(callback, db, state)


@router.callback_query(F.data == "wallet_history")
async def cb_wallet_history(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    rows = await db.fetchall(
        """
        SELECT amount, kind, created_at
        FROM transactions
        WHERE sender_id = ? OR receiver_id = ?
        ORDER BY id DESC LIMIT 10
        """,
        (user["id"], user["id"]),
    )
    if not rows:
        await callback.answer("История операций пока пуста.", show_alert=True)
        return

    lines = ["📜 <b>Последние 10 операций:</b>\n"]
    for r in rows:
        amt = int(r["amount"])
        sign = "+" if amt > 0 else ""
        lines.append(f"• <b>{sign}{amt} 🪙</b> — <i>{html.escape(str(r['kind']))}</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в кошелёк", callback_data="wallet")]
    ])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer()
@router.message(F.text == "🛍 Магазин")
@router.callback_query(F.data == "shop")
async def open_shop(event: Message | CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(event.from_user.id)
    if not user:
        return

    rows = []
    cat_keys = list(CAT_NAMES.keys())
    for i in range(0, len(cat_keys), 2):
        row = []
        for k in cat_keys[i : i + 2]:
            row.append(InlineKeyboardButton(text=CAT_NAMES[k], callback_data=f"shop_cat:{k}"))
        rows.append(row)

    rows.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="cart")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = f"🛍 <b>Магазин сервера</b>\n💰 Ваш баланс: <b>{user['balance']} 🪙</b>\nВыберите категорию:"

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data.startswith("shop_cat:"))
async def cb_shop_category(callback: CallbackQuery, db):
    cat = callback.data.split(":", 1)[1]
    cat_title = CAT_NAMES.get(cat, cat)

    subcats = await db.fetchall(
        "SELECT DISTINCT subcategory FROM shop_items WHERE category = ? AND is_active = 1 ORDER BY subcategory ASC",
        (cat,),
    )
    sub_names = [str(r["subcategory"]).strip() for r in subcats if r["subcategory"] and str(r["subcategory"]).strip()]

    if sub_names:
        rows = [
            [InlineKeyboardButton(text=f"📁 {html.escape(s)}", callback_data=f"shop_sub:{cat}:{s}")]
            for s in sub_names
        ]
        rows.append([InlineKeyboardButton(text="📦 Все товары категории", callback_data=f"shop_items:{cat}:__all__")])
        rows.append([
            InlineKeyboardButton(text="🛒 Корзина", callback_data="cart"),
            InlineKeyboardButton(text="⬅️ Категории", callback_data="shop"),
        ])
        await callback.message.edit_text(
            f"{cat_title}\nВыберите подкатегорию:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await callback.answer()
        return

    await _show_shop_items(callback, db, cat, "__all__")


@router.callback_query(F.data.startswith("shop_sub:"))
async def cb_shop_subcategory(callback: CallbackQuery, db):
    _, cat, sub = callback.data.split(":", 2)
    await _show_shop_items(callback, db, cat, sub)


@router.callback_query(F.data.startswith("shop_items:"))
async def cb_shop_items(callback: CallbackQuery, db):
    _, cat, sub = callback.data.split(":", 2)
    await _show_shop_items(callback, db, cat, sub)


async def _show_shop_items(callback: CallbackQuery, db, cat: str, sub: str):
    if sub == "__all__":
        items = await db.fetchall(
            "SELECT id, display_name, price, subcategory FROM shop_items WHERE category = ? AND is_active = 1 ORDER BY id ASC",
            (cat,),
        )
    else:
        items = await db.fetchall(
            "SELECT id, display_name, price, subcategory FROM shop_items WHERE category = ? AND subcategory = ? AND is_active = 1 ORDER BY id ASC",
            (cat, sub),
        )

    cat_title = CAT_NAMES.get(cat, cat)
    header = f"{cat_title}" + (f" / {html.escape(sub)}" if sub != "__all__" else "")

    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_cat:{cat}")]
        ])
        await callback.message.edit_text(f"{header}\n\nВ этом разделе пока нет товаров.", reply_markup=kb)
        await callback.answer()
        return

    rows = [
        [
            InlineKeyboardButton(
                text=f"{it['display_name']} — {it['price']} 🪙",
                callback_data=f"shop_add:{it['id']}",
            )
        ]
        for it in items
    ]
    rows.append([
        InlineKeyboardButton(text="🛒 Корзина", callback_data="cart"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_cat:{cat}"),
    ])
    await callback.message.edit_text(
        f"<b>{header}</b>\nНажмите на товар, чтобы добавить его в корзину:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("shop_add:"))
async def cb_shop_add(callback: CallbackQuery, db):
    item_id = int(callback.data.split(":", 1)[1])
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    item = await db.fetchone("SELECT id, display_name, price FROM shop_items WHERE id = ? AND is_active = 1", (item_id,))
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    existing = await db.fetchone(
        "SELECT id, qty FROM cart WHERE user_id = ? AND item_id = ?",
        (user["id"], item_id),
    )
    if existing:
        await db.execute("UPDATE cart SET qty = qty + 1 WHERE id = ?", (existing["id"],))
    else:
        await db.execute("INSERT INTO cart (user_id, item_id, qty) VALUES (?, ?, 1)", (user["id"], item_id))

    await callback.answer(f"✅ Добавлено в корзину: {item['display_name']}", show_alert=False)
@router.message(F.text == "🛒 Корзина")
@router.callback_query(F.data == "cart")
async def open_cart(event: Message | CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(event.from_user.id)
    if not user:
        return

    is_cb = isinstance(event, CallbackQuery) and getattr(event, "id", None) != "0"
    await _render_cart_view(event, db, user["id"], edit=is_cb)


@router.callback_query(F.data.startswith("cart_inc:"))
async def cb_cart_inc(callback: CallbackQuery, db):
    cart_id = int(callback.data.split(":", 1))
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    await db.execute("UPDATE cart SET qty = qty + 1 WHERE id = ? AND user_id = ?", (cart_id, user["id"]))
    await _render_cart_view(callback, db, user["id"], edit=True)


@router.callback_query(F.data.startswith("cart_dec:"))
async def cb_cart_dec(callback: CallbackQuery, db):
    cart_id = int(callback.data.split(":", 1))
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    row = await db.fetchone("SELECT qty FROM cart WHERE id = ? AND user_id = ?", (cart_id, user["id"]))
    if row:
        if int(row["qty"]) <= 1:
            await db.execute("DELETE FROM cart WHERE id = ? AND user_id = ?", (cart_id, user["id"]))
        else:
            await db.execute("UPDATE cart SET qty = qty - 1 WHERE id = ? AND user_id = ?", (cart_id, user["id"]))
    await _render_cart_view(callback, db, user["id"], edit=True)


@router.callback_query(F.data.startswith("cart_del:"))
async def cb_cart_del(callback: CallbackQuery, db):
    cart_id = int(callback.data.split(":", 1))
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    await db.execute("DELETE FROM cart WHERE id = ? AND user_id = ?", (cart_id, user["id"]))
    await _render_cart_view(callback, db, user["id"], edit=True)


@router.callback_query(F.data == "cart_clear")
async def cb_cart_clear(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    await db.execute("DELETE FROM cart WHERE user_id = ?", (user["id"],))
    await _render_cart_view(callback, db, user["id"], edit=True)


@router.callback_query(F.data == "checkout_start")
async def cb_checkout_start(callback: CallbackQuery, db, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user:
        return

    items = await _get_cart_items(db, user["id"])
    if not items:
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    total = sum(int(it["qty"]) * int(it["price"]) for it in items)
    if int(user["balance"]) < total:
        await callback.answer(
            f"Недостаточно монет! Нужно {total} 🪙, у вас {user['balance']} 🪙.",
            show_alert=True,
        )
        return

    await state.set_state(CheckoutOrder.wait_coordinates)
    await callback.message.answer(
        "📍 <b>Укажите координаты доставки с карты iZurvive</b>\n\n"
        "Введите координаты <b>X</b> и <b>Z</b> через пробел (например: <code>4512 10234</code> или <code>4512.5 10234.8</code>).\n"
        "Для отмены введите <code>/cancel</code>.",
        parse_mode="HTML",
    )
    await callback.answer()
@router.message(CheckoutOrder.wait_coordinates)
async def msg_checkout_coordinates(message: Message, db, nitrado, settings, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Оформление заказа отменено.")
        return

    parts = raw.replace(",", " ").replace(";", " ").split()
    if len(parts) != 2:
        await message.answer(
            "❌ Введите ровно два числа: <b>X</b> и <b>Z</b> через пробел (например: <code>6540 8120</code>):",
            parse_mode="HTML",
        )
        return

    try:
        x = round(float(parts), 2)
        z = round(float(parts), 2)
    except ValueError:
        await message.answer("❌ Координаты должны быть числами. Попробуйте снова или введите /cancel.")
        return

    if not (0 <= x <= 15360 and 0 <= z <= 15360):
        await message.answer("❌ Координаты должны быть в пределах карты (от 0 до 15360). Попробуйте снова.")
        return

    user = await db.get_user(message.from_user.id)
    if not user:
        await state.clear()
        return

    await _finalize_checkout(message, db, nitrado, settings, state, user, x, z)


# ==========================================
# ПЕРЕВОД МОНЕТ ДРУГОМУ ИГРОКУ
# ==========================================

@router.callback_query(F.data == "transfer_start")
async def cb_transfer_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Transfer.nick)
    await callback.message.answer(
        "💸 <b>Перевод монет</b>\nВведите игровой ник получателя (или <code>/cancel</code> для отмены):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Transfer.nick)
async def msg_transfer_nick(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Перевод отменён.")
        return

    target = await db.fetchone("SELECT id, telegram_id, game_nick FROM users WHERE LOWER(game_nick) = LOWER(?)", (raw,))
    if not target:
        await message.answer("❌ Игрок с таким ником не найден. Проверьте написание или введите /cancel:")
        return

    if int(target["telegram_id"]) == message.from_user.id:
        await message.answer("❌ Нельзя переводить монеты самому себе. Введите ник другого игрока:")
        return

    await state.update_data(target_id=target["id"], target_nick=target["game_nick"])
    await state.set_state(Transfer.amount)
    await message.answer(
        f"👤 Получатель: <b>{html.escape(target['game_nick'])}</b>\n"
        "Введите сумму перевода целым числом:",
        parse_mode="HTML",
    )


@router.message(Transfer.amount)
async def msg_transfer_amount(message: Message, db, state: FSMContext):
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

    user = await db.get_user(message.from_user.id)
    if not user or int(user["balance"]) < amount:
        bal = int(user["balance"]) if user else 0
        await message.answer(f"❌ Недостаточно средств. Ваш баланс: <b>{bal} 🪙</b>.", parse_mode="HTML")
        return

    data = await state.get_data()
    await state.update_data(amount=amount)
    await state.set_state(Transfer.confirm)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="transfer_yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_no"),
        ]
    ])
    await message.answer(
        f"Подтвердите перевод:\n"
        f"👤 Кому: <b>{html.escape(data['target_nick'])}</b>\n"
        f"💰 Сумма: <b>{amount} 🪙</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )
@router.callback_query(Transfer.confirm, F.data == "transfer_no")
async def cb_transfer_no(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Перевод отменён.")
    await callback.answer()


@router.callback_query(Transfer.confirm, F.data == "transfer_yes")
async def cb_transfer_yes(callback: CallbackQuery, db, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    sender = await db.get_user(callback.from_user.id)
    target_id = data.get("target_id")
    target_nick = data.get("target_nick", "Игрок")
    amount = int(data.get("amount", 0))

    if not sender or not target_id or amount <= 0 or int(sender["balance"]) < amount:
        await callback.message.edit_text("❌ Ошибка выполнения перевода (недостаточно средств).")
        await callback.answer()
        return

    await db.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, sender["id"]))
    await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, target_id))
    await db.execute(
        "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, ?, ?, ?)",
        (sender["id"], target_id, amount, f"Перевод игроку {target_nick}"),
    )

    await callback.message.edit_text(
        f"✅ Вы успешно перевели <b>{amount} 🪙</b> игроку <b>{html.escape(target_nick)}</b>!",
        parse_mode="HTML",
    )
    await callback.answer()


# ==========================================
# АДМИН-ПАНЕЛЬ УПРАВЛЕНИЯ МАГАЗИНОМ
# ==========================================

@router.message(F.text == "🛠 Управление магазином")
@router.callback_query(F.data == "admin_shop")
async def admin_shop_menu(event: Message | CallbackQuery, db, settings, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    user = await db.get_user(event.from_user.id)
    if not _is_admin(user, settings):
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="ashop_add")],
        [InlineKeyboardButton(text="📋 Список и редактирование товаров", callback_data="ashop_list:weapon")],
    ])
    text = "🛠 <b>Управление магазином</b>\nВыберите действие:"

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data == "ashop_add")
async def cb_ashop_add(callback: CallbackQuery, db, settings, state: FSMContext):
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
async def cb_ashop_add_cat(callback: CallbackQuery, db, state: FSMContext):
    cat = callback.data.split(":", 1)
    await state.update_data(category=cat)

    subcats = await db.fetchall(
        "SELECT DISTINCT subcategory FROM shop_items WHERE category = ? ORDER BY subcategory ASC",
        (cat,),
    )
    existing = [str(r["subcategory"]).strip() for r in subcats if r["subcategory"] and str(r["subcategory"]).strip()]

    rows = [
        [InlineKeyboardButton(text=f"📁 {s}", callback_data=f"ashop_sub:{s}")]
        for s in existing
    ]
    rows.append([InlineKeyboardButton(text="➕ Новая подкатегория", callback_data="ashop_sub_new")])
    rows.append([InlineKeyboardButton(text="⏭ Без подкатегории", callback_data="ashop_sub:__none__")])

    await state.set_state(AdminShop.add_subcategory)
    await callback.message.edit_text(
        f"Категория: <b>{CAT_NAMES.get(cat, cat)}</b>\nВыберите или создайте подкатегорию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(AdminShop.add_subcategory, F.data == "ashop_sub_new")
async def cb_ashop_sub_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminShop.new_subcat_name)
    await callback.message.edit_text("Введите название новой подкатегории (например: <code>Штурмовые винтовки</code>):", parse_mode="HTML")
    await callback.answer()


@router.message(AdminShop.new_subcat_name)
async def msg_ashop_sub_new(message: Message, state: FSMContext):
    sub_name = (message.text or "").strip()
    if not sub_name or sub_name.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление товара отменено.")
        return

    await state.update_data(subcategory=sub_name)
    await state.set_state(AdminShop.add_display_name)
    await message.answer(
        f"Подкатегория: <b>{html.escape(sub_name)}</b>\n"
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        parse_mode="HTML",
    )


@router.callback_query(AdminShop.add_subcategory, F.data.startswith("ashop_sub:"))
async def cb_ashop_add_sub(callback: CallbackQuery, state: FSMContext):
    sub = callback.data.split(":", 1)
    sub_val = None if sub == "__none__" else sub
    await state.update_data(subcategory=sub_val)
    await state.set_state(AdminShop.add_display_name)
    await callback.message.edit_text(
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminShop.add_display_name)
async def msg_ashop_display_name(message: Message, state: FSMContext):
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
async def msg_ashop_classname(message: Message, state: FSMContext):
    cname = (message.text or "").strip()
    if cname.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление товара отменено.")
        return

    if not CLASSNAME_RE.match(cname):
        await message.answer("❌ Некорректный Classname. Разрешены только латинские буквы, цифры и <code>_</code>:", parse_mode="HTML")
        return

    await state.update_data(game_classname=cname)
    await state.set_state(AdminShop.add_price)
    await message.answer("Введите цену товара в монетах (целое положительное число):")


@router.message(AdminShop.add_price)
async def msg_ashop_price(message: Message, db, nitrado, state: FSMContext):
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
    sub = data.get("subcategory")
    dname = data["display_name"]
    cname = data["game_classname"]

    await db.execute(
        """
        INSERT INTO shop_items (category, subcategory, display_name, game_classname, price, is_active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (cat, sub, dname, cname, price),
    )
    xml_ok = await nitrado.ensure_shop_event_exists(cname)
    xml_note = "✅ Эвент в <code>shop_events.xml</code> создан." if xml_ok else "⚠️ Не удалось обновить <code>shop_events.xml</code> по FTP."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё товар", callback_data="ashop_add")],
        [InlineKeyboardButton(text="⬅️ В управление магазином", callback_data="admin_shop")],
    ])
    await message.answer(
        f"✅ Товар <b>{html.escape(dname)}</b> (<code>{cname}</code>) за <b>{price} 🪙</b> добавлен!\n{xml_note}",
        reply_markup=kb,
        parse_mode="HTML",
    )
@router.callback_query(F.data.startswith("ashop_list:"))
async def cb_ashop_list(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    cat = callback.data.split(":", 1)
    items = await db.fetchall(
        "SELECT id, display_name, price, is_active FROM shop_items WHERE category = ? ORDER BY id ASC",
        (cat,),
    )

    cat_tabs = [
        InlineKeyboardButton(
            text=("• " if k == cat else "") + title.split(" ", 1)[-1],
            callback_data=f"ashop_list:{k}",
        )
        for k, title in CAT_NAMES.items()
    ]
    rows = [cat_tabs[:3], cat_tabs[3:]]

    for it in items:
        status_icon = "🟢" if int(it["is_active"]) == 1 else "🔴"
        rows.append([
            InlineKeyboardButton(
                text=f"{status_icon} {it['display_name']} — {it['price']} 🪙",
                callback_data=f"ashop_item:{it['id']}",
            )
        ])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_shop")])
    await callback.message.edit_text(
        f"📋 <b>Товары категории: {CAT_NAMES.get(cat, cat)}</b>\nНажмите на товар для редактирования:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_item:"))
async def cb_ashop_item(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    item_id = int(callback.data.split(":", 1))
    item = await db.fetchone("SELECT * FROM shop_items WHERE id = ?", (item_id,))
    if not item:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    status_text = "🟢 Активен" if int(item["is_active"]) == 1 else "🔴 Скрыт"
    sub_text = html.escape(str(item["subcategory"])) if item["subcategory"] else "—"

    text = (
        f"📦 <b>Товар #{item['id']}</b>\n\n"
        f"• Категория: <b>{CAT_NAMES.get(item['category'], item['category'])}</b>\n"
        f"• Подкатегория: <b>{sub_text}</b>\n"
        f"• Название: <b>{html.escape(item['display_name'])}</b>\n"
        f"• Classname: <code>{html.escape(item['game_classname'])}</code>\n"
        f"• Цена: <b>{item['price']} 🪙</b>\n"
        f"• Статус: <b>{status_text}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Название", callback_data=f"ashop_edit:{item_id}:display_name"),
            InlineKeyboardButton(text="💰 Цена", callback_data=f"ashop_edit:{item_id}:price"),
        ],
        [
            InlineKeyboardButton(text="🧬 Classname", callback_data=f"ashop_edit:{item_id}:game_classname"),
            InlineKeyboardButton(text="📁 Подкатегория", callback_data=f"ashop_edit:{item_id}:subcategory"),
        ],
        [
            InlineKeyboardButton(text="👁 Вкл/Выкл показ", callback_data=f"ashop_toggle:{item_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ashop_del:{item_id}"),
        ],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data=f"ashop_list:{item['category']}")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_toggle:"))
async def cb_ashop_toggle(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    item_id = int(callback.data.split(":", 1))
    item = await db.fetchone("SELECT is_active FROM shop_items WHERE id = ?", (item_id,))
    if not item:
        return

    new_val = 0 if int(item["is_active"]) == 1 else 1
    await db.execute("UPDATE shop_items SET is_active = ? WHERE id = ?", (new_val, item_id))
    callback.data = f"ashop_item:{item_id}"
    await cb_ashop_item(callback, db, settings)


@router.callback_query(F.data.startswith("ashop_del:"))
async def cb_ashop_del(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    item_id = int(callback.data.split(":", 1))
    item = await db.fetchone("SELECT category FROM shop_items WHERE id = ?", (item_id,))
    cat = item["category"] if item else "weapon"

    await db.execute("DELETE FROM cart WHERE item_id = ?", (item_id,))
    await db.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))
    await callback.answer("🗑 Товар удалён.", show_alert=True)
    callback.data = f"ashop_list:{cat}"
    await cb_ashop_list(callback, db, settings)
@router.callback_query(F.data.startswith("ashop_edit:"))
async def cb_ashop_edit(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str, field = callback.data.split(":", 2)
    prompts = {
        "display_name": "Введите новое отображаемое название товара:",
        "price": "Введите новую цену товара в монетах (целое число > 0):",
        "game_classname": "Введите новый игровой Classname предмета (латиницей):",
        "subcategory": "Введите название подкатегории (или <code>-</code> чтобы убрать подкатегорию):",
    }
    if field not in prompts:
        return

    await state.update_data(edit_item_id=int(item_id_str), edit_field=field)
    await state.set_state(AdminShop.edit_value)
    await callback.message.answer(f"{prompts[field]}\nДля отмены введите <code>/cancel</code>.", parse_mode="HTML")
    await callback.answer()


@router.message(AdminShop.edit_value)
async def msg_ashop_edit_value(message: Message, db, nitrado, state: FSMContext):
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
            await message.answer("❌ Некорректный Classname. Разрешены только латинские буквы, цифры и <code>_</code>:", parse_mode="HTML")
            return
        val = raw
    elif field == "subcategory":
        val = None if raw == "-" else raw
    else:
        val = raw

    await db.execute(f"UPDATE shop_items SET {field} = ? WHERE id = ?", (val, item_id))
    if field == "game_classname":
        await nitrado.ensure_shop_event_exists(val)

    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к товару", callback_data=f"ashop_item:{item_id}")]
    ])
    await message.answer("✅ Параметр товара успешно обновлён!", reply_markup=kb)


# ==========================================
# АДМИН-МЕНЮ ФИНАНСОВ И НАСТРОЕК ЭКОНОМИКИ
# ==========================================

@router.message(F.text == "💸 Финансы (Админ)")
@router.callback_query(F.data == "admin_finance")
async def admin_finance_menu(event: Message | CallbackQuery, db, settings, state: FSMContext | None = None):
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
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
@router.callback_query(F.data.startswith("afin_user:"))
async def cb_afin_user(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    target_id = int(callback.data.split(":", 1)[1])
    target = await db.fetchone("SELECT id, game_nick, balance FROM users WHERE id = ?", (target_id,))
    if not target:
        await callback.answer("Игрок не найден.", show_alert=True)
        return

    await state.update_data(afin_target_id=target_id, afin_target_nick=target["game_nick"])
    await state.set_state(AdminFinance.wait_for_balance)
    await callback.message.answer(
        f"👤 Игрок: <b>{html.escape(target['game_nick'])}</b>\n"
        f"💰 Текущий баланс: <b>{target['balance']} 🪙</b>\n\n"
        "Введите сумму для начисления (например: <code>500</code>) или списания (например: <code>-200</code>).\n"
        "Для отмены введите <code>/cancel</code>.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminFinance.wait_for_balance)
async def msg_afin_balance(message: Message, db, state: FSMContext):
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
        await message.answer("❌ Введите ненулевое целое число (например: <code>500</code> или <code>-200</code>):", parse_mode="HTML")
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
        f"✅ Баланс игрока <b>{html.escape(target_nick)}</b> изменён на <b>{sign}{delta} 🪙</b>.\n"
        f"💵 Новый баланс: <b>{new_bal} 🪙</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def _render_eco_settings_menu(event: Message | CallbackQuery, db):
    cfg = await _get_eco_cfg(db)
    lines = ["⚙️ <b>Настройки экономики сервера</b>\nНажмите на любой параметр, чтобы изменить его:\n"]
    rows = []

    for key, (label, hint) in ECO_LABELS.items():
        val = cfg.get(key, DEFAULT_ECONOMY_CONFIG[key])
        lines.append(f"• {label}: <b>{val}</b> — <i>{hint}</i>")
        rows.append([
            InlineKeyboardButton(text=f"{label}: {val}", callback_data=f"aeco_edit:{key}")
        ])

    rows.append([InlineKeyboardButton(text="🔄 Сбросить по умолчанию", callback_data="aeco_reset")])
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
import json
from datetime import datetime, timezone
