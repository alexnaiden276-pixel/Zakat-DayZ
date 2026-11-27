import contextlib
import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from handlers.common import profile, rules
from handlers.economy import (
    admin_finance_menu,
    admin_shop_menu,
    terminal,
    wallet,
)
from handlers.groups import admin_flags_menu, group_menu
from handlers.summary import restart, summary_menu
from handlers.support import support_start
from keyboards.menus import MenuBtn, admin_menu, main_menu

router = Router()
log = logging.getLogger(__name__)


class Registration(StatesGroup):
    waiting_for_data = State()


def get_fake_callback(message: Message, callback_data: str) -> CallbackQuery:
    return CallbackQuery(
        id="0",
        from_user=message.from_user,
        chat_instance="0",
        message=message,
        data=callback_data,
    )


@router.message(
    StateFilter("*"),
    or_f(CommandStart(), F.text == "Главное меню", F.text == MenuBtn.BACK_TO_MAIN),
)
async def start(message: Message, state: FSMContext, db, settings):
    user = await db.get_user(message.from_user.id)
    if user:
        await state.clear()
        is_admin = message.from_user.id in settings.admin_ids
        safe_nick = html.escape(str(user["game_nick"]))
        await message.answer(
            f"🤖 Главное меню открыто. Добро пожаловать, <b>{safe_nick}</b>!",
            reply_markup=main_menu(is_admin),
            parse_mode="HTML",
        )
        return

    await state.set_state(Registration.waiting_for_data)
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Для привязки аккаунта выполните следующие шаги:\n"
        "1. Зайдите на наш сервер DayZ (вы должны появиться в свежих логах).\n"
        "2. Отправьте сюда <b>последние 8 символов вашего ID</b> из игры (ваш игровой ник определится автоматически).\n\n"
        "Пример отправки:\n<code>e5f6g7h8</code>",
        parse_mode="HTML",
    )


# --- ОБРАБОТЧИКИ КНОПОК ГЛАВНОГО МЕНЮ ---

@router.message(F.text == MenuBtn.TERMINAL)
async def process_terminal(message: Message, db, state: FSMContext):
    await state.clear()
    await terminal(get_fake_callback(message, "terminal"), db, state)


@router.message(F.text == MenuBtn.WALLET)
async def process_wallet(message: Message, db, state: FSMContext):
    await state.clear()
    await wallet(get_fake_callback(message, "wallet"), db, state)


@router.message(F.text == MenuBtn.SUPPORT)
async def process_support(message: Message, state: FSMContext):
    await state.clear()
    await support_start(get_fake_callback(message, "support"), state)


@router.message(F.text == MenuBtn.SUMMARY)
async def process_summary(message: Message, state: FSMContext):
    await state.clear()
    await summary_menu(get_fake_callback(message, "summary"))


@router.message(F.text == MenuBtn.GROUP)
async def process_group(message: Message, db, state: FSMContext):
    await state.clear()
    await group_menu(get_fake_callback(message, "group"), db, state)


@router.message(F.text == MenuBtn.DOSSIER)
async def process_profile(message: Message, db, state: FSMContext):
    await state.clear()
    await profile(get_fake_callback(message, "profile"), db)


@router.message(F.text == MenuBtn.RULES)
async def process_rules(message: Message, settings, state: FSMContext):
    await state.clear()
    await rules(get_fake_callback(message, "rules"), settings)


# --- АДМИН-ПАНЕЛЬ И АДМИН-КНОПКИ ---

@router.message(F.text == MenuBtn.ADMIN_PANEL)
async def process_admin_panel(message: Message, settings, state: FSMContext):
    if message.from_user.id not in settings.admin_ids:
        return
    await state.clear()
    await message.answer(
        "⚙️ <b>Админ-панель</b>\nВыберите нужный раздел:",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )
