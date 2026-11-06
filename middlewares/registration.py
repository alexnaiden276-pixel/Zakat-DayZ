from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject, Message, CallbackQuery

class RegistrationMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event: TelegramObject, data: dict):
        tg_user = getattr(event, "from_user", None)
        
        # Получаем пользователя из вашей БД по его Telegram ID
        # (Убедитесь, что метод get_user возвращает объект/словарь со свойством is_verified)
        user = await self.db.get_user(tg_user.id) if tg_user else None
        data["db_user"] = user
        
        return await handler(event, data)


class IsVerifiedPlayer(BaseFilter):
    """
    Фильтр для защиты игровых команд.
    Пропускает только тех, у кого в БД поле is_verified == True (или 1).
    """
    async def __call__(self, event: Message | CallbackQuery, db_user: dict | getattr | None) -> bool:
        # Проверяем флаг верификации (в зависимости от того, возвращает БД словарь или объект)
        is_verified = False
        if db_user:
            if isinstance(db_user, dict):
                is_verified = db_user.get("is_verified")
            else:
                is_verified = getattr(db_user, "is_verified", False)

        if is_verified:
            return True

        # Если не верифицирован — отправляем предупреждение и блокируем хэндлер
        if isinstance(event, CallbackQuery):
            await event.answer("❌ Сначала верифицируйте аккаунт через /register", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("❌ Эта команда доступна только верифицированным игрокам.\nИспользуйте команду: /register")
            
        return False
