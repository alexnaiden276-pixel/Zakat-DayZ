import contextlib
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from keyboards.menus import MenuBtn
from services.raid_scheduler import (
    DAY_NAMES_SHORT,
    RESTART_HOURS_MSK,
    apply_raid_state_now,
    format_days_list,
    is_raid_active_at,
    load_raid_config,
    save_raid_config,
)

router = Router()
logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))


def _is_admin(user_id: int, settings) -> bool:
    return user_id in getattr(settings, "admin_ids", set())


async def _build_raid_panel(db) -> tuple[str, InlineKeyboardMarkup]:
    cfg = await load_raid_config(db)
    now_msk = datetime.now(MSK)
    active_now = is_raid_active_at(now_msk, cfg)
    applied_raw = await db.get_setting("raid_applied_file_state")

    mode = cfg.get("mode", "auto")
    mode_titles = {
        "auto": "🤖 Авто (по расписанию)",
        "force_on": "🟢 Принудительно ВКЛ",
        "force_off": "🔴 Принудительно ВЫКЛ",
    }
    days = [int(d) for d in cfg.get("days", ())]
    start_h = int(cfg.get("start_hour", 18))
    end_h = int(cfg.get("end_hour", 22))
    announce = bool(cfg.get("announce_public", True))

    if applied_raw == "1":
        file_status = "🟢 Рейды ВКЛ (урон по стенам/палаткам есть)"
    elif applied_raw == "0":
        file_status = "🔴 Рейды ВЫКЛ (стены и палатки защищены)"
    else:
        file_status = "⚪ Ещё не синхронизирован"

    text = (
        "💥 <b>Управление рейд-таймом (DayZ)</b>\n\n"
        f"• <b>Режим работы:</b> {mode_titles.get(mode, mode)}\n"
        f"• <b>По графику сейчас:</b> {'🟢 Рейд-тайм' if active_now else '🔴 Мирный режим'}\n"
        f"• <b>Записано в cfggameplay.json:</b>\n  {file_status}\n\n"
        f"📅 <b>Дни рейдов:</b> {format_days_list(days)}\n"
        f"🕒 <b>Время (МСК):</b> с <code>{start_h:02d}:00</code> до <code>{end_h:02d}:00</code>\n"
        f"📢 <b>Анонс в общий чат:</b> {'Включён ✅' if announce else 'Выключен ❌'}\n\n"
        "<i>ℹ️ Бот автоматически обновляет cfggameplay.json за 15 минут до ближайшего планового рестарта.</i>"
    )

    day_buttons = []
    for d_idx, d_name in enumerate(DAY_NAMES_SHORT):
        mark = "✅" if d_idx in days else "⬜"
        day_buttons.append(
            InlineKeyboardButton(text=f"{mark}{d_name}", callback_data=f"raid:day:{d_idx}")
        )

    rows = [
        day_buttons[:4],
        day_buttons[4:],
        [
            InlineKeyboardButton(text=f"🟢 Старт: {start_h:02d}:00", callback_data="raid:pick_start"),
            InlineKeyboardButton(text=f"🔴 Конец: {end_h:02d}:00", callback_data="raid:pick_end"),
        ],
        [
            InlineKeyboardButton(text="🤖 Авто", callback_data="raid:mode:auto"),
            InlineKeyboardButton(text="🟢 Вкл", callback_data="raid:mode:force_on"),
            InlineKeyboardButton(text="🔴 Выкл", callback_data="raid:mode:force_off"),
        ],
        [
            InlineKeyboardButton(
                text=f"📢 Анонс: {'ВКЛ' if announce else 'ВЫКЛ'}",
                callback_data="raid:toggle_announce",
            ),
            InlineKeyboardButton(text="🔄 Записать в файл сейчас", callback_data="raid:apply_now"),
        ],
    ]

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _build_hours_keyboard(target: str, current_hour: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора чётного часа рестарта (00:00 - 22:00 МСК)."""
    rows = []
    pair: list[InlineKeyboardButton] = []
    for h in RESTART_HOURS_MSK:
        mark = "✅ " if h == current_hour else ""
        pair.append(
            InlineKeyboardButton(text=f"{mark}{h:02d}:00", callback_data=f"raid:set_{target}:{h}")
        )
        if len(pair) == 3:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="⬅️ Назад к настройкам рейд-тайма", callback_data="raid:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == MenuBtn.ADMIN_RAID)
@router.callback_query(F.data == "raid:panel")
async def raid_admin_panel(event: Message | CallbackQuery, db, settings):
    if not event.from_user or not _is_admin(event.from_user.id, settings):
        return

    text, markup = await _build_raid_panel(db)
    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