@router.message(F.text == MenuBtn.ADMIN_RESTART)
async def process_restart(message: Message, settings, state: FSMContext):
    if message.from_user.id not in settings.admin_ids:
        return
    await state.clear()
    await restart(get_fake_callback(message, "restart"), settings)


@router.message(F.text == MenuBtn.ADMIN_FINANCE)
async def process_admin_finance(message: Message, db, settings, state: FSMContext):
    if message.from_user.id not in settings.admin_ids:
        return
    await state.clear()
    await admin_finance_menu(
        get_fake_callback(message, "admin_finance"),
        db=db,
        settings=settings,
        state=state,
    )


@router.message(F.text == MenuBtn.ADMIN_SHOP)
async def process_admin_shop(message: Message, db, settings, state: FSMContext):
    if message.from_user.id not in settings.admin_ids:
        return
    await state.clear()
    await admin_shop_menu(message, db=db, settings=settings, state=state)


@router.message(F.text == MenuBtn.ADMIN_CLANS)
async def process_admin_flags(message: Message, db, settings, state: FSMContext):
    if message.from_user.id not in settings.admin_ids:
        return
    await state.clear()
    try:
        await admin_flags_menu(message, db=db, settings=settings, state=state)
    except Exception:
        log.exception("Ошибка при открытии меню 'Кланы и флаги'")
        await message.answer("⚠️ Ошибка при открытии меню флагов. Подробности записаны в консоль бота.")


# --- РЕГИСТРАЦИЯ И ВОЗВРАТ В МЕНЮ ---

@router.message(Registration.waiting_for_data)
async def register_player(message: Message, state: FSMContext, db, nitrado_api, settings):
    user_short_id = (message.text or "").strip()

    if " " in user_short_id or len(user_short_id) != 8:
        await message.answer(
            "❌ Неверный формат.\n"
            "Отправьте только <b>последние 8 символов</b> вашего игрового ID (без ника и пробелов).",
            parse_mode="HTML",
        )
        return

    safe_short_id = html.escape(user_short_id)
    status_msg = await message.answer("⏳ Скачиваем свежие логи сервера Nitrado и ищем ваш ID...")

    try:
        result = await nitrado_api.find_player_by_short_id(user_short_id)
    except Exception:
        log.exception("Ошибка при запросе к API Nitrado")
        await status_msg.edit_text("❌ Не удалось связаться с сервером логов Nitrado. Попробуйте позже.")
        return

    if not result:
        await status_msg.edit_text(
            f"❌ Игрок с ID, оканчивающимся на <code>{safe_short_id}</code>, не найден в последних логах подключений.",
            parse_mode="HTML",
        )
        return

    nick, _ = result
    safe_nick = html.escape(str(nick))

    if await db.get_user_by_nick(nick):
        await status_msg.edit_text(
            f"❌ Игровой аккаунт <b>{safe_nick}</b> уже привязан к другому профилю Telegram.",
            parse_mode="HTML",
        )
        return

    role = "admin" if message.from_user.id in settings.admin_ids else "player"
    try:
        await db.create_user(message.from_user.id, nick, role)
    except Exception:
        log.exception("Не удалось зарегистрировать Telegram ID %s", message.from_user.id)
        await status_msg.edit_text("❌ Не удалось сохранить данные в базу данных.")
        return

    await state.clear()
    with contextlib.suppress(TelegramBadRequest):
        await status_msg.delete()

    await message.answer(
        f"🎉 Аккаунт <b>{safe_nick}</b> успешно верифицирован и зарегистрирован!",
        reply_markup=main_menu(role == "admin"),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "menu")
async def show_menu(callback: CallbackQuery, db, settings, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через команду /start", show_alert=True)
        return

    await state.clear()
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(reply_markup=None)

    is_admin = callback.from_user.id in settings.admin_ids
    await callback.message.answer(
        "Вы вернулись в главное меню.",
        reply_markup=main_menu(is_admin),
    )
    await callback.answer()
