import asyncio
import contextlib
import html
import logging
import math
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from database.db import DEFAULT_ECONOMY_CONFIG

router = Router()
logger = logging.getLogger(__name__)

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
COORDS_INPUT_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*[,;/\s]+\s*(-?\d+(?:[.,]\d+)?)\s*$")
POSITION_MAX_AGE_SECONDS = 90
ITEMS_PER_PAGE = 10

_checkout_locks: dict[int, asyncio.Lock] = {}


class CheckoutOrder(StatesGroup):
    confirm_position = State()
    enter_manual_coords = State()


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
    cat_create_title = State()
    cat_rename_title = State()
    sub_create_title = State()
    sub_rename_title = State()


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


def _chunk_buttons(buttons: list[InlineKeyboardButton], cols: int = 2) -> list[list[InlineKeyboardButton]]:
    """Раскладывает список кнопок сеткой по cols в ряд."""
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def _cancel_kb(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=back_cb)]
    ])
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

    cat_row = await db.get_shop_category_by_slug(key)
    cat_title_raw = str(cat_row["title"]) if cat_row else key
    cat_title = html.escape(cat_title_raw)

    if sub_id is None:
        subcats = await db.get_subcategories(key)
        sub_btns = [
            InlineKeyboardButton(text=f"📁 {sc['name']}", callback_data=f"subcat:{key}:{sc['id']}")
            for sc in subcats
        ]
        rows.extend(_chunk_buttons(sub_btns, cols=2))
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
    rows = _chunk_buttons(cat_buttons, cols=2)
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
    cat_row = await db.get_shop_category_by_slug(key)
    if not cat_row:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    text, markup = await _build_category_view(db, state, key, sub_id=None)
    await _safe_render_menu(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("subcat:"))
async def open_subcategory(callback: CallbackQuery, db, state: FSMContext):
    _, key, sub_id_str = callback.data.split(":", 2)
    cat_row = await db.get_shop_category_by_slug(key)
    if not cat_row:
        await callback.answer("Категория не найдена", show_alert=True)
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

        if not valid_cart:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🖥 В терминал", callback_data="terminal")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
            ])
            await _safe_render_menu(callback, "🛒 <b>Ваша корзина пуста.</b>", kb)
            await callback.answer()
            return

        lines.append(f"\n💰 <b>Итого к оплате: {total_sum} 🪙</b>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout:start")],
            [InlineKeyboardButton(text="🗑 Очистить корзину", callback_data="cart:clear")],
            [InlineKeyboardButton(text="⬅️ Продолжить покупки", callback_data="terminal")],
        ])
        await _safe_render_menu(callback, "\n".join(lines), kb)
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
        await _safe_render_menu(callback, text, kb)
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
    await _safe_render_menu(callback, text, kb)
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
    await _safe_render_menu(
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
        await callback.answer("⏳ Заказ уже оформляется, подождите...", show_alert=True)
        return

    async with lock:
        user = await db.get_user(callback.from_user.id)
        if not user:
            await state.clear()
            await callback.answer("Профиль не найден. Введите /start.", show_alert=True)
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

            x = float(live_pos["x"])
            y_height = float(live_pos["y_height"])
            z = float(live_pos["z"])

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
            await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (total_sum, int(user["id"])))
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
        await _safe_render_menu(
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
        await _safe_render_menu(callback, text, kb)
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

    cats = await db.get_shop_categories()
    first_slug = str(cats[0]["slug"]) if cats else "weapon"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="ashop:add")],
        [InlineKeyboardButton(text="📋 Товары и редактирование", callback_data=f"ashop:list:{first_slug}:0")],
        [
            InlineKeyboardButton(text="🗂 Категории", callback_data="acat:menu"),
            InlineKeyboardButton(text="📁 Подкатегории", callback_data="asub:menu"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад в админ-панель", callback_data="menu")],
    ])
    text = "🛠 <b>Управление магазином</b>\nВыберите раздел:"

    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb, parse_mode="HTML")
    elif getattr(event, "id", None) == "0":
        await event.message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await _safe_render_menu(event, text, kb)
        await event.answer()


# ---------- 3.1 УПРАВЛЕНИЕ КАТЕГОРИЯМИ ----------

