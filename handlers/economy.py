import asyncio
import html
import logging
import math
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import DEFAULT_ECONOMY_CONFIG
from utils.ui import chunk_buttons, safe_render_menu

router = Router(name="economy")
logger = logging.getLogger(__name__)

COORDS_INPUT_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*[,;/\s]+\s*(-?\d+(?:[.,]\d+)?)\s*$")
POSITION_MAX_AGE_SECONDS = 90
MAX_CONFIRM_DRIFT_METERS = 10.0
MAX_CART_ITEM_QTY = 50

_checkout_locks: dict[int, asyncio.Lock] = {}


class CheckoutOrder(StatesGroup):
    confirm_position = State()
    enter_manual_coords = State()


class Transfer(StatesGroup):
    nick = State()
    amount = State()
    confirm = State()
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

    cat_row = await db.get_shop_category_by_slug(key)
    cat_title_raw = str(cat_row["title"]) if cat_row else key
    cat_title = html.escape(cat_title_raw)

    if sub_id is None:
        subcats = await db.get_subcategories(key)
        sub_btns = [
            InlineKeyboardButton(text=f"📁 {sc['name']}", callback_data=f"subcat:{key}:{sc['id']}")
            for sc in subcats
        ]
        rows.extend(chunk_buttons(sub_btns, cols=2))
        items = await db.get_shop_items(key, subcategory_id=None)
        header = f"<b>{cat_title}</b>"
    else:
        subcat = await db.get_subcategory(sub_id)
        sub_title = html.escape(str(subcat["name"])) if subcat else "Группа"
        items = await db.get_shop_items(key, subcategory_id=sub_id)
        header = f"<b>{cat_title} → 📁 {sub_title}</b>"

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
        rows.append([InlineKeyboardButton(text=f"⬅️ Назад в «{cat_title_raw}»", callback_data=f"cat:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Все категории", callback_data="terminal")])

    if has_content:
        text = f"{header}\nВыберите группу или товар для просмотра и покупки:"
    else:
        text = f"{header}\n\n<i>В этом разделе пока пусто.</i>"

    return text, InlineKeyboardMarkup(inline_keyboard=rows)
@router.callback_query(F.data == "terminal")
async def terminal(callback: CallbackQuery, db, state: FSMContext | None = None):
    total_count = 0
    if state:
        await state.set_state(None)
        data = await state.get_data()
        cart = data.get("cart", {})
        total_count = sum(cart.values())

    cats = await db.get_shop_categories()
    cat_buttons = [
        InlineKeyboardButton(text=str(c["title"]), callback_data=f"cat:{c['slug']}")
        for c in cats
    ]
    rows = chunk_buttons(cat_buttons, cols=2)
    rows.append([InlineKeyboardButton(text=f"🛒 Корзина ({total_count} шт.)", callback_data="cart:view")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])

    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    text = "🖥 <b>Терминал снабжения</b>\nВыберите категорию товаров:"

    if getattr(callback, "id", None) == "0":
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        await safe_render_menu(callback, text, markup)
        await callback.answer()


@router.callback_query(F.data.startswith("cat:"))
async def open_category(callback: CallbackQuery, db, state: FSMContext):
    _, key = callback.data.split(":", 1)
    cat_row = await db.get_shop_category_by_slug(key)
    if not cat_row:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=None)
    await safe_render_menu(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("subcat:"))
async def open_subcategory(callback: CallbackQuery, db, state: FSMContext):
    _, key, sub_id_str = callback.data.split(":", 2)
    cat_row = await db.get_shop_category_by_slug(key)
    if not cat_row:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=int(sub_id_str))
    await safe_render_menu(callback, text, markup)
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
    await safe_render_menu(
        callback,
        text,
        InlineKeyboardMarkup(inline_keyboard=rows),
        photo_id=item["photo_file_id"],
    )
    await callback.answer()
