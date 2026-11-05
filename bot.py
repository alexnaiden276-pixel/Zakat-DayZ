import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    logger.info("Команда /start от %s", message.from_user.id)
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n"
        "Я тестовый бот. Всё работает ✅\n\n"
        "Доступные команды:\n"
        "/help — справка\n"
        "/ping — проверка отклика"
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
        raise SystemExit("❌ Не задан BOT_TOKEN. Создайте .env (см. .env.example)")
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
