from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject, Message, CallbackQuery

class RegistrationMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event: TelegramObject, data: dict):
        tg_user = getattr(event, "from_user", None)
        
        # Получаем пользователя из вашей БД по его Telegram ID
        user = await self.db.get_user(tg_user.id) if tg_user else None
        data["db_user"] = user
        
        return await handler(event, data)


class IsVerifiedPlayer(BaseFilter):
    """
    Фильтр для защиты игровых команд.
    Пропускает только тех, кто успешно зарегистрирован в базе данных.
    """
    # ИСПРАВЛЕНО: добавлено значение по умолчанию = None
    async def __call__(self, event: Message | CallbackQuery, db_user: dict | object | None = None) -> bool:
        # Так как ваш метод create_user записывает игрока только после успешной верификации,
        # сам факт наличия db_user в базе данных означает, что он верифицирован.
        if db_user is not None:
            return True

        # Если записи в БД нет — отправляем предупреждение и блокируем хэндлер
        if isinstance(event, CallbackQuery):
            await event.answer("❌ Сначала зарегистрируйтесь через /start", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("❌ Эта команда доступна только зарегистрированным игрокам.\nИспользуйте команду: /start")
            
        return False
