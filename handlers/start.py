import logging
import re
from aiogram import F, Router
from aiogram.filters import CommandStart, or_f  # Добавили корректный логический фильтр or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

# Импортируем обе функции создания клавиатур
from keyboards.menus import main_menu, start_reply_keyboard

router = Router()
log = logging.getLogger(__name__)


# Структура FSM состояний
class Registration(StatesGroup):
    waiting_for_data = State()  # Ждем строку вида: Ник 12345678


# Применили or_f вместо оператора |, чтобы библиотека magic-filter не выдавала ошибку типов
@router.message(or_f(CommandStart(), F.text == "Старт 🚀"))
async def start(message: Message, state: FSMContext, db, settings):
    user = await db.get_user(message.from_user.id)
    if user:
        await state.clear()
        # Сначала отправляем Reply-клавиатуру, чтобы кнопка встала вниз экрана навсегда
        await message.answer(
            "Вы перешли в главное меню.", 
            reply_markup=start_reply_keyboard()
        )
        # Следом отправляем само инлайн-меню действий
        await message.answer(
            f"Добро пожаловать, {user['game_nick']}!",
            reply_markup=main_menu(message.from_user.id in settings.admin_ids),
        )
        return

    # Включаем ожидание ввода данных для верификации (здесь Reply-клавиатуру не шлем, чтобы не мешать)
    await state.set_state(Registration.waiting_for_data)
    await message.answer(
        "👋 **Добро пожаловать!**\n\n"
        "Для привязки аккаунта выполните следующие шаги:\n"
        "1. Зайдите на наш server DayZ (вы должны появиться в свежих логах).\n"
        "2. Отправьте сюда ваш **игровой ник** и через пробел **последние 8 символов вашего ID** из игры.\n\n"
        "Пример отправки:\n`SonyGamer99 e5f6g7h8`"
    )


# Хэндлер обработки ввода данных для регистрации
@router.message(Registration.waiting_for_data)
async def register_player(message: Message, state: FSMContext, db, nitrado_api, settings):
    # Разделяем входящий текст по пробелам
    text_data = (message.text or "").strip().split()
    
    # Проверяем, что ввели ровно два значения (Ник и Хвост)
    if len(text_data) != 2:
        await message.answer(
            "❌ Неверный формат.\n"
            "Введите ваш ник и последние 8 символов ID строго через пробел.\n"
            "Пример: `SonyGamer99 e5f6g7h8`"
        )
        return
        
    nick, user_short_id = text_data[0], text_data[1]
    
    # Проверяем, что длина хвоста ID равна 8 символам
    if len(user_short_id) != 8:
        await message.answer("❌ Ошибка: Хвост вашего игрового ID должен состоять ровно из 8 символов.")
        return

    # Проверяем, не занят ли уже ник в нашей БД
    if await db.get_user_by_nick(nick):
        await message.answer("Этот игровой ник уже привязан к другому аккаунту Telegram.")
        return

    status_msg = await message.answer("⏳ Скачиваем свежие логи сервера Nitrado и сверяем данные, пожалуйста, подождите...")

    try:
        full_game_id = await nitrado_api.find_player_id_in_logs(nick)
    except Exception:
        log.exception("Ошибка при запросе к API Nitrado")
        await status_msg.delete()
        await message.answer("❌ Не удалось связаться с API Nitrado. Попробуйте позже.")
        return

    # Если игрока с таким ником нет в логах
    if not full_game_id:
        await status_msg.delete()
        await message.answer(
            f"❌ Игрок с ником `{nick}` не найден в последних логах подключений.\n\n"
            "**Что делать?**\n"
            "1. Зайдите на сервер на 1-2 минуты, после чего выйдите.\n"
            "2. Убедитесь, что ник написан точь-в-точь как в игре (с учетом регистра букв)."
        )
        return

    # Обрезаем последние 8 символов от РЕАЛЬНОГО полного ID из лога Nitrado
    real_short_id = full_game_id[-8:]

    # Сравниваем хвосты без учета регистра букв (lower)
    if real_short_id.lower() != user_short_id.lower():
        await status_msg.delete()
        await message.answer("❌ Указанные 8 символов ID не совпадают с логами сервера. Проверьте данные и попробуйте еще раз.")
        return

    # Если проверка пройдена, определяем роль и записываем в БД
    role = "admin" if message.from_user.id in settings.admin_ids else "player"
    try:
        await db.create_user(message.from_user.id, nick, role)
    except Exception:
        log.exception("Не удалось зарегистрировать Telegram ID %s", message.from_user.id)
        await status_msg.delete()
        await message.answer("Не удалось сохранить данные в базу данных. Попробуйте позже.")
        return

    await state.clear()
    await status_msg.delete()
    
    # После успешной регистрации жестко закрепляем нижнюю кнопку и выводим меню
    await message.answer("Нижняя панель активирована.", reply_markup=start_reply_keyboard())
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
    await callback.message.edit_text(
        "Главное меню:",
        reply_markup=main_menu(callback.from_user.id in settings.admin_ids),
    )
    await callback.answer()
