@router.callback_query(F.data.startswith("raid:day:"))
async def raid_toggle_day(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return

    _, _, day_str = callback.data.split(":", 2)
    day_idx = int(day_str)
    cfg = await load_raid_config(db)
    days = set(int(d) for d in cfg.get("days", ()))
    if day_idx in days:
        days.remove(day_idx)
    else:
        days.add(day_idx)
    cfg["days"] = sorted(days)
    await save_raid_config(db, cfg)

    text, markup = await _build_raid_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "raid:pick_start")
async def raid_pick_start(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    cfg = await load_raid_config(db)
    markup = _build_hours_keyboard("start", int(cfg.get("start_hour", 18)))
    await callback.message.edit_text(
        "🟢 <b>Выберите время НАЧАЛА рейд-тайма (по МСК):</b>\n"
        "<i>Время привязано к плановым чётным рестартам сервера.</i>",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "raid:pick_end")
async def raid_pick_end(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    cfg = await load_raid_config(db)
    markup = _build_hours_keyboard("end", int(cfg.get("end_hour", 22)))
    await callback.message.edit_text(
        "🔴 <b>Выберите время ОКОНЧАНИЯ рейд-тайма (по МСК):</b>\n"
        "<i>Время привязано к плановым чётным рестартам сервера.</i>",
        reply_markup=markup,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("raid:set_start:"))
async def raid_set_start(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, hour_str = callback.data.split(":", 2)
    hour = int(hour_str)
    cfg = await load_raid_config(db)
    cfg["start_hour"] = hour
    await save_raid_config(db, cfg)

    text, markup = await _build_raid_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer(f"Старт рейд-тайма установлен на {hour:02d}:00 МСК")


@router.callback_query(F.data.startswith("raid:set_end:"))
async def raid_set_end(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, hour_str = callback.data.split(":", 2)
    hour = int(hour_str)
    cfg = await load_raid_config(db)
    cfg["end_hour"] = hour
    await save_raid_config(db, cfg)

    text, markup = await _build_raid_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer(f"Окончание рейд-тайма установлено на {hour:02d}:00 МСК")


@router.callback_query(F.data.startswith("raid:mode:"))
async def raid_set_mode(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    _, _, new_mode = callback.data.split(":", 2)
    if new_mode not in ("auto", "force_on", "force_off"):
        return

    cfg = await load_raid_config(db)
    cfg["mode"] = new_mode
    await save_raid_config(db, cfg)

    text, markup = await _build_raid_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer("Режим рейд-тайма обновлён.")


@router.callback_query(F.data == "raid:toggle_announce")
async def raid_toggle_announce(callback: CallbackQuery, db, settings):
    if not _is_admin(callback.from_user.id, settings):
        return
    cfg = await load_raid_config(db)
    cfg["announce_public"] = not bool(cfg.get("announce_public", True))
    await save_raid_config(db, cfg)

    text, markup = await _build_raid_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "raid:apply_now")
async def raid_apply_now(callback: CallbackQuery, db, settings, bot: Bot, nitrado_api):
    if not _is_admin(callback.from_user.id, settings):
        return

    await callback.answer("⏳ Записываем настройки в cfggameplay.json...")
    cfg = await load_raid_config(db)
    target_state = is_raid_active_at(datetime.now(MSK), cfg)

    ok = await apply_raid_state_now(
        bot=bot,
        db=db,
        nitrado=nitrado_api,
        target_state=target_state,
        cfg=cfg,
        admin_chat_id=None,
        public_chat_id=settings.raid_chat_id,
        public_thread_id=settings.raid_thread_id,
        reason="ручная синхронизация администратором",
    )

    text, markup = await _build_raid_panel(db)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")

    if ok:
        await callback.message.answer(
            "✅ Настройки успешно записаны в <code>cfggameplay.json</code>! "
            "Они вступят в силу после ближайшего рестарта сервера.",
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            "❌ Ошибка при записи <code>cfggameplay.json</code> на FTP Nitrado. Проверьте логи.",
            parse_mode="HTML",
        )
