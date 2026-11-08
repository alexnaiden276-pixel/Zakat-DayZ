import logging
import re
from aiogram import F, Router
from aiogram.filters import CommandStart, or_f  # Используем корректный логический фильтр or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

# Импортируем клавиатуру меню
from keyboards.menus import main_menu

# ИМПОРТИРУЕМ ФУНКЦИИ ИЗ ВАШИХ ДРУГИХ ФАЙЛОВ ХЭНДЛЕРОВ
# (Убедитесь, что названия файлов и функций совпадают с вашим проектом)
from handlers.common import show_rules, show_summary
from handlers.economy import process_terminal_view, process_sell_loot, view_wallet
from handlers.groups import view_group_info
from handlers.support import open_support_ticket
from handlers.profile import show_user_profile  # если досье в отдельном файле, иначе импортируйте откуда нужно

router = Router()
log = logging.getLogger(__name__)


# Структура FSM состояний
class Registration(StatesGroup):
    waiting_for_data = State()  # Ждем строку вида: Ник 12345678


# Срабатывает на команду /start или на текстовое сообщение "Главное меню"
@router.message(or_f(CommandStart(), F.text == "Главное меню"))
async def start(message: Message, state: FSMContext, db, settings):
    user = await db.get_user(message.from_user.id)
    if user:
        await state.clear()
        is_admin = message.from_user.id in settings.admin_ids
        # Отправляем полноценное скрывающееся текстовое меню вниз экрана за один раз
        await message.answer(
            f"🤖 Главное меню открыто. Добро пожаловать, {user['game_nick']}!",
            reply_markup=main_menu(is_admin),
        )
        return

    # Включаем ожидание ввода данных для верификации
    await state.set_state(Registration.waiting_for_data)
    await message.answer(
        "👋 **Добро пожаловать!**\n\n"
        "Для привязки аккаунта выполните следующие шаги:\n"
        "1. Зайдите на наш server DayZ (вы должны появиться в свежих логах).\n"
        "2. Отправьте сюда ваш **игровой ник** и через пробел **последние 8 символов вашего ID** из игры.\n\n"
        "Пример отправки:\n`SonyGamer99 e5f6g7h8`"
    )


# --- ПОДКЛЮЧЕНИЕ ФУНКЦИЙ К НИЖНИМ КНОПКАМ МЕНЮ ---

@router.message(F.text == "🖥 Терминал")
async def process_terminal(message: Message, db):
    # Вызываем ту же функцию, которая работала на инлайн-кнопке terminal
    # Передаем ей message и другие нужные параметры (например, db), которые она требует
    await process_terminal_view(message, db)

@router.message(F.text == "📦 Сдать лут")
async def process_sell(message: Message, db):
    await process_sell_loot(message, db)

@router.message(F.text == "💰 Кошелек")
async def process_wallet(message: Message, db):
    await view_wallet(message, db)

@router.message(F.text == "📁 Досье")
async def process_profile(message: Message, db):
    await show_user_profile(message, db)

@router.message(F.text == "👥 Группировка")
async def process_group(message: Message, db):
    await view_group_info(message, db)

@router.message(F.text == "🆘 Поддержка")
async def process_support(message: Message, state: FSMContext):
    await open_support_ticket(message, state)

@router.message(F.text == "📜 Правила")
async def process_rules(message: Message):
    await show_rules(message)

@router.message(F.text == "📊 Сводка")
async def process_summary(message: Message, db):
    await show_summary(message, db)


# --- АДМИНСКИЕ КНОПКИ ---

@router.message(F.text == "💸 Финансы (Админ)")
async def process_admin_finance(message: Message, db, settings):
    if message.from_user.id not in settings.admin_ids:
        return
    # Импортируем функцию админки локально или сверху файла
    from handlers.admin import view_admin_finances
    await view_admin_finances(message, db)

@router.message(F.text == "🔄 Рестарт сервера")
async def process_restart(message: Message, nitrado_api, settings):
    if message.from_user.id not in settings.admin_ids:
        return
    from handlers.admin import trigger_server_restart
    await trigger_server_restart(message, nitrado_api)


# --- РЕГИСТРАЦИЯ И СТАРЫЙ КОЛБЭК ---

@router.message(Registration.waiting_for_data)
async def register_player(message: Message, state: FSMContext, db, nitrado_api, settings):
    text_data = (message.text or "").strip().split()
    if len(text_data) != 2:
        await message.answer("❌ Неверный формат.\nВведите ваш ник и последние 8 символов ID строго через пробел.")
        return
        
    nick, user_short_id = text_data[0], text_data[1]
    if len(user_short_id) != 8:
        await message.answer("❌ Ошибка: Хвост вашего игрового ID должен состоять ровно из 8 символов.")
        return

    if await db.get_user_by_nick(nick):
        await message.answer("Этот игровой ник уже привязан к другому аккаунту Telegram.")
        return

    status_msg = await message.answer("⏳ Скачиваем свежие логи сервера Nitrado и сверяем данные...")

    try:
        full_game_id = await nitrado_api.find_player_id_in_logs(nick)
    except Exception:
        log.exception("Ошибка при запросе к API Nitrado")
        await status_msg.delete()
        await message.answer("❌ Не удалось связаться с API Nitrado. Попробуйте позже.")
        return

    if not full_game_id:
        await status_msg.delete()
        await message.answer(f"❌ Игрок с ником `{nick}` не найден в последних логах подключений.")
        return

    if full_game_id[-8:].lower() != user_short_id.lower():
        await status_msg.delete()
        await message.answer("❌ Указанные 8 символов ID не совпадают с логами сервера.")
        return

    role = "admin" if message.from_user.id in settings.admin_ids else "player"
    try:
        await db.create_user(message.from_user.id, nick, role)
    except Exception:
        log.exception("Не удалось зарегистрировать Telegram ID %s", message.from_user.id)
        await status_msg.delete()
        await message.answer("Не удалось сохранить данные в базу данных.")
        return

    await state.clear()
    await status_msg.delete()
    
    await message.answer(
        f"🎉 Аккаунт {nick} успешно верифицирован и зарегистрирован!",
        reply_markup=main_menu(role == "admin"),
    )


@router.callback_query(F.data == "menu")
async def show_menu(callback: CallbackQuery, db, settings):
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("Сначала зарегистрируйтесь через команду /start", show_alert=True)
        return
    
    is_admin = callback.from_user.id in settings.admin_ids
    await callback.message.answer(
        "Вы вернулись в главное меню.",
        reply_markup=main_menu(is_admin)
    )
    await callback.answer()
