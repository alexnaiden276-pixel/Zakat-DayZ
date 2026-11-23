import html
import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

router = Router()
logger = logging.getLogger(__name__)


class SupportTicket(StatesGroup):
    message = State()


@router.callback_query(F.data == "support")
async def support_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportTicket.message)
    await callback.message.answer(
        "🆘 <b>Служба поддержки</b>\n\n"
        "Опишите вашу проблему одним текстовым сообщением (или введите /cancel для отмены):",
        parse_mode="HTML",
    )
    if callback.id != "0":
        await callback.answer()


@router.message(SupportTicket.message)
async def support_send(message: Message, state: FSMContext, db, bot, settings):
    body = (message.text or message.caption or "").strip()
    if body.lower() == "/cancel":
        await state.set_state(None)
        await message.answer("❌ Обращение в поддержку отменено.")
        return

    if not body:
        await message.answer("❌ Пожалуйста, опишите проблему текстом (или отправьте фото с текстовым описанием):")
        return

    if not settings.admin_chat_id:
        await state.set_state(None)
        await message.answer("⚠️ Чат администрации не настроен. Попробуйте позже.")
        return

    user = await db.get_user(message.from_user.id)
    if not user:
        await state.set_state(None)
        await message.answer("❌ Сначала зарегистрируйтесь через /start.")
        return

    ticket_id = await db.execute(
        "INSERT INTO tickets(user_id, status) VALUES (?, 'open')",
        (user["id"],),
    )
    msg_row_id = await db.execute(
        "INSERT INTO ticket_messages(ticket_id, sender_id, body) VALUES (?, ?, ?)",
        (ticket_id, user["id"], body),
    )

    safe_nick = html.escape(str(user["game_nick"]))
    safe_body = html.escape(body)

    try:
        sent = await bot.send_message(
            settings.admin_chat_id,
            f"🎫 <b>Обращение #{ticket_id}</b> от <code>{safe_nick}</code> "
            f"(ID: <code>{message.from_user.id}</code>)\n\n"
            f"{safe_body}\n\n"
            "<i>Ответьте реплаем на это сообщение.</i>",
            parse_mode="HTML",
        )
        await db.execute(
            "UPDATE ticket_messages SET admin_message_id = ? WHERE id = ?",
            (sent.message_id, msg_row_id),
        )
    except Exception as e:
        logger.error("[SUPPORT] Не удалось переслать тикет #%s в админ-чат: %s", ticket_id, e)
        await state.set_state(None)
        await message.answer("❌ Не удалось доставить обращение администрации. Попробуйте позже.")
        return

    await state.set_state(None)
    await message.answer(f"✅ Обращение <b>#{ticket_id}</b> отправлено администрации.", parse_mode="HTML")


@router.message(F.reply_to_message)
async def admin_reply(message: Message, db, settings, bot):
    if not settings.admin_chat_id or message.chat.id != settings.admin_chat_id:
        return
    if not message.from_user or message.from_user.id not in settings.admin_ids:
        return

    reply_text = (message.text or message.caption or "").strip()
    if not reply_text:
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
        await message.answer("❌ Не удалось найти автора обращения.")
        return

    try:
        await bot.send_message(
            user["telegram_id"],
            f"💬 <b>Ответ администрации по обращению #{row['ticket_id']}:</b>\n\n"
            f"{html.escape(reply_text)}",
            parse_mode="HTML",
        )
    except Exception:
        await message.answer("❌ Не удалось отправить ответ игроку (возможно, он заблокировал бота).")
        return

    admin_user = await db.get_user(message.from_user.id)
    sender_db_id = admin_user["id"] if admin_user else message.from_user.id
    await db.execute(
        "INSERT INTO ticket_messages(ticket_id, sender_id, body) VALUES (?, ?, ?)",
        (row["ticket_id"], sender_db_id, reply_text),
    )
    await message.answer("✅ Ответ отправлен игроку.")
