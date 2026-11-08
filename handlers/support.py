import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from keyboards.menus import back_button

router = Router()

class SupportTicket(StatesGroup):
    message = State()


@router.callback_query(F.data == "support")
async def support_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportTicket.message)
    await callback.message.answer("Опишите проблему одним сообщением.")
    
    # ИСПРАВЛЕНО: Вызываем ответ на колбэк только если это реальный клик по инлайн-кнопке
    if callback.id != "0":
        await callback.answer()


@router.message(SupportTicket.message)
async def support_send(message: Message, state: FSMContext, db, bot, settings):
    if not settings.admin_chat_id:
        await state.clear()
        await message.answer("Чат администрации не настроен. Попробуйте позже.")
        return

    user = await db.get_user(message.from_user.id)
    ticket_id = await db.execute(
        "INSERT INTO tickets(user_id, status) VALUES (?, 'open')",
        (user["id"],),
    )
    body = (message.text or message.caption or "").strip()
    await db.execute(
        "INSERT INTO ticket_messages(ticket_id, sender_id, body) VALUES (?, ?, ?)",
        (ticket_id, user["id"], body),
    )

    sent = await bot.send_message(
        settings.admin_chat_id,
        f"🎫 Обращение #{ticket_id} от {user['game_nick']} "
        f"(Telegram ID: {message.from_user.id})\n\n{body}\n\n"
        "Ответьте реплаем на это сообщение.",
    )
    await db.execute(
        """UPDATE ticket_messages SET admin_message_id = ?
           WHERE ticket_id = ? ORDER BY id DESC LIMIT 1""",
        (sent.message_id, ticket_id),
    )
    await state.clear()
    await message.answer(f"Обращение #{ticket_id} отправлено администрации.")


@router.message(F.reply_to_message)
async def admin_reply(message: Message, db, settings, bot):
    if message.chat.id != settings.admin_chat_id:
        return
    if message.from_user.id not in settings.admin_ids:
        return

    row = await db.fetchone(
        """SELECT tm.ticket_id, t.user_id
           FROM ticket_messages tm
           JOIN tickets t ON t.id = tm.ticket_id
           WHERE tm.admin_message_id = ?""",
        (message.reply_to_message.message_id,),
    )
    if not row:
        return

    user = await db.fetchone("SELECT telegram_id FROM users WHERE id = ?", (row["user_id"],))
    if not user:
        await message.answer("Не удалось найти автора обращения.")
        return

    try:
        await bot.send_message(
            user["telegram_id"],
            f"💬 Ответ администрации по обращению #{row['ticket_id']}:\n\n"
            f"{message.text or message.caption or ''}",
        )
    except Exception:
        await message.answer("Не удалось отправить ответ игроку.")
        return

    await db.execute(
        "INSERT INTO ticket_messages(ticket_id, sender_id, body) VALUES (?, ?, ?)",
        (row["ticket_id"], message.from_user.id, message.text or message.caption or ""),
    )
    await message.answer("Ответ отправлен игроку.")
