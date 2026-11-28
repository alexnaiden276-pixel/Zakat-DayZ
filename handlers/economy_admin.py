import html
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import DEFAULT_ECONOMY_CONFIG
from filters.admin import IsAdmin
from utils.ui import cancel_kb, chunk_buttons, notify_balance_change, safe_render_menu

router = Router(name="economy_admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

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
ITEMS_PER_PAGE = 10


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
# ==========================================
# 1. АДМИН-МЕНЮ УПРАВЛЕНИЯ МАГАЗИНОМ
# ==========================================

@router.callback_query(F.data == "admin_shop")
async def admin_shop_menu(event: Message | CallbackQuery, db, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

    cats = await db.get_shop_categories()
    first_slug = str(cats["slug"]) if cats else "weapon"

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
        await safe_render_menu(event, text, kb)
        await event.answer()


# ---------- 1.1 УПРАВЛЕНИЕ КАТЕГОРИЯМИ ----------

async def _render_acat_menu(callback: CallbackQuery, db) -> None:
    cats = await db.get_shop_categories()
    cat_btns = [
        InlineKeyboardButton(text=str(c["title"]), callback_data=f"acat:view:{c['id']}")
        for c in cats
    ]
    rows = chunk_buttons(cat_btns, cols=2)
    rows.append([InlineKeyboardButton(text="➕ Создать категорию", callback_data="acat:add")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню магазина", callback_data="admin_shop")])

    text = (
        f"🗂 <b>Управление категориями</b> (всего: {len(cats)})\n"
        "Нажмите на категорию для настройки, сортировки или удаления:"
    )
    await safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "acat:menu")
async def acat_menu(callback: CallbackQuery, db, state: FSMContext):
    await state.set_state(None)
    await _render_acat_menu(callback, db)
    await callback.answer()


@router.callback_query(F.data == "acat:add")
async def acat_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminShop.cat_create_title)
    await safe_render_menu(
        callback,
        "➕ <b>Создание категории</b>\n\n"
        "Введите название новой категории (желательно с эмодзи в начале, например: <code>🚗 Транспорт</code>):",
        cancel_kb("acat:menu"),
    )
    await callback.answer()


@router.message(AdminShop.cat_create_title)
async def acat_add_save(message: Message, db, state: FSMContext):
    title = (message.text or "").strip()
    if title.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Создание категории отменено.")
        return

    if not title or len(title) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов. Введите название категории:",
            reply_markup=cancel_kb("acat:menu"),
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
    await safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))
    return True


@router.callback_query(F.data.startswith("acat:view:"))
async def acat_view(callback: CallbackQuery, db, state: FSMContext):
    await state.set_state(None)
    cat_id = int(callback.data.split(":"))
    if await _render_acat_detail(callback, db, cat_id):
        await callback.answer()


@router.callback_query(F.data.startswith("acat:move:"))
async def acat_move(callback: CallbackQuery, db):
    _, _, cat_id_str, direction = callback.data.split(":", 3)
    cat_id = int(cat_id_str)
    await db.move_shop_category(cat_id, direction)
    await _render_acat_detail(callback, db, cat_id)
    await callback.answer("Порядок обновлён")


@router.callback_query(F.data.startswith("acat:rename:"))
async def acat_rename_start(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split(":"))
    await state.update_data(edit_cat_id=cat_id)
    await state.set_state(AdminShop.cat_rename_title)
    await safe_render_menu(
        callback,
        "✏️ Введите новое название категории (до 40 символов):",
        cancel_kb(f"acat:view:{cat_id}"),
    )
    await callback.answer()


@router.message(AdminShop.cat_rename_title)
async def acat_rename_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    cat_id = int(data.get("edit_cat_id", 0))
    title = (message.text or "").strip()
    if title.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Переименование отменено.")
        return

    if not title or len(title) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов. Попробуйте снова:",
            reply_markup=cancel_kb(f"acat:view:{cat_id}"),
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
async def acat_delete_ask(callback: CallbackQuery, db):
    cat_id = int(callback.data.split(":"))
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

    await safe_render_menu(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("acat:del_yes:"))
async def acat_delete_confirm(callback: CallbackQuery, db):
    _, _, cat_id_str, cascade_str = callback.data.split(":", 3)
    ok = await db.delete_shop_category(int(cat_id_str), cascade=(cascade_str == "1"))
    if not ok:
        await callback.answer("Не удалось удалить категорию", show_alert=True)
        return
    await callback.answer("🗑 Категория удалена", show_alert=True)
    await _render_acat_menu(callback, db)