async def _render_acat_menu(callback: CallbackQuery, db) -> None:
    cats = await db.get_shop_categories()
    cat_btns = [
        InlineKeyboardButton(text=str(c["title"]), callback_data=f"acat:view:{c['id']}")
        for c in cats
    ]
    rows = _chunk_buttons(cat_btns, cols=2)
    rows.append([InlineKeyboardButton(text="➕ Создать категорию", callback_data="acat:add")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню магазина", callback_data="admin_shop")])

    text = (
        f"🗂 <b>Управление категориями</b> (всего: {len(cats)})\n"
        "Нажмите на категорию для настройки, сортировки или удаления:"
    )
    await _safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "acat:menu")
async def acat_menu(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    await _render_acat_menu(callback, db)
    await callback.answer()


@router.callback_query(F.data == "acat:add")
async def acat_add_start(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    await state.set_state(AdminShop.cat_create_title)
    await _safe_render_menu(
        callback,
        "➕ <b>Создание категории</b>\n\n"
        "Введите название новой категории (желательно с эмодзи в начале, например: <code>🚗 Транспорт</code>):",
        _cancel_kb("acat:menu"),
    )
    await callback.answer()


@router.message(AdminShop.cat_create_title)
async def acat_add_save(message: Message, db, state: FSMContext):
    title = (message.text or "").strip()
    if not title or len(title) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов. Введите название категории:",
            reply_markup=_cancel_kb("acat:menu"),
        )
        return

    await db.add_shop_category(title)
    await state.set_state(None)
    await message.answer(
        f"✅ Категория <b>{html.escape(title)}</b> создана!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗂 К списку категорий", callback_data="acat:menu")]
        ]),
        parse_mode="HTML",
    )


async def _render_acat_detail(callback: CallbackQuery, db, cat_id: int) -> bool:
    cat = await db.get_shop_category_by_id(cat_id)
    if not cat:
        await callback.answer("Категория не найдена", show_alert=True)
        return False

    items_cnt, subs_cnt = await db.count_category_contents(str(cat["slug"]))
    text = (
        f"🗂 <b>Категория: {html.escape(str(cat['title']))}</b>\n\n"
        f"• Ключ (slug): <code>{html.escape(str(cat['slug']))}</code>\n"
        f"• Подкатегорий: <b>{subs_cnt}</b>\n"
        f"• Товаров внутри: <b>{items_cnt}</b>"
    )
    rows = [
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"acat:rename:{cat_id}")],
        [
            InlineKeyboardButton(text="⬆️ Выше", callback_data=f"acat:move:{cat_id}:up"),
            InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"acat:move:{cat_id}:down"),
        ],
        [InlineKeyboardButton(text="🗑 Удалить категорию", callback_data=f"acat:del_ask:{cat_id}")],
        [InlineKeyboardButton(text="⬅️ Ко всем категориям", callback_data="acat:menu")],
    ]
    await _safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    return True
@router.callback_query(F.data.startswith("acat:view:"))
async def acat_view(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    cat_id = int(callback.data.split(":")[2])
    if await _render_acat_detail(callback, db, cat_id):
        await callback.answer()


@router.callback_query(F.data.startswith("acat:move:"))
async def acat_move(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    _, _, cat_id_str, direction = callback.data.split(":", 3)
    cat_id = int(cat_id_str)
    await db.move_shop_category(cat_id, direction)
    await _render_acat_detail(callback, db, cat_id)
    await callback.answer("Порядок обновлён")


@router.callback_query(F.data.startswith("acat:rename:"))
async def acat_rename_start(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    cat_id = int(callback.data.split(":")[2])
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(AdminShop.cat_rename_title)
    await _safe_render_menu(
        callback,
        "✏️ Введите новое название категории (до 40 символов):",
        _cancel_kb(f"acat:view:{cat_id}"),
    )
    await callback.answer()


@router.message(AdminShop.cat_rename_title)
async def acat_rename_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    cat_id = int(data.get("edit_cat_id", 0))
    title = (message.text or "").strip()
    if not title or len(title) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов. Попробуйте снова:",
            reply_markup=_cancel_kb(f"acat:view:{cat_id}"),
        )
        return

    await db.rename_shop_category(cat_id, title)
    await state.set_state(None)
    await message.answer(
        f"✅ Категория переименована в <b>{html.escape(title)}</b>!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К категории", callback_data=f"acat:view:{cat_id}")]
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("acat:del_ask:"))
async def acat_delete_ask(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    cat_id = int(callback.data.split(":")[2])
    cat = await db.get_shop_category_by_id(cat_id)
    if not cat:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    items_cnt, subs_cnt = await db.count_category_contents(str(cat["slug"]))
    if items_cnt > 0 or subs_cnt > 0:
        text = (
            f"⚠️ <b>Внимание! Категория «{html.escape(str(cat['title']))}» не пуста.</b>\n\n"
            f"Внутри находятся: <b>{items_cnt}</b> товаров и <b>{subs_cnt}</b> подкатегорий.\n"
            "Если удалить категорию принудительно, все товары и подкатегории в ней также будут удалены!"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💥 Удалить вместе со всеми товарами", callback_data=f"acat:del_yes:{cat_id}:1")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"acat:view:{cat_id}")],
        ])
    else:
        text = f"🗑 Удалить пустую категорию <b>«{html.escape(str(cat['title']))}»</b>?"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"acat:del_yes:{cat_id}:0")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"acat:view:{cat_id}")],
        ])

    await _safe_render_menu(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("acat:del_yes:"))