@router.callback_query(F.data.startswith("cart:"))
async def cart_actions(callback: CallbackQuery, db, state: FSMContext):
    parts = callback.data.split(":")
    action = parts

    if action == "noop":
        await callback.answer()
        return

    data = await state.get_data()
    cart: dict[str, int] = dict(data.get("cart", {}))

    if action in ("inc", "dec"):
        item_id_str, key, sub_suffix = parts, parts, parts
        cur_qty = int(cart.get(item_id_str, 0))
        if action == "inc":
            if cur_qty >= MAX_CART_ITEM_QTY:
                await callback.answer(f"Максимум {MAX_CART_ITEM_QTY} шт. одного товара за заказ", show_alert=True)
                return
            item_exists = await db.get_shop_item(int(item_id_str))
            if not item_exists:
                cart.pop(item_id_str, None)
                await state.update_data(cart=cart)
                await callback.answer("Этот товар больше не продаётся", show_alert=True)
                return
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
            await safe_render_menu(callback, "🛒 <b>Ваша корзина пуста.</b>", kb)
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

        if not valid_cart:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🖥 В терминал", callback_data="terminal")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
            ])
            await safe_render_menu(callback, "🛒 <b>Ваша корзина пуста.</b>", kb)
            await callback.answer()
            return

        lines.append(f"\n💰 <b>Итого к оплате: {total_sum} 🪙</b>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout:start")],
            [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="cart:clear")],
            [InlineKeyboardButton(text="⬅️ Продолжить покупки", callback_data="terminal")],
        ])
        await safe_render_menu(callback, "\n".join(lines), kb)
        if action == "view":
            await callback.answer()
# ==========================================
# 1.1 ОФОРМЛЕНИЕ ЗАКАЗА (АВТО-ПОЗИЦИЯ И РУЧНЫЕ X Z)
# ==========================================

async def _check_and_render_checkout_position(
    callback: CallbackQuery,
    db,
    nitrado_api,
    state: FSMContext,
) -> None:
    """Проверяет актуальную 3D-позицию игрока в .ADM логе и выводит экран подтверждения заказа."""
    data = await state.get_data()
    cart: dict[str, int] = data.get("cart", {})
    if not cart:
        await state.set_state(None)
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return

    total_sum = 0
    valid_items_count = 0
    for str_id, qty in cart.items():
        item = await db.get_shop_item(int(str_id))
        if item:
            total_sum += int(item["price"]) * int(qty)
            valid_items_count += 1

    if valid_items_count == 0:
        await state.update_data(cart={})
        await state.set_state(None)
        await callback.answer("Выбранные товары больше недоступны.", show_alert=True)
        return

    if int(user["balance"] or 0) < total_sum:
        await callback.answer(
            f"Недостаточно средств! Нужно {total_sum} 🪙, на балансе {user['balance']} 🪙.",
            show_alert=True,
        )
        return

    game_nick = str(user["game_nick"] or "").strip()
    player_id = str(user["player_id"] or "").strip() if "player_id" in user.keys() and user["player_id"] else None

    pos_info = await nitrado_api.get_player_live_position(
        game_nick=game_nick,
        player_id=player_id,
        max_age_seconds=POSITION_MAX_AGE_SECONDS,
    )

    if not pos_info or not pos_info.get("is_fresh"):
        reason = (pos_info or {}).get("reason") or (
            "Вы не найдены онлайн на сервере или ваши координаты ещё не записались в лог."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить мою позицию", callback_data="checkout:refresh")],
            [InlineKeyboardButton(text="🧭 Указать координаты X Z вручную", callback_data="checkout:manual")],
            [InlineKeyboardButton(text="⬅️ Вернуться в корзину", callback_data="cart:view")],
        ])
        text = (
            f"📡 <b>Определение точки доставки ({total_sum} 🪙)</b>\n\n"
            f"👤 Игрок: <b>{html.escape(game_nick)}</b>\n"
            f"⚠️ <b>Авто-позиция:</b> {html.escape(reason)}\n\n"
            "Вы можете зайти в игру, выйти на ровное место и нажать <b>«Обновить мою позицию»</b>, "
            "либо ввести координаты <b>X Z</b> с карты вручную."
        )
        await state.set_state(CheckoutOrder.confirm_position)
        await safe_render_menu(callback, text, kb)
        await callback.answer()
        return

    x = float(pos_info["x"])
    y_height = float(pos_info["y_height"])
    z = float(pos_info["z"])
    age_sec = int(pos_info.get("age_seconds", 0))
    adm_time = html.escape(str(pos_info.get("adm_time", "")))

    await state.update_data(
        checkout_pos={
            "source": "live",
            "x": x,
            "y_height": y_height,
            "z": z,
            "adm_time": adm_time,
        }
    )
    await state.set_state(CheckoutOrder.confirm_position)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и заказать сюда", callback_data="checkout:confirm")],
        [InlineKeyboardButton(text="🔄 Обновить позицию", callback_data="checkout:refresh")],
        [InlineKeyboardButton(text="🧭 Указать координаты X Z вручную", callback_data="checkout:manual")],
        [InlineKeyboardButton(text="⬅️ Вернуться в корзину", callback_data="cart:view")],
    ])
    text = (
        f"📍 <b>Подтверждение точки доставки ({total_sum} 🪙)</b>\n\n"
        f"👤 Игрок: <b>{html.escape(game_nick)}</b> (🟢 Онлайн)\n"
        f"🗺 Координаты: <code>X={round(x, 1)} / Z={round(z, 1)}</code>\n"
        f"⛰ Высота поверхности: <code>{round(y_height, 2)} м</code>\n"
        f"🕒 Актуальность: <b>{age_sec} сек. назад</b> (лог <code>{adm_time}</code>)\n\n"
        "⚠️ <i>Убедитесь, что стоите на открытом ровном месте (не внутри дома, камня или воды).</i>"
    )
    await safe_render_menu(callback, text, kb)
    await callback.answer()
