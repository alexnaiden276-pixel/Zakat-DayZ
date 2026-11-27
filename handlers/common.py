import contextlib
import html
import logging
import os

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from keyboards.menus import back_button
from services.leaderboard import build_leaderboard_text, publish_leaderboard

router = Router()
logger = logging.getLogger(__name__)


async def _send_or_edit_common(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if getattr(callback, "id", None) == "0":
        await callback.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
    else:
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await callback.answer()


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
    clean_username = (bot_info.username or "").lstrip("@").strip()

    bot_url = os.getenv("CABINET_BUTTON_URL", "").strip()
    if bot_url:
        if not bot_url.startswith(("https://", "http://")):
            await message.answer(
                "❌ Некорректный формат <code>CABINET_BUTTON_URL</code> в <code>.env</code> — ссылка должна начинаться с <code>https://</code>.",
                parse_mode="HTML",
            )
            return
    else:
        if clean_username.lower().endswith("bot"):
            bot_url = f"https://t.me{clean_username}?start=cabinet"
        else:
            await message.answer(
                "❌ Юзернейм бота (<code>@" + html.escape(clean_username) + "</code>) не заканчивается на <code>bot</code>, "
                "поэтому Telegram блокирует прямую ссылку в кнопке.\n\n"
                "<b>Как исправить за 1 минуту:</b>\n"
                "1. Сократите ссылку <code>https://t.me" + html.escape(clean_username) + "?start=cabinet</code> через любой сервис (например, clck.ru) "
                "или привяжите боту второй юзернейм с окончанием на <code>bot</code> в @BotFather.\n"
                "2. Добавьте в <code>.env</code> строку:\n"
                "<code>CABINET_BUTTON_URL=ваша_https_ссылка</code>\n"
                "3. Перезапустите бота и снова нажмите /post_cabinet.",
                parse_mode="HTML",
            )
            return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👤 Открыть личный кабинет", url=bot_url)]
        ]
    )

    try:
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
    except TelegramAPIError as err:
        logger.warning("Не удалось опубликовать кнопку личного кабинета: %s", err)
        await message.answer(
            f"❌ Ошибка при отправке сообщения в группу: <code>{html.escape(str(err))}</code>",
            parse_mode="HTML",
        )
        return

    with contextlib.suppress(TelegramAPIError):
        await bot.pin_chat_message(
            chat_id=settings.cabinet_chat_id,
            message_id=msg.message_id,
            disable_notification=True,
        )

    await message.answer(
        f"✅ Кнопка личного кабинета опубликована и закреплена в чате "
        f"<code>{settings.cabinet_chat_id}</code> (тема: <code>{settings.cabinet_thread_id}</code>).\n"
        f"Ссылка кнопки: <code>{html.escape(bot_url)}</code>",
        parse_mode="HTML",
    )


@router.message(Command("top_now"))
async def cmd_publish_top_now(message: Message, bot: Bot, db, settings):
    """Админ-команда для немедленной публикации/обновления таблицы лидеров в теме группы."""
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    try:
        await publish_leaderboard(
            bot=bot,
            db=db,
            chat_id=settings.leaderboard_chat_id,
            thread_id=settings.leaderboard_thread_id,
            force_new_message=False,
        )
    except TelegramAPIError as err:
        logger.warning("Не удалось опубликовать таблицу лидеров: %s", err)
        await message.answer(
            f"❌ Ошибка при публикации таблицы лидеров: <code>{html.escape(str(err))}</code>",
            parse_mode="HTML",
        )
        return

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
    await _send_or_edit_common(callback, settings.rules_text, back_button())


@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery, db):
    user = await db.get_user(callback.from_user.id)
    if not user:
        if getattr(callback, "id", None) != "0":
            await callback.answer("Профиль не найден. Нажмите /start", show_alert=True)
        return

    group_text = "Нет группировки"
    position = "—"

    if user["group_id"]:
        group = await db.fetchone(
            "SELECT name, tag FROM groups WHERE id = ?", (user["group_id"],)
        )
        if group:
            group_text = f"{html.escape(str(group['name']))} [{html.escape(str(group['tag']))}]"
            position = {
                "leader": "Глава",
                "officer": "Заместитель",
                "player": "Боец",
            }.get(user["role"], html.escape(str(user["role"])))

    nick_safe = html.escape(str(user["game_nick"] or "Не указан"))
    kills = user["kills"] or 0
    deaths = user["deaths"] or 0
    headshots = user["headshots"] or 0
    max_dist = round(user["max_kill_distance"] or 0, 1)

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
    await _send_or_edit_common(callback, text_content, back_button())
