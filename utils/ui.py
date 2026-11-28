import contextlib
import html

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto


def chunk_buttons(buttons: list[InlineKeyboardButton], cols: int = 2) -> list[list[InlineKeyboardButton]]:
    """Раскладывает список кнопок сеткой по cols в ряд."""
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def cancel_kb(back_cb: str) -> InlineKeyboardMarkup:
    """Возвращает клавиатуру с одной кнопкой отмены."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=back_cb)]
    ])


async def safe_render_menu(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
    photo_id: str | None = None,
) -> None:
    """Безопасно переключает сообщение между текстом и фото без ошибок Telegram API."""
    msg = callback.message
    if not msg:
        return

    had_photo = bool(getattr(msg, "photo", None))

    try:
        if photo_id and had_photo:
            await msg.edit_media(
                media=InputMediaPhoto(media=photo_id, caption=text, parse_mode="HTML"),
                reply_markup=markup,
            )
            return
        if not photo_id and not had_photo:
            await msg.edit_text(text=text, reply_markup=markup, parse_mode="HTML")
            return
    except TelegramAPIError as e:
        if "message is not modified" in str(e).lower():
            return

    with contextlib.suppress(Exception):
        await msg.delete()

    if photo_id:
        await msg.answer_photo(photo=photo_id, caption=text, reply_markup=markup, parse_mode="HTML")
    else:
        await msg.answer(text=text, reply_markup=markup, parse_mode="HTML")


async def notify_balance_change(
    bot: Bot | None,
    telegram_id: int | None,
    delta: int,
    reason: str,
    new_balance: int | None = None,
) -> None:
    """Безопасно отправляет игроку в ЛС сообщение об изменении баланса."""
    if not bot or not telegram_id or delta == 0:
        return

    sign = "+" if delta > 0 else ""
    icon = "💰" if delta > 0 else "💸"
    lines = [
        f"{icon} <b>Изменение баланса: {sign}{delta} 🪙</b>",
        f"📝 Причина: <i>{html.escape(reason)}</i>",
    ]
    if new_balance is not None:
        lines.append(f"💵 Текущий баланс: <b>{new_balance} 🪙</b>")

    with contextlib.suppress(TelegramAPIError, Exception):
        await bot.send_message(
            chat_id=int(telegram_id),
            text="\n".join(lines),
            parse_mode="HTML",
        )