@router.callback_query(F.data.in_({"checkout:start", "checkout:refresh"}))
async def checkout_start_or_refresh(callback: CallbackQuery, db, nitrado_api, state: FSMContext):
    await _check_and_render_checkout_position(callback, db, nitrado_api, state)


@router.callback_query(F.data == "checkout:manual")
async def checkout_manual_start(callback: CallbackQuery, terrain_map, state: FSMContext):
    if not terrain_map or not terrain_map.is_loaded:
        await callback.answer("Карта высот не загружена на сервере. Используйте авто-позицию.", show_alert=True)
        return

    data = await state.get_data()
    if not data.get("cart"):
        await state.set_state(None)
        await callback.answer("Корзина пуста!", show_alert=True)
        return

    await state.set_state(CheckoutOrder.enter_manual_coords)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 Определить по персонажу в игре", callback_data="checkout:refresh")],
        [InlineKeyboardButton(text="⬅️ Вернуться в корзину", callback_data="cart:view")],
    ])
    await safe_render_menu(
        callback,
        "🧭 <b>Ввод координат доставки (X и Z)</b>\n\n"
        "Отправьте сообщением координаты точки на карте Черноруси через пробел (например: <code>7520 5410</code> или <code>4512.5 10240.0</code>).\n\n"
        "⚠️ <b>Важно:</b> указывайте только открытую ровную поверхность (поляну, дорогу, двор). "
        "Не указывайте координаты внутри многоэтажных зданий, на мостах или в воде.",
        kb,
    )
    await callback.answer()


