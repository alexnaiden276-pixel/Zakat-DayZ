from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message


def is_user_admin(user_row, settings, tg_id: int) -> bool:
    """Проверяет, является ли пользователь администратором в БД или в настройках."""
    if user_row and user_row["role"] == "admin":
        return True

    admin_ids = getattr(settings, "admin_ids", None) or getattr(settings, "admins", None) or []
    if isinstance(admin_ids, (list, tuple, set)) and tg_id in admin_ids:
        return True

    single_admin = getattr(settings, "admin_id", None)
    return tg_id == single_admin


class IsAdmin(BaseFilter):
    """Фильтр для защиты админских роутеров и хендлеров."""

    async def __call__(self, event: Message | CallbackQuery, db, settings) -> bool:
        if not event.from_user:
            return False

        user = await db.get_user(event.from_user.id)
        allowed = is_user_admin(user, settings, event.from_user.id)

        if not allowed and isinstance(event, CallbackQuery):
            await event.answer("⛔ Доступ запрещён", show_alert=True)

        return allowed

