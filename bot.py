import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# Bothost сам подставляет BOT_TOKEN — хардкод не нужен
BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏓 Проверить пинг", callback_data="ping")
    builder.button(text="ℹ️ Помощь", callback_data="help")
    builder.adjust(1)

    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я тестовый бот на <b>Bothost</b>. Всё работает ✅\n\n"
        "Выберите действие на клавиатуре ниже или отправьте любой текст — я верну его эхом.",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@dp.callback_query(F.data == "ping")
async def callback_ping(callback):
    await callback.answer("🏓 Pong! Бот отвечает.")
    await callback.message.answer("🏓 Pong! Бот жив и обрабатывает нажатия.")


@dp.callback_query(F.data == "help")
async def callback_help(callback):
    await callback.answer()
    await callback.message.answer(
        "ℹ️ <b>Справка</b>\n\n"
        "/start — главное меню\n"
        "/help — эта справка\n"
        "/ping — проверка отклика\n\n"
        "Любой текст — эхо.",
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>Справка</b>\n\n"
        "/start — начать\n"
        "/help — эта справка\n"
        "/ping — проверить связь\n\n"
        "Любое другое сообщение я верну эхом.",
        parse_mode="HTML",
    )


@dp.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("🏓 Pong! Бот жив и отвечает.")


@dp.message(F.text)
async def echo(message: Message) -> None:
    await message.answer(f"🔁 Эхо: {message.text}")


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Пока я понимаю только текст 🤷")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задан BOT_TOKEN в переменных окружения Bothost")
    bot = Bot(token=BOT_TOKEN)
    try:
        me = await bot.get_me()
        logger.info("Бот запущен: @%s (id=%s)", me.username, me.id)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