# ---------- 1.2 УПРАВЛЕНИЕ ПОДКАТЕГОРИЯМИ ----------

@router.callback_query(F.data == "asub:menu")
async def asub_menu(callback: CallbackQuery, db, state: FSMContext):
    await state.set_state(None)

    cats = await db.get_shop_categories()
    btns = [
        InlineKeyboardButton(text=str(c["title"]), callback_data=f"asub:cat:{c['slug']}")
        for c in cats
    ]
    rows = chunk_buttons(btns, cols=2)
    rows.append([InlineKeyboardButton(text="⬅️ В меню магазина", callback_data="admin_shop")])
    await safe_render_menu(
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
    rows = chunk_buttons(sub_btns, cols=2)
    rows.append([InlineKeyboardButton(text="➕ Создать подкатегорию", callback_data=f"asub:add:{slug}")])
    rows.append([InlineKeyboardButton(text="⬅️ Выбор категории", callback_data="asub:menu")])

    text = f"📁 <b>Подкатегории в «{cat_title}»</b>"
    if not subs:
        text += "\n\n<i>В этой категории пока нет подкатегорий.</i>"
    await safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("asub:cat:"))
async def asub_cat_view(callback: CallbackQuery, db, state: FSMContext):
    await state.set_state(None)
    slug = callback.data.split(":", 2)
    await _render_asub_cat(callback, db, slug)
    await callback.answer()


@router.callback_query(F.data.startswith("asub:add:"))
async def asub_add_start(callback: CallbackQuery, state: FSMContext):
    slug = callback.data.split(":", 2)
    await state.update_data(sub_target_cat=slug)
    await state.set_state(AdminShop.sub_create_title)
    await safe_render_menu(
        callback,
        "➕ Введите название новой подкатегории (до 40 символов):",
        cancel_kb(f"asub:cat:{slug}"),
    )
    await callback.answer()


@router.message(AdminShop.sub_create_title)
async def asub_add_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    slug = str(data.get("sub_target_cat", ""))
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Создание подкатегории отменено.")
        return

    if not name or len(name) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов. Попробуйте снова:",
            reply_markup=cancel_kb(f"asub:cat:{slug}"),
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
async def asub_detail(callback: CallbackQuery, db):
    sub_id = int(callback.data.split(":"))
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
    await safe_render_menu(
        callback,
        f"📁 <b>Подкатегория: {html.escape(str(sub['name']))}</b>",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("asub:rename:"))
async def asub_rename_start(callback: CallbackQuery, state: FSMContext):
    sub_id = int(callback.data.split(":"))
    await state.update_data(edit_sub_id=sub_id)
    await state.set_state(AdminShop.sub_rename_title)
    await safe_render_menu(
        callback,
        "✏️ Введите новое название подкатегории (до 40 символов):",
        cancel_kb(f"asub:view:{sub_id}"),
    )
    await callback.answer()