@router.message(CheckoutOrder.enter_manual_coords)
async def checkout_manual_receive(message: Message, db, terrain_map, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Оформление заказа отменено.")
        return

    match = COORDS_INPUT_RE.match(raw)
    if not match:
        await message.answer(
            "❌ Неверный формат. Введите два числа <b>X</b> и <b>Z</b> через пробел (например: <code>6540 8120</code>):",
            parse_mode="HTML",
        )
        return

    try:
        x = float(match.group(1).replace(",", "."))
        z = float(match.group(2).replace(",", "."))
        if not (math.isfinite(x) and math.isfinite(z)):
            raise ValueError
    except ValueError:
        await message.answer("❌ Некорректные числовые координаты. Попробуйте снова:")
        return

    if not terrain_map or not terrain_map.is_loaded:
        await state.set_state(None)
        await message.answer("❌ Карта высот временно недоступна. Попробуйте оформить заказ по позиции персонажа.")
        return

    y_height = terrain_map.get_height(x, z)
    if y_height is None:
        await message.answer(
            "❌ Координаты выходят за пределы карты Черноруси (<code>0 .. 15360</code>). Введите корректные X и Z:",
            parse_mode="HTML",
        )
        return

    if y_height < 0.5:
        await message.answer(
            f"⚠️ Точка <code>X={round(x, 1)} / Z={round(z, 1)}</code> находится в море или под водой "
            f"(высота <code>{round(y_height, 2)} м</code>). Укажите координаты на суше:",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    cart: dict[str, int] = data.get("cart", {})
    if not cart:
        await state.set_state(None)
        await message.answer("🛒 Ваша корзина пуста.")
        return

    total_sum = 0
    for str_id, qty in cart.items():
        item = await db.get_shop_item(int(str_id))
        if item:
            total_sum += int(item["price"]) * int(qty)

    await state.update_data(
        checkout_pos={
            "source": "manual",
            "x": round(x, 2),
            "y_height": round(y_height, 3),
            "z": round(z, 2),
        }
    )
    await state.set_state(CheckoutOrder.confirm_position)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и заказать сюда", callback_data="checkout:confirm")],
        [InlineKeyboardButton(text="✏️ Ввести другие X Z", callback_data="checkout:manual")],
        [InlineKeyboardButton(text="⬅️ Вернуться в корзину", callback_data="cart:view")],
    ])
    await message.answer(
        f"📍 <b>Подтверждение точки доставки ({total_sum} 🪙)</b>\n\n"
        f"🧭 Способ: <b>По координатам карты</b>\n"
        f"🗺 Координаты: <code>X={round(x, 1)} / Z={round(z, 1)}</code>\n"
        f"⛰ Высота земли по карте: <code>{round(y_height, 2)} м</code>\n\n"
        "⚠️ <i>Убедитесь, что точка находится на открытой местности (не внутри здания).</i>",
        reply_markup=kb,
        parse_mode="HTML",
    )
