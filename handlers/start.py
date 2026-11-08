import logging
import re
from aiogram import F, Router
from aiogram.filters import CommandStart, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

# Импортируем только клавиатуру меню
from keyboards.menus import main_menu

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


# --- МАГИЯ ИМИТАЦИИ ИНЛАЙН-КНОПОК ЧЕРЕЗ ТЕКСТОВЫЕ КНОПКИ ---
# Бот перехватывает текст, превращает его в виртуальный клик инлайн-кнопки и передает в другие ваши файлы!

async def simulate_callback(message: Message, callback_data: str):
    """Вспомогательная функция, которая создает фейковый CallbackQuery и отправляет его в роутер"""
    fake_call = CallbackQuery(
        id="0",
        from_user=message.from_user,
        chat_instance="0",
        message=message,
        data=callback_data
    )
    # Отправляем фейковый клик обратно в основной диспетчер бота
    await message.bot.feed_update(message.bot, update={"callback_query": fake_call.model_dump()})

@router.message(F.text == "🖥 Терминал")
async def process_terminal(message: Message):
    await simulate_callback(message, "terminal")

@router.message(F.text == "📦 Сдать лут")
async def process_sell(message: Message):
    await simulate_callback(message, "sell")

@router.message(F.text == "💰 Кошелек")
async def process_wallet(message: Message):
    await simulate_callback(message, "wallet")

@router.message(F.text == "📁 Досье")
async def process_profile(message: Message):
    await simulate_callback(message, "profile")

@router.message(F.text == "👥 Группировка")
async def process_group(message: Message):
    await simulate_callback(message, "group")

@router.message(F.text == "🆘 Поддержка")
async def process_support(message: Message):
    await simulate_callback(message, "support")

@router.message(F.text == "📜 Правила")
async def process_rules(message: Message):
    await simulate_callback(message, "rules")

@router.message(F.text == "📊 Сводка")
async def process_summary(message: Message):
    await simulate_callback(message, "summary")

@router.message(F.text == "💸 Финансы (Админ)")
async def process_admin_finance(message: Message, settings):
    if message.from_user.id in settings.admin_ids:
        await simulate_callback(message, "admin_finance")

@router.message(F.text == "🔄 Рестарт сервера")
async def process_restart(message: Message, settings):
    if message.from_user.id in settings.admin_ids:
        await simulate_callback(message, "restart")


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