async def acat_delete_confirm(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    _, _, cat_id_str, cascade_str = callback.data.split(":", 3)
    ok = await db.delete_shop_category(int(cat_id_str), cascade=(cascade_str == "1"))
    if not ok:
        await callback.answer("Не удалось удалить категорию", show_alert=True)
        return
    await callback.answer("🗑 Категория удалена", show_alert=True)
    await _render_acat_menu(callback, db)


# ---------- 3.2 УПРАВЛЕНИЕ ПОДКАТЕГОРИЯМИ ----------

@router.callback_query(F.data == "asub:menu")
async def asub_menu(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    cats = await db.get_shop_categories()
    btns = [
        InlineKeyboardButton(text=str(c["title"]), callback_data=f"asub:cat:{c['slug']}")
        for c in cats
    ]
    rows = _chunk_buttons(btns, cols=2)
    rows.append([InlineKeyboardButton(text="⬅️ В меню магазина", callback_data="admin_shop")])
    await _safe_render_menu(
        callback,
        "📁 <b>Управление подкатегориями</b>\nВыберите категорию:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def _render_asub_cat(callback: CallbackQuery, db, slug: str) -> None:
    cat = await db.get_shop_category_by_slug(slug)
    cat_title = html.escape(str(cat["title"])) if cat else html.escape(slug)
    subs = await db.get_subcategories(slug)

    sub_btns = [
        InlineKeyboardButton(text=f"📁 {s['name']}", callback_data=f"asub:view:{s['id']}")
        for s in subs
    ]
    rows = _chunk_buttons(sub_btns, cols=2)
    rows.append([InlineKeyboardButton(text="➕ Создать подкатегорию", callback_data=f"asub:add:{slug}")])
    rows.append([InlineKeyboardButton(text="⬅️ Выбор категории", callback_data="asub:menu")])

    text = f"📁 <b>Подкатегории в «{cat_title}»</b>"
    if not subs:
        text += "\n\n<i>В этой категории пока нет подкатегорий.</i>"
    await _safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
@router.callback_query(F.data.startswith("asub:cat:"))
async def asub_cat_view(callback: CallbackQuery, db, settings, state: FSMContext):
    await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    slug = callback.data.split(":", 2)[2]
    await _render_asub_cat(callback, db, slug)
    await callback.answer()


@router.callback_query(F.data.startswith("asub:add:"))
async def asub_add_start(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    slug = callback.data.split(":", 2)[2]
    await state.update_data(sub_target_cat=slug)
    await state.set_state(AdminShop.sub_create_title)
    await _safe_render_menu(
        callback,
        "➕ Введите название новой подкатегории (до 40 символов):",
        _cancel_kb(f"asub:cat:{slug}"),
    )
    await callback.answer()


@router.message(AdminShop.sub_create_title)
async def asub_add_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    slug = str(data.get("sub_target_cat", ""))
    name = (message.text or "").strip()
    if not name or len(name) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов. Попробуйте снова:",
            reply_markup=_cancel_kb(f"asub:cat:{slug}"),
        )
        return

    await db.add_subcategory(slug, name)
    await state.set_state(None)
    await message.answer(
        f"✅ Подкатегория <b>{html.escape(name)}</b> добавлена!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К подкатегориям", callback_data=f"asub:cat:{slug}")]
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("asub:view:"))
async def asub_detail(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    sub_id = int(callback.data.split(":")[2])
    sub = await db.get_subcategory(sub_id)
    if not sub:
        await callback.answer("Подкатегория не найдена", show_alert=True)
        return

    slug = str(sub["category"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"asub:rename:{sub_id}")],
        [InlineKeyboardButton(text="🗑 Удалить (товары останутся)", callback_data=f"asub:del:{sub_id}:{slug}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"asub:cat:{slug}")],
    ])
    await _safe_render_menu(
        callback,
        f"📁 <b>Подкатегория: {html.escape(str(sub['name']))}</b>",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asub:rename:"))
async def asub_rename_start(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    sub_id = int(callback.data.split(":")[2])
    await state.update_data(edit_sub_id=sub_id)
    await state.set_state(AdminShop.sub_rename_title)
    await _safe_render_menu(
        callback,
        "✏️ Введите новое название подкатегории (до 40 символов):",
        _cancel_kb(f"asub:view:{sub_id}"),
    )
    await callback.answer()


@router.message(AdminShop.sub_rename_title)
async def asub_rename_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    sub_id = int(data.get("edit_sub_id", 0))
    name = (message.text or "").strip()
    if not name or len(name) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов:",
            reply_markup=_cancel_kb(f"asub:view:{sub_id}"),
        )
        return

    await db.rename_subcategory(sub_id, name)
    await state.set_state(None)
    await message.answer(
        f"✅ Подкатегория переименована в <b>{html.escape(name)}</b>!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К подкатегории", callback_data=f"asub:view:{sub_id}")]
        ]),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("asub:del:"))
