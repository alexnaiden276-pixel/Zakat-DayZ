import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from keyboards.menus import main_menu

router = Router()
log = logging.getLogger(__name__)


class Registration(StatesGroup):
    nick = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, db, settings):
    user = await db.get_user(message.from_user.id)
    if user:
        await state.clear()
        await message.answer(
            f"Добро пожаловать, {user['game_nick']}!",
            reply_markup=main_menu(message.from_user.id in settings.admin_ids),
        )
        return

    await state.set_state(Registration.nick)
    await message.answer("Введите игровой ник для привязки аккаунта.")


@router.message(Registration.nick)
async def register_nick(message: Message, state: FSMContext, db, game_api, settings):
    nick = (message.text or "").strip()
    if not await game_api.validate_nick(nick):
        await message.answer("Ник не прошёл проверку. Введите корректный игровой ник.")
        return

    if await db.get_user_by_nick(nick):
        await message.answer("Этот игровой ник уже привязан к аккаунту.")
        return

    role = "admin" if message.from_user.id in settings.admin_ids else "player"
    try:
        await db.create_user(message.from_user.id, nick, role)
    except Exception:
        log.exception("Не удалось зарегистрировать Telegram ID %s", message.from_user.id)
        await message.answer("Не удалось завершить регистрацию. Попробуйте позже.")
        return

    await state.clear()
    await message.answer(
        f"Аккаунт {nick} зарегистрирован.",
        reply_markup=main_menu(role == "admin"),
    )


@router.callback_query(F.data == "menu")
async def show_menu(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
        return
    await callback.message.edit_text(
        "Главное меню:",
        reply_markup=main_menu(callback.from_user.id in settings.admin_ids),
    )
    await callback.answer()