@router.message(AdminShop.sub_rename_title)
async def asub_rename_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    sub_id = int(data.get("edit_sub_id", 0))
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Переименование отменено.")
        return

    if not name or len(name) > 40:
        await message.answer(
            "❌ Название должно быть от 1 до 40 символов:",
            reply_markup=cancel_kb(f"asub:view:{sub_id}"),
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
async def asub_delete(callback: CallbackQuery, db):
    _, _, sub_id_str, slug = callback.data.split(":", 3)
    await db.delete_subcategory(int(sub_id_str))
    await callback.answer("🗑 Подкатегория удалена", show_alert=True)
    await _render_asub_cat(callback, db, slug)
# ---------- 1.3 ДОБАВЛЕНИЕ ТОВАРА ----------

@router.callback_query(F.data == "ashop:add")
async def ashop_add_start(callback: CallbackQuery, db, state: FSMContext):
    cats = await db.get_shop_categories()
    cat_btns = [
        InlineKeyboardButton(text=str(c["title"]), callback_data=f"ashop_cat:{c['slug']}")
        for c in cats
    ]
    rows = chunk_buttons(cat_btns, cols=2)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")])
    await state.set_state(AdminShop.add_category)
    await safe_render_menu(
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
    rows = chunk_buttons(sub_btns, cols=2)
    rows.append([InlineKeyboardButton(text="➕ Новая подкатегория", callback_data="ashop_sub_new")])
    rows.append([InlineKeyboardButton(text="⏭ Без подкатегории", callback_data="ashop_sub:0")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_shop")])

    await state.set_state(AdminShop.add_subcategory)
    await safe_render_menu(
        callback,
        f"Категория: <b>{html.escape(str(cat_row['title']))}</b>\nВыберите или создайте подкатегорию:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(AdminShop.add_subcategory, F.data == "ashop_sub_new")
async def ashop_sub_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminShop.new_subcat_name)
    await safe_render_menu(
        callback,
        "Введите название новой подкатегории (до 40 символов):",
        cancel_kb("admin_shop"),
    )
    await callback.answer()


@router.message(AdminShop.new_subcat_name)
async def ashop_sub_new_name(message: Message, db, state: FSMContext):
    sub_name = (message.text or "").strip()
    if sub_name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Добавление товара отменено.")
        return

    if not sub_name or len(sub_name) > 40:
        await message.answer("❌ Название должно быть от 1 до 40 символов:", reply_markup=cancel_kb("admin_shop"))
        return

    data = await state.get_data()
    cat = data["category"]
    sub_id = await db.add_subcategory(cat, sub_name)
    await state.update_data(subcategory_id=sub_id)
    await state.set_state(AdminShop.add_display_name)
    await message.answer(
        f"Подкатегория: <b>{html.escape(sub_name)}</b>\n"
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        reply_markup=cancel_kb("admin_shop"),
        parse_mode="HTML",
    )


@router.callback_query(AdminShop.add_subcategory, F.data.startswith("ashop_sub:"))
async def ashop_add_subcategory(callback: CallbackQuery, state: FSMContext):
    _, sub_id_str = callback.data.split(":", 1)
    sub_id = int(sub_id_str)
    await state.update_data(subcategory_id=sub_id if sub_id > 0 else None)
    await state.set_state(AdminShop.add_display_name)
    await safe_render_menu(
        callback,
        "Введите отображаемое название товара в магазине (например: <code>АК-74 + магазин</code>):",
        cancel_kb("admin_shop"),
    )
    await callback.answer()


@router.message(AdminShop.add_display_name)
async def ashop_display_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if name.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Добавление товара отменено.")
        return

    if not name or len(name) > 45:
        await message.answer("❌ Название должно быть от 1 до 45 символов:", reply_markup=cancel_kb("admin_shop"))
        return

    await state.update_data(display_name=name)
    await state.set_state(AdminShop.add_classname)
    await message.answer(
        "Введите игровой <b>classname</b> предмета из DayZ (латиница, цифры, подчёркивание).\n"
        "Например: <code>AK74</code>",
        reply_markup=cancel_kb("admin_shop"),
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
            "❌ Некорректный classname. Используйте только <b>A-Z, a-z, 0-9, _</b> (без пробелов):",
            reply_markup=cancel_kb("admin_shop"),
            parse_mode="HTML",
        )
        return

    await state.update_data(game_classname=raw)
    await state.set_state(AdminShop.add_price)
    await message.answer("Введите цену товара в монетах (целое число больше 0):", reply_markup=cancel_kb("admin_shop"))


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
        await message.answer("❌ Цена должна быть положительным целым числом:", reply_markup=cancel_kb("admin_shop"))
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
        await safe_render_menu(target, text, kb)
        await target.answer()


@router.callback_query(AdminShop.add_photo, F.data == "ashop:add_skip_photo")
async def ashop_add_skip_photo(callback: CallbackQuery, db, state: FSMContext):
    await _finish_create_item(callback, db, state, photo_file_id=None)


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
# ---------- 1.4 СПИСОК ТОВАРОВ С ПАГИНАЦИЕЙ ----------

async def _render_ashop_list(callback: CallbackQuery, db, cat: str, page: int = 0) -> None:
    cats = await db.get_shop_categories()
    if not cats:
        await safe_render_menu(
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
        cat = str(cats["slug"])

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

    cat_tabs = [
        InlineKeyboardButton(
            text=("• " if str(c["slug"]) == cat else "") + str(c["title"]),
            callback_data=f"ashop:list:{c['slug']}:0",
        )
        for c in cats
    ]
    rows.extend(chunk_buttons(cat_tabs, cols=2))
    rows.append([
        InlineKeyboardButton(text="➕ Добавить сюда", callback_data=f"ashop_cat_quick:{cat}"),
        InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_shop"),
    ])

    text = f"📋 <b>Товары — {html.escape(cat_map[cat])}</b> (всего: {total_items})"
    if not all_cat_items:
        text += "\n\n<i>В этой категории пока нет товаров.</i>"
    else:
        text += "\nНажмите на товар для редактирования:"

    await safe_render_menu(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("ashop_cat_quick:"))
async def ashop_quick_add(callback: CallbackQuery, db, state: FSMContext):
    """Быстрое добавление товара сразу в текущую открытую категорию."""
    slug = callback.data.split(":", 1)
    fake_cb = callback.model_copy(update={"data": f"ashop_cat:{slug}"})
    await ashop_add_category(fake_cb, db, state)


@router.callback_query(F.data.startswith("ashop:list:"))
async def ashop_list(callback: CallbackQuery, db):
    parts = callback.data.split(":")
    cat = parts
    page = int(parts) if len(parts) > 3 else 0
    await _render_ashop_list(callback, db, cat, page)
    await callback.answer()


# ---------- 1.5 КАРТОЧКА РЕДАКТИРОВАНИЯ, ДУБЛИРОВАНИЕ И СМЕНА ГРУППЫ ----------

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
    await safe_render_menu(callback, text, kb, photo_id=item["photo_file_id"])
    return True


@router.callback_query(F.data.startswith("ashop:edit:"))
async def ashop_edit_item(callback: CallbackQuery, db):
    parts = callback.data.split(":")
    item_id = int(parts)
    page = int(parts) if len(parts) > 3 else 0
    if await _render_admin_item_detail(callback, db, item_id, page):
        await callback.answer()
@router.callback_query(F.data.startswith("ashop_dup:"))
async def ashop_duplicate_item(callback: CallbackQuery, db):
    _, item_id_str, page_str = callback.data.split(":", 2)
    new_id = await db.duplicate_shop_item(int(item_id_str))
    if not new_id:
        await callback.answer("Не удалось дублировать товар", show_alert=True)
        return
    await callback.answer("📋 Создана копия товара!", show_alert=True)
    await _render_admin_item_detail(callback, db, new_id, int(page_str))


@router.callback_query(F.data.startswith("ashop_move:"))
async def ashop_move_group_menu(callback: CallbackQuery, db):
    """Выбор новой категории/подкатегории для существующего товара."""
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
    rows = chunk_buttons(cat_btns, cols=2)
    rows.append([InlineKeyboardButton(text="⬅️ Назад к товару", callback_data=f"ashop:edit:{item_id}:{page}")])
    await safe_render_menu(
        callback,
        "📁 <b>Перенос товара</b>\nВыберите целевую категорию:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_mv_cat:"))
async def ashop_move_select_sub(callback: CallbackQuery, db):
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
    rows = chunk_buttons(sub_btns, cols=2)
    rows.append([InlineKeyboardButton(text="⏭ В корень категории (без группы)", callback_data=f"ashop_mv_set:{item_id}:{new_slug}:0:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ashop_move:{item_id}:{page}")])
    await safe_render_menu(
        callback,
        "Выберите подкатегорию назначения:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_mv_set:"))
async def ashop_move_apply(callback: CallbackQuery, db):
    _, item_id_str, new_slug, sub_id_str, page_str = callback.data.split(":", 4)
    item_id, sub_id, page = int(item_id_str), int(sub_id_str), int(page_str)

    await db.update_shop_item_field(item_id, "category", new_slug)
    await db.update_shop_item_field(item_id, "subcategory_id", sub_id if sub_id > 0 else None)
    await callback.answer("✅ Товар перемещён!", show_alert=True)
    await _render_admin_item_detail(callback, db, item_id, page)


@router.callback_query(F.data.startswith("ashop_fld:"))
async def ashop_field_prompt(callback: CallbackQuery, state: FSMContext):
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
    await safe_render_menu(callback, prompts[field], cancel_kb(f"ashop:edit:{item_id}:{page}"))
    await callback.answer()


@router.message(AdminShop.edit_value)
async def ashop_field_save(message: Message, db, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    field = str(data.get("edit_field", ""))
    item_id = int(data.get("edit_item_id", 0))
    page = int(data.get("edit_item_page", 0))

    if raw.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Редактирование отменено.")
        return

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
                reply_markup=cancel_kb(f"ashop:edit:{item_id}:{page}"),
            )
            return
    elif field == "game_classname":
        if not CLASSNAME_RE.match(raw):
            await message.answer(
                "❌ Некорректный classname (разрешены A-Z, a-z, 0-9, _):",
                reply_markup=cancel_kb(f"ashop:edit:{item_id}:{page}"),
            )
            return
        val = raw
    else:
        if not raw or len(raw) > 45:
            await message.answer(
                "❌ Название должно быть от 1 до 45 символов:",
                reply_markup=cancel_kb(f"ashop:edit:{item_id}:{page}"),
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
async def ashop_photo_prompt(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    item_id = int(parts)
    page = int(parts) if len(parts) > 2 else 0
    await state.update_data(edit_item_id=item_id, edit_item_page=page)
    await state.set_state(AdminShop.edit_photo)
    await safe_render_menu(
        callback,
        "🖼 Отправьте <b>новую фотографию</b> для этого товара:",
        cancel_kb(f"ashop:edit:{item_id}:{page}"),
    )
    await callback.answer()


@router.message(AdminShop.edit_photo)
async def ashop_photo_save(message: Message, db, state: FSMContext):
    data = await state.get_data()
    item_id = int(data.get("edit_item_id", 0))
    page = int(data.get("edit_item_page", 0))

    if (message.text or "").strip().lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Замена фото отменена.")
        return

    if not message.photo:
        await message.answer(
            "❌ Пожалуйста, отправьте именно <b>фотографию</b> (не документом/файлом):",
            reply_markup=cancel_kb(f"ashop:edit:{item_id}:{page}"),
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
async def ashop_delete_photo(callback: CallbackQuery, db):
    parts = callback.data.split(":")
    item_id = int(parts)
    page = int(parts) if len(parts) > 2 else 0
    await db.update_shop_item_field(item_id, "photo_file_id", None)
    await callback.answer("🧹 Фотография удалена с товара", show_alert=True)
    await _render_admin_item_detail(callback, db, item_id, page)


@router.callback_query(F.data.startswith("ashop_del_ask:"))
async def ashop_delete_ask(callback: CallbackQuery, db):
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
    await safe_render_menu(
        callback,
        f"⚠️ Вы уверены, что хотите удалить товар <b>«{html.escape(str(item['display_name']))}»</b>?",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ashop_del_yes:"))
async def ashop_delete_item(callback: CallbackQuery, db):
    _, item_id_str, cat, page_str = callback.data.split(":", 3)
    await db.delete_shop_item(int(item_id_str))
    await callback.answer("🗑 Товар удалён", show_alert=True)
    await _render_ashop_list(callback, db, cat, int(page_str))
# ==========================================
# 2. АДМИН-МЕНЮ ФИНАНСОВ (РУЧНОЙ ВВОД НИКА И ОПАСНЫЕ ЗОНЫ)
# ==========================================

@router.callback_query(F.data == "admin_finance")
async def admin_finance_menu(event: Message | CallbackQuery, state: FSMContext | None = None):
    if state:
        await state.set_state(None)

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
        await safe_render_menu(event, text, markup)
        await event.answer()


@router.callback_query(F.data == "afin:by_nick")
async def afin_ask_nick(callback: CallbackQuery, state: FSMContext):
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

    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (delta, target_id))
            await conn.execute(
                "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (NULL, ?, ?, ?)",
                (target_id, delta, "Корректировка баланса администратором"),
            )
            cur = await conn.execute("SELECT balance, telegram_id FROM users WHERE id = ?", (target_id,))
            row = await cur.fetchone()
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    new_bal = int(row["balance"] or 0) if row else 0
    target_tg_id = row["telegram_id"] if row else None
    sign = "+" if delta > 0 else ""

    if target_tg_id:
        await notify_balance_change(
            bot=message.bot,
            telegram_id=int(target_tg_id),
            delta=delta,
            reason="Корректировка баланса администратором",
            new_balance=new_bal,
        )

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
# 3. НАСТРОЙКИ ЭКОНОМИКИ (БОНУСЫ, КИЛЛЫ, ШТРАФЫ)
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
        await safe_render_menu(event, text, kb)


@router.callback_query(F.data == "aeco:menu")
async def aeco_menu(callback: CallbackQuery, db, state: FSMContext):
    if state:
        await state.set_state(None)

    await _render_eco_settings_menu(callback, db)
    await callback.answer()


@router.callback_query(F.data.startswith("aeco:edit:"))
async def aeco_edit_start(callback: CallbackQuery, db, state: FSMContext):
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
async def aeco_reset_defaults(callback: CallbackQuery, db):
    for k, default_val in DEFAULT_ECONOMY_CONFIG.items():
        await db.update_economy_setting(k, default_val)

    await callback.answer("🔄 Настройки экономики сброшены к значениям по умолчанию!", show_alert=True)
    await _render_eco_settings_menu(callback, db)