async def asub_delete(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    _, _, sub_id_str, slug = callback.data.split(":", 3)
    await db.delete_subcategory(int(sub_id_str))
    await callback.answer("🗑 Подкатегория удалена", show_alert=True)
    await _render_asub_cat(callback, db, slug)
# ---------- 3.3 ДОБАВЛЕНИЕ ТОВАРА ----------

@router.callback_query(F.data == "ashop:add")
async def ashop_add_start(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    cats = await db.get_shop_categories()
    cat_btns = [
        InlineKeyboardButton(text=str(c["title"]), callback_data=f"ashop_cat:{c['slug']}")
        for c in cats
    ]
    rows = _chunk_buttons(cat_btns, cols=2)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")])
    await state.set_state(AdminShop.add_category)
    await _safe_render_menu(
        callback,
        "➕ <b>Добавление товара (Шаг 1/5)</b>\nВыберите категорию:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(AdminShop.add_category, F.data.startswith("ashop_cat:"))
async def ashop_add_category(callback: CallbackQuery, db, state: FSMContext):
    _, cat = callback.data.split(":", 1)
    cat_row = await db.get_shop_category_by_slug(cat)
    if not cat_row:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    await state.update_data(category=cat)
    subcats = await db.get_subcategories(cat)

    sub_btns = [
        InlineKeyboardButton(text=f"📁 {sc['name']}", callback_data=f"ashop_sub:{sc['id']}")
        for sc in subcats
    ]
    rows = _chunk_buttons(sub_btns, cols=2)
    rows.append([InlineKeyboardButton(text="➕ Новая подкатегория", callback_data="ashop_sub_new")])
    rows.append([InlineKeyboardButton(text="⏭ Без подкатегории", callback_data="ashop_sub:0")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")])

    await state.set_state(AdminShop.add_subcategory)
    await _safe_render_menu(
        callback,
        f"Категория: <b>{html.escape(str(cat_row['title']))}</b>\nВыберите или создайте подкатегорию:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(AdminShop.add_subcategory, F.data == "ashop_sub_new")
async def ashop_sub_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminShop.new_subcat_name)
    await _safe_render_menu(
        callback,
        "Введите название новой подкатегории (до 40 символов):",
        _cancel_kb("admin_shop"),
    )
    await callback.answer()


@router.message(AdminShop.new_subcat_name)
async def ashop_sub_new_name(message: Message, db, state: FSMContext):
    sub_name = (message.text or "").strip()
    if not sub_name or len(sub_name) > 40:
        await message.answer("❌ Название должно быть от 1 до 40 символов:", reply_markup=_cancel_kb("admin_shop"))
        return

    data = await state.get_data()
    cat = data["category"]
    sub_id = await db.add_subcategory(cat, sub_name)
    await state.update_data(subcategory_id=sub_id)
    await state.set_state(AdminShop.add_display_name)
    await message.answer(
        f"Подкатегория: <b>{html.escape(sub_name)}</b>\n"
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        reply_markup=_cancel_kb("admin_shop"),
        parse_mode="HTML",
    )


@router.callback_query(AdminShop.add_subcategory, F.data.startswith("ashop_sub:"))
async def ashop_add_subcategory(callback: CallbackQuery, state: FSMContext):
    _, sub_id_str = callback.data.split(":", 1)
    sub_id = int(sub_id_str)
    await state.update_data(subcategory_id=sub_id if sub_id > 0 else None)
    await state.set_state(AdminShop.add_display_name)
    await _safe_render_menu(
        callback,
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        _cancel_kb("admin_shop"),
    )
    await callback.answer()


@router.message(AdminShop.add_display_name)
async def ashop_display_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name or len(name) > 45:
        await message.answer("❌ Название должно быть от 1 до 45 символов:", reply_markup=_cancel_kb("admin_shop"))
        return

    await state.update_data(display_name=name)
    await state.set_state(AdminShop.add_classname)
    await message.answer(
        "Введите игровой <b>classname</b> предмета из DayZ (латиница, цифры, подчёркивание).\n"
        "Например: <code>AK74</code>",
        reply_markup=_cancel_kb("admin_shop"),
        parse_mode="HTML",
    )


@router.message(AdminShop.add_classname)
async def ashop_classname(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not CLASSNAME_RE.match(raw):
        await message.answer(
            "❌ Некорректный classname. Используйте только <b>A-Z, a-z, 0-9, _</b> (без пробелов):",
            reply_markup=_cancel_kb("admin_shop"),
            parse_mode="HTML",
        )
        return

    await state.update_data(game_classname=raw)
    await state.set_state(AdminShop.add_price)
    await message.answer("Введите цену товара в монетах (целое число больше 0):", reply_markup=_cancel_kb("admin_shop"))


@router.message(AdminShop.add_price)
async def ashop_price(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        price = int(raw)
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Цена должна быть положительным целым числом:", reply_markup=_cancel_kb("admin_shop"))
        return

    await state.update_data(price=price)
    await state.set_state(AdminShop.add_photo)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Создать без фото", callback_data="ashop:add_skip_photo")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")],
    ])
    await message.answer(
        "🖼 Отправьте <b>фотографию товара</b> (как фото, не файлом) или нажмите кнопку ниже:",
        reply_markup=kb,
        parse_mode="HTML",
    )
async def _finish_create_item(target: Message | CallbackQuery, db, state: FSMContext, photo_file_id: str | None) -> None:
    data = await state.get_data()
    await state.set_state(None)

    cat_slug = str(data["category"])
    item_id = await db.add_shop_item(
        category=cat_slug,
        display_name=data["display_name"],
        game_classname=data["game_classname"],
        price=int(data["price"]),
        subcategory_id=data.get("subcategory_id"),
        photo_file_id=photo_file_id,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё товар", callback_data="ashop:add")],
        [InlineKeyboardButton(text="📋 Открыть список товаров", callback_data=f"ashop:list:{cat_slug}:0")],
        [InlineKeyboardButton(text="🛠 В меню магазина", callback_data="admin_shop")],
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
        await _safe_render_menu(target, text, kb)
        await target.answer()


@router.callback_query(AdminShop.add_photo, F.data == "ashop:add_skip_photo")
async def ashop_add_skip_photo(callback: CallbackQuery, db, state: FSMContext):
    await _finish_create_item(callback, db, state, photo_file_id=None)


@router.message(AdminShop.add_photo)
async def ashop_add_photo_save(message: Message, db, state: FSMContext):
    if not message.photo:
        await message.answer(
            "❌ Пожалуйста, отправьте именно <b>фотографию</b> товара (или нажмите «Создать без фото»):",
            parse_mode="HTML",
        )
        return

    photo_id = message.photo[-1].file_id
    await _finish_create_item(message, db, state, photo_file_id=photo_id)


# ---------- 3.4 СПИСОК ТОВАРОВ С ПАГИНАЦИЕЙ И КАТЕГОРИЯМИ В 2 РЯДА ----------

async def _render_ashop_list(callback: CallbackQuery, db, cat: str, page: int = 0) -> None:
    cats = await db.get_shop_categories()
    if not cats:
        await _safe_render_menu(
            callback,
            "📋 <b>Список товаров</b>\n\n<i>Сначала создайте хотя бы одну категорию.</i>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗂 Создать категорию", callback_data="acat:add")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_shop")],
            ]),
        )
        return

    cat_map = {str(c["slug"]): str(c["title"]) for c in cats}
    if cat not in cat_map:
        cat = str(cats[0]["slug"])

    all_cat_items = await db.get_all_category_items_with_sub(cat)
    total_items = len(all_cat_items)
    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ITEMS_PER_PAGE
    page_items = all_cat_items[start_idx:start_idx + ITEMS_PER_PAGE]

    rows = []
    for it in page_items:
        pic_icon = "🖼 " if it["photo_file_id"] else ""
        sub_prefix = f"[{it['sub_name']}] " if it["sub_name"] else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{pic_icon}{sub_prefix}{it['display_name']} — {it['price']} 🪙",
                callback_data=f"ashop:edit:{it['id']}:{page}",
            )
        ])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"ashop:list:{cat}:{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="cart:noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"ashop:list:{cat}:{page + 1}"))
        rows.append(nav_row)

    # Вкладки категорий в 2 столбца (два ряда по паре кнопок)
    cat_tabs = [
        InlineKeyboardButton(
            text=("• " if str(c["slug"]) == cat else "") + str(c["title"]),
            callback_data=f"ashop:list:{c['slug']}:0",
        )
        for c in cats
    ]
    rows.extend(_chunk_buttons(cat_tabs, cols=2))
    rows.append([
        InlineKeyboardButton(text="➕ Добавить сюда", callback_data=f"ashop_cat_quick:{cat}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_shop"),
    ])

    text = f"📋 <b>Товары — {html.escape(cat_map[cat])}</b> (всего: {total_items})"
    if not all_cat_items:
        text += "\n\n<i>В этой категории пока нет товаров.</i>"
    else:
        text += "\nНажмите на товар для редактирования:"

    await _safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("ashop_cat_quick:"))
