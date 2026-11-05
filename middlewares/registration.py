from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


class RegistrationMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event, data):
        tg_user = getattr(event, "from_user", None)
        user = await self.db.get_user(tg_user.id) if tg_user else None
        data["db_user"] = user

        # /start всегда доступен, в том числе незарегистрированным.
        if isinstance(event, Message):
            text = event.text or ""
            if text.startswith("/start"):
                return await handler(event, data)

        # FSM-регистрация должна принимать ник от незарегистрированного игрока.
        state = data.get("state")
        current_state = await state.get_state() if state else None
        if current_state and current_state.startswith("Registration:"):
            return await handler(event, data)

        if not user:
            if isinstance(event, CallbackQuery):
                await event.answer("Сначала зарегистрируйтесь через /start", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Сначала зарегистрируйтесь через /start.")
            return None

        return await handler(event, data)