@router.callback_query(CheckoutOrder.confirm_position, F.data == "checkout:confirm")
async def checkout_confirm_order(callback: CallbackQuery, db, nitrado_api, terrain_map, state: FSMContext):
    lock = _checkout_locks.setdefault(callback.from_user.id, asyncio.Lock())
    if lock.locked():
        await callback.answer("Заказ уже обрабатывается, подождите...", show_alert=True)
        return

    async with lock:
        user = await db.get_user(callback.from_user.id)
        if not user:
            await state.set_state(None)
            await callback.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
            return

        data = await state.get_data()
        cart: dict[str, int] = data.get("cart", {})
        saved_pos: dict | None = data.get("checkout_pos")
        if not cart or not saved_pos:
            await state.set_state(None)
            await callback.answer("Данные заказа устарели. Откройте корзину заново.", show_alert=True)
            return

        source = saved_pos.get("source", "live")
        used_terrain_map = None

        if source == "manual":
            x = float(saved_pos["x"])
            y_height = float(saved_pos["y_height"])
            z = float(saved_pos["z"])
            used_terrain_map = terrain_map
        else:
            game_nick = str(user["game_nick"] or "").strip()
            player_id = str(user["player_id"] or "").strip() if "player_id" in user.keys() and user["player_id"] else None
            live_pos = await nitrado_api.get_player_live_position(
                game_nick=game_nick,
                player_id=player_id,
                max_age_seconds=POSITION_MAX_AGE_SECONDS,
            )
            if not live_pos or not live_pos.get("is_fresh"):
                await callback.answer(
                    "Координаты устарели или вы вышли с сервера. Нажмите «Обновить позицию».",
                    show_alert=True,
                )
                await _check_and_render_checkout_position(callback, db, nitrado_api, state)
                return

            live_x = float(live_pos["x"])
            live_z = float(live_pos["z"])
            drift = math.hypot(live_x - float(saved_pos["x"]), live_z - float(saved_pos["z"]))
            if drift > MAX_CONFIRM_DRIFT_METERS:
                await callback.answer(
                    f"Вы переместились на {round(drift)} м. Подтвердите новую позицию.",
                    show_alert=True,
                )
                await _check_and_render_checkout_position(callback, db, nitrado_api, state)
                return

            x = float(saved_pos["x"])
            y_height = float(saved_pos["y_height"])
            z = float(saved_pos["z"])

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
            await state.update_data(cart={}, checkout_pos=None)
            await state.set_state(None)
            await callback.answer("Выбранные товары больше недоступны.", show_alert=True)
            return

        if int(user["balance"] or 0) < total_sum:
            await state.set_state(None)
            await callback.answer(
                f"Недостаточно средств! Нужно {total_sum} 🪙, на балансе {user['balance']} 🪙.",
                show_alert=True,
            )
            return
        await state.set_state(None)

        async with db.connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute("SELECT balance FROM users WHERE id = ?", (int(user["id"]),))
                row = await cur.fetchone()
                current_bal = int(row["balance"] or 0) if row else 0
                if current_bal < total_sum:
                    await conn.rollback()
                    await callback.answer(
                        f"Недостаточно средств! Нужно {total_sum} 🪙, на балансе {current_bal} 🪙.",
                        show_alert=True,
                    )
                    return

                await conn.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (total_sum, int(user["id"])))
                await conn.commit()
            except Exception:
                await conn.rollback()
                await state.set_state(CheckoutOrder.confirm_position)
                raise

        try:
            ok_spawn = await nitrado_api.append_json_spawn_orders(
                items=order_items,
                x=x,
                y_height=y_height,
                z=z,
                radius=4.0,
                terrain_map=used_terrain_map,
            )
        except Exception:
            logger.exception("Сбой записи заказа на Nitrado для пользователя %s", user["id"])
            ok_spawn = False

        if not ok_spawn:
            try:
                await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (total_sum, int(user["id"])))
            except Exception:
                logger.critical(
                    "КРИТИЧНО: Не удалось вернуть %d монет пользователю id=%s (tg=%s) после сбоя Nitrado!",
                    total_sum,
                    user["id"],
                    user["telegram_id"],
                )
            await state.set_state(CheckoutOrder.confirm_position)
            await callback.answer(
                "Ошибка записи заказа на сервер. Монеты возвращены, попробуйте ещё раз.",
                show_alert=True,
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
            x=round(x, 1),
            z=round(z, 1),
        )
        await state.update_data(cart={}, checkout_pos=None)

        new_bal = current_bal - total_sum
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🖥 Вернуться в терминал", callback_data="terminal")],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu")],
        ])
        await safe_render_menu(
            callback,
            f"✅ <b>Заказ успешно оформлен!</b>\n\n"
            f"📍 Точка спавна: <code>X={round(x, 1)} / Z={round(z, 1)}</code> (высота <code>{round(y_height, 2)} м</code>)\n"
            f"💸 Списано: <b>{total_sum} 🪙</b> | Остаток: <b>{new_bal} 🪙</b>\n"
            f"🔄 Весь заказанный лут появится на этой точке после ближайшего рестарта сервера.",
            kb,
        )
        await callback.answer("Заказ принят!")
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
        await safe_render_menu(callback, text, kb)
        await callback.answer()


@router.callback_query(F.data == "daily_bonus")
async def claim_daily(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return

    ok, msg = await db.claim_daily_bonus(callback.from_user.id)
    await callback.answer(msg, show_alert=True)
    if ok:
        await wallet(callback, db)
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
