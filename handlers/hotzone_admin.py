import html
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.db import Database
from services.hotzone_service import HotzoneService, format_hotzone_announcement

router = Router(name="hotzone_admin")

ALLOWED_HZ_PARAMS = {
    "eco_hotzone_salary_mult",
    "eco_hotzone_kill_mult",
    "eco_hotzone_duration_min",
    "eco_hotzone_announce_min",
    "eco_hotzone_min_online",
}


class AddHotzoneFSM(StatesGroup):
    waiting_name = State()
    waiting_coords = State()
    waiting_radius = State()


class EditHzParamFSM(StatesGroup):
    waiting_value = State()
    waiting_hours = State()


def _is_admin(tg_id: int | None, user_row, settings) -> bool:
    if not tg_id:
        return False
    admin_ids = getattr(settings, "admin_ids", None) or getattr(settings, "admins", None) or []
    if isinstance(admin_ids, (list, tuple, set)) and tg_id in admin_ids:
        return True
    single_admin = getattr(settings, "admin_id", None)
    if tg_id == single_admin:
        return True
    return bool(user_row and user_row["role"] == "admin")


def _build_hz_main_kb(has_active: bool, auto_enabled: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_active:
        rows.append([InlineKeyboardButton(text="🛑 Остановить текущий ивент", callback_data="hzadm:stop")])
    else:
        rows.append([InlineKeyboardButton(text="▶️ Запустить Опасную зону", callback_data="hzadm:launch_list")])

    auto_status = "ВКЛ ✅" if auto_enabled else "ВЫКЛ ❌"
    rows.append([InlineKeyboardButton(text=f"🤖 Автозапуск (Рандом): {auto_status}", callback_data="hzadm:toggle_auto")])
    rows.append([InlineKeyboardButton(text="📍 Локации (Пресеты)", callback_data="hzadm:presets")])
    rows.append([InlineKeyboardButton(text="⚙️ Настроить множители и условия", callback_data="hzadm:edit_mult")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в финансы", callback_data="admin_finance")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_presets_kb(presets: list, for_launch: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for p in presets:
        if for_launch:
            rows.append([
                InlineKeyboardButton(
                    text=f"☢️ {p['name']} ({int(p['radius'])} м)",
                    callback_data=f"hzadm:pick_mode:{p['id']}",
                )
            ])
        else:
            mark = "✅" if p["is_enabled"] else "❌"
            rows.append([
                InlineKeyboardButton(
                    text=f"{mark} {p['name']} [{int(p['x'])}, {int(p['z'])}, {int(p['radius'])}м]",
                    callback_data=f"hzadm:toggle:{p['id']}",
                ),
                InlineKeyboardButton(text="🗑", callback_data=f"hzadm:del:{p['id']}"),
            ])

    if not for_launch:
        rows.append([InlineKeyboardButton(text="➕ Добавить локацию", callback_data="hzadm:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="hzadm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_hz_dashboard(db: Database) -> tuple[str, InlineKeyboardMarkup]:
    eco = await db.get_economy_config()
    current = await db.get_current_hotzone_event()
    prime_hours = await db.get_hotzone_hours()

    auto_enabled = int(eco.get("eco_hotzone_enabled", 1)) == 1
    sal_mult = int(eco.get("eco_hotzone_salary_mult", 3))
    kill_mult = int(eco.get("eco_hotzone_kill_mult", 2))
    dur_min = int(eco.get("eco_hotzone_duration_min", 60))
    ann_min = int(eco.get("eco_hotzone_announce_min", 15))
    min_online = int(eco.get("eco_hotzone_min_online", 4))

    lines = ["☢️ <b>Управление Опасными Зонами (Кровавый час)</b>\n"]

    if current:
        starts_at = datetime.fromisoformat(current["starts_at"]).strftime("%H:%M")
        ends_at = datetime.fromisoformat(current["ends_at"]).strftime("%H:%M")
        state_str = "⏳ <b>Ожидает старта (Анонс)</b>" if current["status"] == "announced" else "🔥 <b>АКТИВНА СЕЙЧАС</b>"
        lines.append(f"Статус: {state_str}")
        lines.append(f"📍 Локация: <b>{html.escape(str(current['name']))}</b> (радиус {int(current['radius'])} м)")
        lines.append(f"🕒 Время: с <b>{starts_at}</b> до <b>{ends_at} МСК</b>\n")
    else:
        lines.append("Статус: 💤 <b>Нет активного ивента</b>\n")

    hours_str = ", ".join(f"{h:02d}:00" for h in prime_hours)
    boosted_salary = int(eco["eco_online_reward"]) * sal_mult
    online_cond = f"от {min_online} чел." if min_online > 0 else "любой (0)"

    lines.append(f"💰 Онлайн в зоне: <b>x{sal_mult}</b> ({boosted_salary} 🪙 вместо {eco['eco_online_reward']})")
    lines.append(f"🔫 Награда за килл в зоне: <b>x{kill_mult}</b>")
    lines.append(f"⏱ Длительность: <b>{dur_min} мин</b> (анонс за <b>{ann_min} мин</b>)")
    lines.append(f"📅 Часы автозапуска (МСК): <b>{hours_str}</b>")
    lines.append(f"👥 Условие онлайна: <b>{online_cond}</b>")

    return "\n".join(lines), _build_hz_main_kb(has_active=bool(current), auto_enabled=auto_enabled)
@router.callback_query(F.data == "hzadm:menu")
async def cb_hz_menu(callback: CallbackQuery, db: Database, settings, state: FSMContext) -> None:
    if state:
        await state.set_state(None)
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    text, kb = await _render_hz_dashboard(db)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "hzadm:toggle_auto")
async def cb_toggle_auto(callback: CallbackQuery, db: Database, settings) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    eco = await db.get_economy_config()
    new_val = 0 if int(eco.get("eco_hotzone_enabled", 1)) == 1 else 1
    await db.update_economy_setting("eco_hotzone_enabled", new_val)

    text, kb = await _render_hz_dashboard(db)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Режим автозапуска обновлён")


@router.callback_query(F.data == "hzadm:stop")
async def cb_stop_hz(callback: CallbackQuery, db: Database, settings, hotzone_service: HotzoneService) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    await hotzone_service.stop_event()
    text, kb = await _render_hz_dashboard(db)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("🛑 Опасная зона остановлена", show_alert=True)


# --- РУЧНОЙ ЗАПУСК ---

@router.callback_query(F.data == "hzadm:launch_list")
async def cb_launch_list(callback: CallbackQuery, db: Database, settings) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    presets = await db.get_hotzone_presets(only_enabled=False)
    if not presets:
        await callback.answer("Сначала добавьте хотя бы одну локацию!", show_alert=True)
        return

    await callback.message.edit_text(
        "☢️ <b>Выберите локацию для запуска Опасной зоны:</b>",
        reply_markup=_build_presets_kb(presets, for_launch=True),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hzadm:pick_mode:"))
async def cb_pick_mode(callback: CallbackQuery, db: Database, settings) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    preset_id = int(callback.data.split(":")[2])
    preset = await db.get_hotzone_preset(preset_id)
    if not preset:
        await callback.answer("Локация не найдена", show_alert=True)
        return

    eco = await db.get_economy_config()
    ann_min = int(eco.get("eco_hotzone_announce_min", 15))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏳ С анонсом за {ann_min} мин", callback_data=f"hzadm:start_ann:{preset_id}")],
        [InlineKeyboardButton(text="🔥 Запустить прямо сейчас", callback_data=f"hzadm:start_now:{preset_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="hzadm:launch_list")],
    ])
    await callback.message.edit_text(
        f"☢️ Локация: <b>{html.escape(str(preset['name']))}</b>\nВыберите режим запуска:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith(("hzadm:start_ann:", "hzadm:start_now:")))
async def cb_do_launch(
    callback: CallbackQuery,
    db: Database,
    settings,
    hotzone_service: HotzoneService,
) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    _, mode, id_str = callback.data.split(":", 2)
    preset_id = int(id_str)
    is_now = mode == "start_now"

    eco = await db.get_economy_config()
    ann_min = 0 if is_now else int(eco.get("eco_hotzone_announce_min", 15))
    dur_min = int(eco.get("eco_hotzone_duration_min", 60))
    sal_mult = float(eco.get("eco_hotzone_salary_mult", 3))
    kill_mult = float(eco.get("eco_hotzone_kill_mult", 2))

    event = await hotzone_service.launch_event(
        preset_id=preset_id,
        announce_minutes=ann_min,
        duration_minutes=dur_min,
        salary_mult=sal_mult,
        kill_mult=kill_mult,
    )
    if not event:
        await callback.answer("Ошибка запуска зоны", show_alert=True)
        return

    public_text = format_hotzone_announcement(
        event=event,
        base_online_reward=int(eco.get("eco_online_reward", 60)),
        is_immediate=is_now,
    )

    await callback.bot.send_message(
        chat_id=settings.hotzone_chat_id,
        message_thread_id=settings.hotzone_thread_id,
        text=public_text,
        parse_mode="HTML",
    )

    text, kb = await _render_hz_dashboard(db)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("✅ Ивент запущен и анонсирован в тему!")


# --- УПРАВЛЕНИЕ ЛОКАЦИЯМИ (ПРЕСЕТАМИ) ---

@router.callback_query(F.data == "hzadm:presets")
async def cb_presets(callback: CallbackQuery, db: Database, settings) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    presets = await db.get_hotzone_presets()
    await callback.message.edit_text(
        "📍 <b>Локации для Опасных зон:</b>\n"
        "<i>Нажмите на локацию, чтобы включить (✅) или выключить (❌) её из рандомного запуска.</i>",
        reply_markup=_build_presets_kb(presets, for_launch=False),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("hzadm:toggle:"))
async def cb_toggle_preset(callback: CallbackQuery, db: Database, settings) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    preset_id = int(callback.data.split(":")[2])
    await db.toggle_hotzone_preset(preset_id)
    presets = await db.get_hotzone_presets()
    await callback.message.edit_reply_markup(reply_markup=_build_presets_kb(presets, for_launch=False))
    await callback.answer()


@router.callback_query(F.data.startswith("hzadm:del:"))
async def cb_del_preset(callback: CallbackQuery, db: Database, settings) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    preset_id = int(callback.data.split(":")[2])
    await db.delete_hotzone_preset(preset_id)
    presets = await db.get_hotzone_presets()
    await callback.message.edit_reply_markup(reply_markup=_build_presets_kb(presets, for_launch=False))
    await callback.answer("🗑 Локация удалена")
@router.callback_query(F.data == "hzadm:add")
async def cb_add_preset_start(callback: CallbackQuery, db: Database, settings, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    await state.set_state(AddHotzoneFSM.waiting_name)
    await callback.message.answer(
        "📝 Введите <b>название новой опасной зоны</b> (например: <code>Аэропорт Красностав</code>)\n"
        "или <code>/cancel</code> для отмены:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AddHotzoneFSM.waiting_name)
async def fsm_preset_name(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление локации отменено.")
        return

    if len(raw) < 2 or len(raw) > 50:
        await message.answer("❌ Название должно быть от 2 до 50 символов. Попробуйте снова:")
        return

    await state.update_data(name=raw)
    await state.set_state(AddHotzoneFSM.waiting_coords)
    await message.answer(
        "🧭 Введите координаты центра зоны <b>X</b> и <b>Z</b> с карты iZurvive через пробел\n"
        "(например: <code>4500 10200</code>):",
        parse_mode="HTML",
    )


@router.message(AddHotzoneFSM.waiting_coords)
async def fsm_preset_coords(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление локации отменено.")
        return

    parts = raw.replace(",", ".").split()
    if len(parts) != 2:
        await message.answer(
            "❌ Нужно ввести ровно два числа: <b>X</b> и <b>Z</b> (например: <code>4500 10200</code>):",
            parse_mode="HTML",
        )
        return

    try:
        x, z = float(parts[0]), float(parts[1])
    except ValueError:
        await message.answer("❌ Координаты должны быть числами. Попробуйте снова:")
        return

    await state.update_data(x=x, z=z)
    await state.set_state(AddHotzoneFSM.waiting_radius)
    await message.answer(
        "📏 Введите <b>радиус зоны в метрах</b> (рекомендуется <code>300</code>–<code>500</code>):",
        parse_mode="HTML",
    )


@router.message(AddHotzoneFSM.waiting_radius)
async def fsm_preset_radius(message: Message, state: FSMContext, db: Database) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление локации отменено.")
        return

    try:
        radius = float(raw)
        if not (50 <= radius <= 5000):
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 50 до 5000 метров:")
        return

    data = await state.get_data()
    await state.clear()
    await db.add_hotzone_preset(data["name"], data["x"], data["z"], radius)

    presets = await db.get_hotzone_presets()
    await message.answer(
        f"✅ Локация <b>{html.escape(data['name'])}</b> ({int(data['x'])}, {int(data['z'])}, R={int(radius)}м) добавлена!",
        reply_markup=_build_presets_kb(presets, for_launch=False),
        parse_mode="HTML",
    )


# --- НАСТРОЙКА МНОЖИТЕЛЕЙ, ВРЕМЕНИ И УСЛОВИЙ ---

@router.callback_query(F.data == "hzadm:edit_mult")
async def cb_edit_mult_menu(callback: CallbackQuery, db: Database, settings) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    eco = await db.get_economy_config()
    prime_hours = await db.get_hotzone_hours()
    hours_str = ", ".join(f"{h:02d}:00" for h in prime_hours)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💰 Множитель онлайна (x{eco['eco_hotzone_salary_mult']})",
            callback_data="hzadm:set_param:eco_hotzone_salary_mult",
        )],
        [InlineKeyboardButton(
            text=f"🔫 Множитель киллов (x{eco['eco_hotzone_kill_mult']})",
            callback_data="hzadm:set_param:eco_hotzone_kill_mult",
        )],
        [InlineKeyboardButton(
            text=f"⏱ Длительность ({eco['eco_hotzone_duration_min']} мин)",
            callback_data="hzadm:set_param:eco_hotzone_duration_min",
        )],
        [InlineKeyboardButton(
            text=f"⏳ Анонс до старта ({eco['eco_hotzone_announce_min']} мин)",
            callback_data="hzadm:set_param:eco_hotzone_announce_min",
        )],
        [InlineKeyboardButton(
            text=f"👥 Мин. онлайн для запуска ({eco['eco_hotzone_min_online']} чел.)",
            callback_data="hzadm:set_param:eco_hotzone_min_online",
        )],
        [InlineKeyboardButton(
            text=f"📅 Часы автозапуска ({hours_str})",
            callback_data="hzadm:edit_hours",
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="hzadm:menu")],
    ])
    await callback.message.edit_text(
        "⚙️ <b>Настройка множителей и условий ивента:</b>",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "hzadm:edit_hours")
async def cb_edit_hours_start(callback: CallbackQuery, db: Database, settings, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    await state.set_state(EditHzParamFSM.waiting_hours)
    await callback.message.answer(
        "📅 Введите часы запуска ивента по МСК (от 0 до 23) через пробел.\n"
        "Например: <code>16 19 22</code> (ивент стартует в 16:00, 19:00 и 22:00, а анонс выйдет за 15 мин до старта).\n\n"
        "Для отмены введите <code>/cancel</code>:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(EditHzParamFSM.waiting_hours)
async def fsm_save_hours(message: Message, state: FSMContext, db: Database) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Изменение расписания отменено.")
        return

    parts = raw.replace(",", " ").split()
    hours = []
    for p in parts:
        if not p.isdigit() or not (0 <= int(p) <= 23):
            await message.answer(
                "❌ Каждый час должен быть целым числом от 0 до 23 (например: <code>16 19 22</code>):",
                parse_mode="HTML",
            )
            return
        hours.append(int(p))

    if not hours:
        await message.answer("❌ Укажите хотя бы один час (например: <code>19</code>):", parse_mode="HTML")
        return

    await state.clear()
    await db.set_hotzone_hours(hours)

    text, kb = await _render_hz_dashboard(db)
    await message.answer(f"✅ Расписание обновлено!\n\n{text}", reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("hzadm:set_param:"))
async def cb_ask_param_val(callback: CallbackQuery, db: Database, settings, state: FSMContext) -> None:
    user = await db.get_user(callback.from_user.id)
    if not _is_admin(callback.from_user.id, user, settings):
        return

    key = callback.data.split(":")[2]
    if key not in ALLOWED_HZ_PARAMS:
        return

    await state.set_state(EditHzParamFSM.waiting_value)
    await state.update_data(param_key=key)

    hint = (
        "целое число от 0 до 100 (0 = без ограничения)"
        if key == "eco_hotzone_min_online"
        else "положительное целое число (от 1 до 1000)"
    )
    await callback.message.answer(
        f"🔢 Введите {hint} или <code>/cancel</code> для отмены:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(EditHzParamFSM.waiting_value)
async def fsm_save_param_val(message: Message, state: FSMContext, db: Database) -> None:
    raw = (message.text or "").strip()
    if raw.lower() == "/cancel":
        await state.clear()
        await message.answer("❌ Изменение параметра отменено.")
        return

    data = await state.get_data()
    key = data.get("param_key")
    if key not in ALLOWED_HZ_PARAMS:
        await state.clear()
        return

    try:
        val = int(raw)
        if key == "eco_hotzone_min_online":
            if val < 0 or val > 100:
                raise ValueError
        else:
            if val <= 0 or val > 1000:
                raise ValueError
    except ValueError:
        await message.answer("❌ Некорректное число. Попробуйте ещё раз:")
        return

    await state.clear()
    await db.update_economy_setting(key, val)

    text, kb = await _render_hz_dashboard(db)
    await message.answer(f"✅ Параметр обновлён!\n\n{text}", reply_markup=kb, parse_mode="HTML")
