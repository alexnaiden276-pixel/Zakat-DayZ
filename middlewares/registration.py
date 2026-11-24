from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject


def _is_private_chat(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        return event.chat.type == "private"
    if isinstance(event, CallbackQuery) and event.message:
        return event.message.chat.type == "private"
    return False


class RegistrationMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event: TelegramObject, data: dict):
        # В группах не запрашиваем профиль из БД (кроме админ-ответов техподдержки)
        if not _is_private_chat(event):
            data["db_user"] = None
            return await handler(event, data)

        tg_user = getattr(event, "from_user", None)
        user = await self.db.get_user(tg_user.id) if tg_user else None
        data["db_user"] = user

        return await handler(event, data)


class IsVerifiedPlayer(BaseFilter):
    """
    Фильтр для защиты игровых команд.
    Пропускает только тех, кто успешно зарегистрирован в базе данных и пишет в личные сообщения.
    """

    async def __call__(self, event: Message | CallbackQuery, db_user: dict | object | None = None) -> bool:
        if not _is_private_chat(event):
            return False

        if db_user is not None:
            return True

        if isinstance(event, CallbackQuery):
            await event.answer("❌ Сначала зарегистрируйтесь через /start", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("❌ Эта команда доступна только зарегистрированным игрокам.\nИспользуйте команду: /start")

        return False
