import contextlib
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from keyboards.menus import MenuBtn
from services.wipe_scheduler import (
    execute_wipe_and_notify,
    format_ts_msk,
    load_wipe_config,
    save_wipe_config,
)

router = Router()
logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))


class WipeStates(StatesGroup):
    waiting_full_date = State()
    waiting_car_date = State()


def _is_admin(user_id: int, settings) -> bool:
    return user_id in getattr(settings, "admin_ids", set())


async def _build_wipe_panel(db) -> tuple[str, InlineKeyboardMarkup]:
    cfg = await load_wipe_config(db)

    full_on = bool(cfg.get("full_enabled", False))
    full_days = int(cfg.get("full_interval_days", 30))
    full_next = int(cfg.get("full_next_ts") or 0)

    car_on = bool(cfg.get("car_enabled", False))
    car_days = int(cfg.get("car_interval_days", 7))
    car_next = int(cfg.get("car_next_ts") or 0)

    announce = bool(cfg.get("announce_public", True))

    text = (
        "🔥 <b>Управление вайпами сервера (DayZ)</b>\n\n"
        f"💥 <b>Полный вайп сервера:</b> {'🟢 ВКЛ' if full_on else '🔴 ВЫКЛ'}\n"
        f"  • Интервал: каждые <code>{full_days} дн.</code>\n"
        f"  • Ближайший: <code>{format_ts_msk(full_next) if full_on else 'отключено'}</code>\n\n"
        f"🚗 <b>Вайп автомобилей:</b> {'🟢 ВКЛ' if car_on else '🔴 ВЫКЛ'}\n"
        f"  • Интервал: каждые <code>{car_days} дн.</code>\n"
        f"  • Ближайший: <code>{format_ts_msk(car_next) if car_on else 'отключено'}</code>\n\n"
        f"📢 <b>Анонс в общий чат:</b> {'Включён ✅' if announce else 'Выключен ❌'}\n\n"
        "<i>⚠️ Во время вайпа бот сам остановит сервер, очистит нужные файлы на FTP и запустит его снова.</i>"
    )

    rows = [
        [
            InlineKeyboardButton(
                text=f"💥 Полный вайп: {'ВКЛ' if full_on else 'ВЫКЛ'}",
                callback_data="wipe:toggle:full",
            ),
            InlineKeyboardButton(text=f"⏱ Интервал ({full_days}д)", callback_data="wipe:int_menu:full"),
        ],
        [
            InlineKeyboardButton(text="📅 Задать дату полного вайпа", callback_data="wipe:set_date:full"),
        ],
        [
            InlineKeyboardButton(
                text=f"🚗 Вайп авто: {'ВКЛ' if car_on else 'ВЫКЛ'}",
                callback_data="wipe:toggle:cars",
            ),
            InlineKeyboardButton(text=f"⏱ Интервал ({car_days}д)", callback_data="wipe:int_menu:cars"),
        ],
        [
            InlineKeyboardButton(text="📅 Задать дату вайпа авто", callback_data="wipe:set_date:cars"),
        ],
        [
            InlineKeyboardButton(
                text=f"📢 Анонс: {'ВКЛ' if announce else 'ВЫКЛ'}",
                callback_data="wipe:toggle_announce",
            ),
        ],
        [
            InlineKeyboardButton(text="⚠️ Полный вайп СЕЙЧАС", callback_data="wipe:confirm_now:full"),
            InlineKeyboardButton(text="⚠️ Вайп авто СЕЙЧАС", callback_data="wipe:confirm_now:cars"),
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == MenuBtn.ADMIN_WIPE)
@router.callback_query(F.data == "wipe:panel")
async def wipe_admin_panel(event: Message | CallbackQuery, db, settings, state: FSMContext):
    if not event.from_user or not _is_admin(event.from_user.id, settings):
        return
    await state.clear()
    text, markup = await _build_wipe_panel(db)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()


@router.callback_query(F.data.startswith("wipe:toggle:"))
async def wipe_toggle(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, target = callback.data.split(":", 2)
    cfg = await load_wipe_config(db)
    now_ts = int(datetime.now(MSK).timestamp())

    if target == "full":
        new_val = not bool(cfg.get("full_enabled", False))
        cfg["full_enabled"] = new_val
        if new_val and int(cfg.get("full_next_ts") or 0) <= now_ts:
            cfg["full_next_ts"] = now_ts + int(cfg.get("full_interval_days", 30)) * 86400
            cfg["full_warned_1h"] = False
    elif target == "cars":
        new_val = not bool(cfg.get("car_enabled", False))
        cfg["car_enabled"] = new_val
        if new_val and int(cfg.get("car_next_ts") or 0) <= now_ts:
            cfg["car_next_ts"] = now_ts + int(cfg.get("car_interval_days", 7)) * 86400
            cfg["car_warned_1h"] = False

    await save_wipe_config(db, cfg)
    text, markup = await _build_wipe_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "wipe:toggle_announce")
async def wipe_toggle_announce(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    cfg = await load_wipe_config(db)
    cfg["announce_public"] = not bool(cfg.get("announce_public", True))
    await save_wipe_config(db, cfg)
    text, markup = await _build_wipe_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()
@router.callback_query(F.data.startswith("wipe:int_menu:"))
async def wipe_interval_menu(callback: CallbackQuery, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, target = callback.data.split(":", 2)
    options = (14, 21, 30, 45, 60, 90) if target == "full" else (3, 5, 7, 10, 14, 30)
    buttons = [
        InlineKeyboardButton(text=f"{d} дн.", callback_data=f"wipe:set_int:{target}:{d}")
        for d in options
    ]
    rows = [buttons[:3], buttons[3:], [InlineKeyboardButton(text="⬅️ Назад", callback_data="wipe:panel")]]
    await callback.message.edit_text(
        "⏱ <b>Выберите периодичность автоматического вайпа:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wipe:set_int:"))
async def wipe_set_interval(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, target, days_str = callback.data.split(":", 3)
    days = int(days_str)
    cfg = await load_wipe_config(db)
    now_ts = int(datetime.now(MSK).timestamp())

    if target == "full":
        cfg["full_interval_days"] = days
        if cfg.get("full_enabled"):
            cfg["full_next_ts"] = now_ts + days * 86400
            cfg["full_warned_1h"] = False
    else:
        cfg["car_interval_days"] = days
        if cfg.get("car_enabled"):
            cfg["car_next_ts"] = now_ts + days * 86400
            cfg["car_warned_1h"] = False

    await save_wipe_config(db, cfg)
    text, markup = await _build_wipe_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer(f"Интервал установлен: {days} дн.")


@router.callback_query(F.data.startswith("wipe:set_date:"))
async def wipe_ask_date(callback: CallbackQuery, settings, state: FSMContext):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, target = callback.data.split(":", 2)
    await state.set_state(WipeStates.waiting_full_date if target == "full" else WipeStates.waiting_car_date)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="wipe:panel")]]
    )
    await callback.message.edit_text(
        "📅 Введите точную дату и время вайпа по <b>МСК</b> в формате:\n"
        "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
        "<i>Например: 01.12.2026 18:00</i>",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(WipeStates.waiting_full_date)
@router.message(WipeStates.waiting_car_date)
async def wipe_save_date(message: Message, db, settings, state: FSMContext):
    if not message.from_user or not _is_admin(message.from_user.id, settings):
        return
    raw_text = (message.text or "").strip()
    try:
        dt_naive = datetime.strptime(raw_text, "%d.%m.%Y %H:%M")
        dt_msk = dt_naive.replace(tzinfo=MSK)
        if dt_msk <= datetime.now(MSK):
            await message.answer("⚠️ Указанная дата уже в прошлом. Введите будущую дату и время:")
            return
    except ValueError:
        await message.answer(
            "⚠️ Неверный формат. Введите дату строго как <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>:",
            parse_mode="HTML",
        )
        return

    current_state = await state.get_state()
    cfg = await load_wipe_config(db)
    if current_state == WipeStates.waiting_full_date.state:
        cfg["full_next_ts"] = int(dt_msk.timestamp())
        cfg["full_enabled"] = True
        cfg["full_warned_1h"] = False
    else:
        cfg["car_next_ts"] = int(dt_msk.timestamp())
        cfg["car_enabled"] = True
        cfg["car_warned_1h"] = False

    await save_wipe_config(db, cfg)
    await state.clear()
    text, markup = await _build_wipe_panel(db)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("wipe:confirm_now:"))