async def ashop_quick_add(callback: CallbackQuery, db, settings, state: FSMContext):
    """Быстрое добавление товара сразу в текущую открытую категорию."""
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    slug = callback.data.split(":", 1)[1]
    fake_cb = callback.model_copy(update={"data": f"ashop_cat:{slug}"})
    await ashop_add_category(fake_cb, db, state)


@router.callback_query(F.data.startswith("ashop:list:"))
async def ashop_list(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split(":")
    cat = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0
    await _render_ashop_list(callback, db, cat, page)
    await callback.answer()
# ---------- 3.5 КАРТОЧКА РЕДАКТИРОВАНИЯ, ДУБЛИРОВАНИЕ И СМЕНА ГРУППЫ ----------

async def _render_admin_item_detail(callback: CallbackQuery, db, item_id: int, page: int = 0) -> bool:
    item = await db.get_shop_item(item_id)
    if not item:
        await callback.answer("Товар не найден", show_alert=True)
        return False

    cat_row = await db.get_shop_category_by_slug(str(item["category"]))
    cat_title = str(cat_row["title"]) if cat_row else str(item["category"])

    sub_name = "Без группы"
    if item["subcategory_id"]:
        sub = await db.get_subcategory(int(item["subcategory_id"]))
        if sub:
            sub_name = str(sub["name"])

    photo_status = "Прикреплено ✅" if item["photo_file_id"] else "Нет ❌"
    text = (
        f"✏️ <b>Редактирование товара #{item_id}</b>\n\n"
        f"• Категория: <b>{html.escape(cat_title)}</b>\n"
        f"• Подкатегория: <b>{html.escape(sub_name)}</b>\n"
        f"• Название: <b>{html.escape(str(item['display_name']))}</b>\n"
        f"• Classname: <code>{html.escape(str(item['game_classname']))}</code>\n"
        f"• Цена: <b>{item['price']} 🪙</b>\n"
        f"• Фото: <b>{photo_status}</b>"
    )
    rows = [
        [
            InlineKeyboardButton(text="✏️ Название", callback_data=f"ashop_fld:display_name:{item_id}:{page}"),
            InlineKeyboardButton(text="🏷 Classname", callback_data=f"ashop_fld:game_classname:{item_id}:{page}"),
        ],
        [
            InlineKeyboardButton(text="💰 Цена", callback_data=f"ashop_fld:price:{item_id}:{page}"),
            InlineKeyboardButton(text="📁 Сменить группу", callback_data=f"ashop_move:{item_id}:{page}"),
        ],
        [
            InlineKeyboardButton(text="🖼 Заменить фото", callback_data=f"ashop_photo:{item_id}:{page}"),
            InlineKeyboardButton(text="📋 Дублировать", callback_data=f"ashop_dup:{item_id}:{page}"),
        ],
    ]
    if item["photo_file_id"]:
        rows.append([
            InlineKeyboardButton(text="🧹 Удалить фото товара", callback_data=f"ashop_del_photo:{item_id}:{page}")
        ])
    rows.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ashop_del_ask:{item_id}:{item['category']}:{page}"),
        InlineKeyboardButton(text="⬅️ К списку", callback_data=f"ashop:list:{item['category']}:{page}"),
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await _safe_render_menu(callback, text, kb, photo_id=item["photo_file_id"])
    return True


@router.callback_query(F.data.startswith("ashop:edit:"))
async def ashop_edit_item(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    parts = callback.data.split(":")
    item_id = int(parts)
    page = int(parts) if len(parts) > 3 else 0
    if await _render_admin_item_detail(callback, db, item_id, page):
        await callback.answer()


@router.callback_query(F.data.startswith("ashop_dup:"))
async def ashop_duplicate_item(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    _, item_id_str, page_str = callback.data.split(":", 2)
    new_id = await db.duplicate_shop_item(int(item_id_str))
    if not new_id:
        await callback.answer("Не удалось дублировать товар", show_alert=True)
        return
    await callback.answer("📋 Создана копия товара!", show_alert=True)
    await _render_admin_item_detail(callback, db, new_id, int(page_str))


@router.callback_query(F.data.startswith("ashop_move:"))
async def ashop_move_group_menu(callback: CallbackQuery, db, settings):
    """Выбор новой категории/подкатегории для существующего товара."""
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    _, item_id_str, page_str = callback.data.split(":", 2)
    item_id, page = int(item_id_str), int(page_str)
    item = await db.get_shop_item(item_id)
    if not item:
        return

    cats = await db.get_shop_categories()
    cat_btns = [
        InlineKeyboardButton(
            text=("• " if str(c["slug"]) == str(item["category"]) else "") + str(c["title"]),
            callback_data=f"ashop_mv_cat:{item_id}:{c['slug']}:{page}",
        )
        for c in cats
    ]
    rows = _chunk_buttons(cat_btns, cols=2)
    rows.append([InlineKeyboardButton(text="⬅️ Назад к товару", callback_data=f"ashop:edit:{item_id}:{page}")])
    await _safe_render_menu(
        callback,
        "📁 <b>Перенос товара</b>\nВыберите целевую категорию:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_mv_cat:"))
async def ashop_move_select_sub(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    _, item_id_str, new_slug, page_str = callback.data.split(":", 3)
    item_id, page = int(item_id_str), int(page_str)

    subs = await db.get_subcategories(new_slug)
    sub_btns = [
        InlineKeyboardButton(
            text=f"📁 {s['name']}",
            callback_data=f"ashop_mv_set:{item_id}:{new_slug}:{s['id']}:{page}",
        )
        for s in subs
    ]
    rows = _chunk_buttons(sub_btns, cols=2)
    rows.append([InlineKeyboardButton(text="⏭ В корень категории (без группы)", callback_data=f"ashop_mv_set:{item_id}:{new_slug}:0:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ashop_move:{item_id}:{page}")])
    await _safe_render_menu(
        callback,
        "Выберите подкатегорию назначения:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_mv_set:"))
async def ashop_move_apply(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return
    _, item_id_str, new_slug, sub_id_str, page_str = callback.data.split(":", 4)
    item_id, sub_id, page = int(item_id_str), int(sub_id_str), int(page_str)

    await db.update_shop_item_field(item_id, "category", new_slug)
    await db.update_shop_item_field(item_id, "subcategory_id", sub_id if sub_id > 0 else None)
    await callback.answer("✅ Товар перемещён!", show_alert=True)
    await _render_admin_item_detail(callback, db, item_id, page)
@router.callback_query(F.data.startswith("ashop_fld:"))
async def ashop_field_prompt(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    parts = callback.data.split(":")
    field, item_id = parts, int(parts)
    page = int(parts) if len(parts) > 3 else 0
    prompts = {
        "display_name": "Введите новое отображаемое название товара (до 45 символов):",
        "game_classname": "Введите новый игровой classname (латиница, цифры, _):",
        "price": "Введите новую цену товара (целое число > 0):",
    }
    if field not in prompts:
        return

    await state.update_data(edit_field=field, edit_item_id=item_id, edit_item_page=page)
    await state.set_state(AdminShop.edit_value)
    await _safe_render_menu(callback, prompts[field], _cancel_kb(f"ashop:edit:{item_id}:{page}"))
    await callback.answer()


@router.message(AdminShop.edit_value)
async def ashop_field_save(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    field = str(data.get("edit_field", ""))
    item_id = int(data.get("edit_item_id", 0))
    page = int(data.get("edit_item_page", 0))

    if field not in ("display_name", "game_classname", "price"):
        await state.set_state(None)
        return

    if field == "price":
        try:
            val: str | int = int(raw)
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.answer(
                "❌ Цена должна быть целым числом больше 0:",
                reply_markup=_cancel_kb(f"ashop:edit:{item_id}:{page}"),
            )
            return
    elif field == "game_classname":
        if not CLASSNAME_RE.match(raw):
            await message.answer(
                "❌ Некорректный classname (разрешены A-Z, a-z, 0-9, _):",
                reply_markup=_cancel_kb(f"ashop:edit:{item_id}:{page}"),
            )
            return
        val = raw
    else:
        if not raw or len(raw) > 45:
            await message.answer(
                "❌ Название должно быть от 1 до 45 символов:",
                reply_markup=_cancel_kb(f"ashop:edit:{item_id}:{page}"),
            )
            return
        val = raw

    await db.update_shop_item_field(item_id, field, val)
    await state.set_state(None)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к товару", callback_data=f"ashop:edit:{item_id}:{page}")]
    ])
    await message.answer("✅ Параметр товара обновлён!", reply_markup=kb)


@router.callback_query(F.data.startswith("ashop_photo:"))
async def ashop_photo_prompt(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    parts = callback.data.split(":")
    item_id = int(parts)
    page = int(parts) if len(parts) > 2 else 0
    await state.update_data(edit_item_id=item_id, edit_item_page=page)
    await state.set_state(AdminShop.edit_photo)
    await _safe_render_menu(
        callback,
        "🖼 Отправьте <b>новую фотографию</b> для этого товара:",
        _cancel_kb(f"ashop:edit:{item_id}:{page}"),
    )
    await callback.answer()


@router.message(AdminShop.edit_photo)
async def ashop_photo_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    item_id = int(data.get("edit_item_id", 0))
    page = int(data.get("edit_item_page", 0))

    if not message.photo:
        await message.answer(
            "❌ Пожалуйста, отправьте именно <b>фотографию</b> (не документом/файлом):",
            reply_markup=_cancel_kb(f"ashop:edit:{item_id}:{page}"),
            parse_mode="HTML",
        )
        return

    await state.set_state(None)
    photo_id = message.photo[-1].file_id
    await db.update_shop_item_field(item_id, "photo_file_id", photo_id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к товару", callback_data=f"ashop:edit:{item_id}:{page}")]
    ])
    await message.answer("✅ Фотография товара обновлена!", reply_markup=kb)


@router.callback_query(F.data.startswith("ashop_del_photo:"))
async def ashop_delete_photo(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    parts = callback.data.split(":")
    item_id = int(parts)
    page = int(parts) if len(parts) > 2 else 0
    await db.update_shop_item_field(item_id, "photo_file_id", None)
    await callback.answer("🧹 Фотография удалена с товара", show_alert=True)
    await _render_admin_item_detail(callback, db, item_id, page)


@router.callback_query(F.data.startswith("ashop_del_ask:"))
async def ashop_delete_ask(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str, cat, page_str = callback.data.split(":", 3)
    item = await db.get_shop_item(int(item_id_str))
    if not item:
        await callback.answer("Товар уже удалён", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"ashop_del_yes:{item_id_str}:{cat}:{page_str}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"ashop:edit:{item_id_str}:{page_str}"),
        ]
    ])
    await _safe_render_menu(
        callback,
        f"⚠️ Вы уверены, что хотите удалить товар <b>«{html.escape(str(item['display_name']))}»</b>?",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_del_yes:"))
async def ashop_delete_item(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(user, settings):
        return

    _, item_id_str, cat, page_str = callback.data.split(":", 3)
    await db.delete_shop_item(int(item_id_str))
    await callback.answer("🗑 Товар удалён", show_alert=True)
    await _render_ashop_list(callback, db, cat, int(page_str))
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
        await _safe_render_menu(event, text, markup)
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
        await _safe_render_menu(event, text, kb)


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
