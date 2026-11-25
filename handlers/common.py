import contextlib
import html
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from keyboards.menus import back_button
from services.leaderboard import build_leaderboard_text, publish_leaderboard

router = Router()


@router.message(Command("post_cabinet"))
async def cmd_post_cabinet(message: Message, bot: Bot, settings):
    """Админ-команда для публикации и закрепления кнопки личного кабинета в теме группы."""
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    if settings.cabinet_thread_id is None:
        await message.answer(
            "❌ В файле <code>.env</code> не задан параметр <code>CABINET_THREAD_ID</code>.",
            parse_mode="HTML",
        )
        return

    bot_info = await bot.get_me()
    if not bot_info.username:
        await message.answer(
            "❌ У вашего бота не задан username! Зайдите в @BotFather -> /mybots -> выберите бота -> Edit Bot -> Edit Username.",
        )
        return

    clean_username = bot_info.username.lstrip("@").strip()
    bot_url = f"https://t.me{clean_username}?start=cabinet"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Открыть личный кабинет", url=bot_url)]
        ]
    )

    msg = await bot.send_message(
        chat_id=settings.cabinet_chat_id,
        message_thread_id=settings.cabinet_thread_id,
        text=(
            "🔐 <b>Личный кабинет игрока</b>\n\n"
            "Нажмите на кнопку ниже, чтобы перейти в личные сообщения с ботом, "
            "привязать игровой никнейм, проверить баланс и статистику."
        ),
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    with contextlib.suppress(TelegramBadRequest):
        await bot.pin_chat_message(
            chat_id=settings.cabinet_chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )

    await message.answer(
        f"✅ Кнопка личного кабинета опубликована и закреплена в чате "
        f"<code>{settings.cabinet_chat_id}</code> (тема: <code>{settings.cabinet_thread_id}</code>).\n"
        f"Ссылка кнопки: <code>{bot_url}</code>",
        parse_mode="HTML",
    )


@router.message(Command("top_now"))
async def cmd_publish_top_now(message: Message, bot: Bot, db, settings):
    """Админ-команда для немедленной публикации/обновления таблицы лидеров в теме группы."""
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    await publish_leaderboard(
        bot=bot,
        db=db,
        chat_id=settings.leaderboard_chat_id,
        thread_id=settings.leaderboard_thread_id,
        force_new_message=False,
    )
    await message.answer(
        f"✅ Таблица лидеров опубликована/обновлена в чате <code>{settings.leaderboard_chat_id}</code> "
        f"(тема: <code>{settings.leaderboard_thread_id or 'основной чат'}</code>).",
        parse_mode="HTML",
    )


@router.message(Command("top"))
async def cmd_view_top(message: Message, db):
    """Показывает актуальную таблицу лидеров игроку в ответ на команду /top."""
    text, _ = await build_leaderboard_text(db)
    await message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "rules")
async def rules(callback: CallbackQuery, settings):
    text = settings.rules_text
    reply_markup = back_button()

    if callback.id == "0":
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user:
        if callback.id != "0":
            await callback.answer("Профиль не найден. Нажмите /start", show_alert=True)
        return

    group_text = "Нет группировки"
    position = "—"

    if user["group_id"]:
        group = await db.fetchone(
            "SELECT name, tag FROM groups WHERE id = ?", (user["group_id"],)
        )
        if group:
            group_text = f"{html.escape(group['name'])} [{html.escape(group['tag'])}]"
            position = {
                "leader": "Глава",
                "officer": "Заместитель",
                "player": "Боец",
            }.get(user["role"], html.escape(user["role"]))

    nick_safe = html.escape(user["game_nick"])
    kills = user["kills"] or 0
    deaths = user["deaths"] or 0
    headshots = user["headshots"] or 0
    max_dist = round(user["max_kill_distance"] or 0, 1)

    # Считаем K/D (убийства / смерти) и процент хедшотов (HS%)
    kd_ratio = round(kills / deaths, 2) if deaths > 0 else float(kills)
    hs_percent = round((headshots / kills) * 100, 1) if kills > 0 else 0.0

    text_content = (
        "📁 <b>Досье</b>\n\n"
        f"Ник: <code>{nick_safe}</code>\n"
        f"Убийства: <b>{kills}</b> | Смерти: <b>{deaths}</b> (K/D: {kd_ratio})\n"
        f"Хедшоты: <b>{headshots}</b> ({hs_percent}%)\n"
        f"Макс. дистанция убийства: <code>{max_dist} м</code>\n"
        f"Баланс: <b>{user['balance']}</b> 🪙\n"
        f"Группировка: {group_text}\n"
        f"Должность: {position}"
    )
    reply_markup = back_button()

    if callback.id == "0":
        await callback.message.answer(text_content, reply_markup=reply_markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(text_content, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()