async def wipe_confirm_now(callback: CallbackQuery, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, target = callback.data.split(":", 2)
    title = "ПОЛНЫЙ ВАЙП СЕРВЕРА" if target == "full" else "ВАЙП АВТОМОБИЛЕЙ"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🔥 ДА, ВЫПОЛНИТЬ {title}", callback_data=f"wipe:run_now:{target}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="wipe:panel")],
        ]
    )
    await callback.message.edit_text(
        f"⚠️ <b>Подтверждение действия</b>\n\n"
        f"Вы уверены, что хотите запустить <b>{title}</b> прямо сейчас?\n"
        "Сервер будет остановлен, данные удалены безвозвратно!",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("wipe:run_now:"))
async def wipe_run_now(callback: CallbackQuery, db, settings, bot: Bot, nitrado_api):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, target = callback.data.split(":", 2)
    await callback.answer("⏳ Запускаю процедуру вайпа...")
    cfg = await load_wipe_config(db)

    await callback.message.edit_text(
        "⏳ <b>Выполняется вайп сервера...</b>\n"
        "Ожидайте завершения (остановка сервера → очистка FTP → запуск).",
        parse_mode="HTML",
    )

    await execute_wipe_and_notify(
        bot=bot,
        db=db,
        nitrado=nitrado_api,
        wipe_type=target,
        cfg=cfg,
        admin_chat_id=callback.from_user.id,
        public_chat_id=settings.target_chat_id,
        public_thread_id=settings.target_thread_id,
        is_manual=True,
    )

