import asyncio
import html
import json
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.nitrado_api import NitradoAPI

logger = logging.getLogger(__name__)

# Таймауты этапов (в секундах)
POS_CHECK_TIMEOUT = 15 * 60         # 15 мин на прибытие в зону после подхода очереди
TOWER_DROP_TIMEOUT = 20 * 60        # 20 мин на активный сброс и уничтожение лута, затем переход в фон
BACKGROUND_WAIT_TIMEOUT = 4 * 3600  # 4 часа фонового ожидания


def _parse_sqlite_utc(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _seconds_since(ts_str: str | None) -> float:
    dt = _parse_sqlite_utc(ts_str)
    if not dt:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def _send_safe(
    bot: Bot,
    chat_id: int,
    text: str,
    thread_id: int | None = None,
) -> bool:
    for _ in range(2):
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
            )
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramAPIError as e:
            logger.error("[LOOT_DROP] Ошибка Telegram API: %s", e)
            return False
        except Exception as e:
            logger.error("[LOOT_DROP] Непредвиденная ошибка отправки: %s", e)
            return False
    return False


async def _track_trade_zone_presence(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    drop_x: float,
    drop_z: float,
    drop_r: float,
) -> set[str]:
    """
    Отслеживает вход и выход зарегистрированных игроков из торговой зоны по последней записи в .ADM.
    Возвращает множество ников (в нижнем регистре), находящихся сейчас в зоне.
    """
    inside_nicks = await nitrado.get_players_in_sell_zone(drop_x, drop_z, drop_r)

    users = await db.fetchall("SELECT id, telegram_id, game_nick FROM users WHERE game_nick IS NOT NULL")
    if not users:
        return inside_nicks

    presence_rows = await db.fetchall("SELECT key, value FROM settings WHERE key LIKE 'zone_presence:%'")
    prev_states = {r["key"]: r["value"] for r in presence_rows}

    watchers_rows = await db.fetchall("SELECT key FROM settings WHERE key LIKE 'sell_pos_watch:%'")
    watching_keys = {r["key"] for r in watchers_rows}

    for u in users:
        uid = u["id"]
        tg_id = u["telegram_id"]
        nick = u["game_nick"]
        nick_lower = nick.strip().lower()

        p_key = f"zone_presence:{uid}"
        w_key = f"sell_pos_watch:{uid}"
        was_inside = prev_states.get(p_key) == "1"
        is_inside = nick_lower in inside_nicks

        if is_inside and not was_inside:
            await db.set_setting(p_key, "1")
            extra_hint = ""
            if w_key in watching_keys:
                await db.execute("DELETE FROM settings WHERE key = ?", (w_key,))
                extra_hint = (
                    "\n\n✅ <b>Ваши координаты подтверждены!</b> Теперь вы можете открыть "
                    "<b>«📦 Сдать лут» → «📋 Мой список сдачи»</b> и подтвердить заявку."
                )

            await _send_safe(
                bot=bot,
                chat_id=tg_id,
                text=(
                    f"🟢 <b>Вы вошли в торговую зону сдачи лута!</b>\n"
                    f"📍 Точка: <code>X: {drop_x} | Z: {drop_z}</code> (радиус <code>{drop_r} м</code>)"
                    f"{extra_hint}"
                ),
            )

        elif not is_inside and was_inside:
            await db.set_setting(p_key, "0")
            await _send_safe(
                bot=bot,
                chat_id=tg_id,
                text=(
                    f"🔴 <b>Вы покинули торговую зону сдачи лута.</b>\n"
                    f"Чтобы оформить или продолжить сдачу предметов, вернитесь в радиус "
                    f"<b>{drop_r} м</b> от точки <code>X: {drop_x} | Z: {drop_z}</code>."
                ),
            )

    return inside_nicks


async def _advance_queue_and_check_position(
    bot: Bot,
    db: Database,
    inside_nicks: set[str],
    drop_x: float,
    drop_z: float,
    drop_r: float,
) -> None:
    """Управляет очередью на вышку и проверяет координаты текущего первого игрока по последней записи .ADM."""
    occupant = await db.get_tower_occupant()

    # 1. Проверяем таймауты текущего игрока на вышке
    if occupant:
        status = occupant["status"]
        ref_time = occupant["active_since"] or occupant["created_at"]
        elapsed = _seconds_since(ref_time)

        if status == "checking_pos" and elapsed > POS_CHECK_TIMEOUT:
            await db.execute(
                """UPDATE sell_sessions
                   SET status = 'cancelled', finished_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (occupant["id"],),
            )
            await _send_safe(
                bot=bot,
                chat_id=occupant["telegram_id"],
                text=(
                    "⏰ <b>Время ожидания прибытия на точку сдачи истекло (15 мин).</b>\n"
                    "Ваша заявка отменена, чтобы не задерживать очередь. Вы можете создать новую в любое время."
                ),
            )
            occupant = None

        elif status == "ready_to_drop" and elapsed > TOWER_DROP_TIMEOUT:
            items_left = json.loads(occupant["items_json"])
            has_remaining = any(int(v.get("qty", 0)) > 0 for v in items_left.values())

            if has_remaining:
                await db.execute(
                    """UPDATE sell_sessions
                       SET status = 'waiting_cleanup', finished_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (occupant["id"],),
                )
                await _send_safe(
                    bot=bot,
                    chat_id=occupant["telegram_id"],
                    text=(
                        "🔄 <b>Время на вышке (20 мин) вышло — вышка освобождена для следующего игрока.</b>\n"
                        "Ваша заявка переведена в <b>фоновое ожидание</b>: как только вы уничтожите оставшиеся "
                        "предметы в зоне сдачи, выплата придёт автоматически."
                    ),
                )
            else:
                await db.execute(
                    """UPDATE sell_sessions
                       SET status = 'completed', finished_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (occupant["id"],),
                )
            occupant = None

    # 2. Если вышка свободна — вызываем следующего из очереди
    if not occupant:
        next_sess = await db.get_next_queued_session()
        if next_sess:
            await db.execute(
                """UPDATE sell_sessions
                   SET status = 'checking_pos', active_since = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (next_sess["id"],),
            )
            occupant = await db.fetchone("SELECT * FROM sell_sessions WHERE id = ?", (next_sess["id"],))
            await _send_safe(
                bot=bot,
                chat_id=occupant["telegram_id"],
                text=(
                    f"🔔 <b>Подошла ваша очередь на сдачу лута!</b>\n\n"
                    f"📍 Убедитесь, что вы находитесь в радиусе <b>{drop_r} м</b> от точки "
                    f"<code>X: {drop_x} | Z: {drop_z}</code>.\n"
                    f"Бот автоматически проверит ваши координаты по нику <code>{html.escape(occupant['game_nick'])}</code> "
                    f"и разрешит сдачу лута (у вас есть 15 минут)."
                ),
            )

    # 3. Если текущий игрок ожидает подтверждения координат — сверяем его ник с актуальным списком в зоне
    if occupant and occupant["status"] == "checking_pos":
        is_here = occupant["game_nick"].strip().lower() in inside_nicks
        if is_here:
            await db.execute(
                """UPDATE sell_sessions
                   SET status = 'ready_to_drop', active_since = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (occupant["id"],),
            )
            await _send_safe(
                bot=bot,
                chat_id=occupant["telegram_id"],
                text=(
                    "✅ <b>Ваша позиция на точке сдачи подтверждена!</b>\n\n"
                    "📦 Положите заявленные предметы в яму/зону сдачи и <b>уничтожьте их</b> "
                    "(выстрелите по ним, сломайте оружием ближнего боя или сожгите).\n"
                    "💰 Выплата будет приходить <b>по мере фиксации уничтожения каждого предмета</b> в логах.\n\n"
                    "Когда уничтожите все вещи, нажмите кнопку <b>«🏁 Я всё сбросил»</b> в меню «📦 Сдать лут», "
                    "чтобы освободить точку для следующих игроков."
                ),
            )
async def _cleanup_expired_background_sessions(bot: Bot, db: Database) -> None:
    """Закрывает фоновые заявки, по которым за 4 часа так и не зафиксировали сдачу."""
    sessions = await db.fetchall("SELECT * FROM sell_sessions WHERE status = 'waiting_cleanup'")
    for sess in sessions:
        ref = sess["finished_at"] or sess["active_since"] or sess["created_at"]
        if _seconds_since(ref) > BACKGROUND_WAIT_TIMEOUT:
            await db.execute(
                "UPDATE sell_sessions SET status = 'completed' WHERE id = ?",
                (sess["id"],),
            )
            await _send_safe(
                bot=bot,
                chat_id=sess["telegram_id"],
                text=(
                    "ℹ️ <b>Фоновое ожидание по заявке сдачи лута завершено (истекли 4 часа).</b>\n"
                    f"Всего выплачено по заявке: <b>{sess['paid_amount']} 🪙</b>."
                ),
            )


async def start_loot_drop_service(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    chat_id: int | None = None,
    thread_id: int | None = None,
    poll_interval: int = 60,
) -> None:
    """Фоновая служба очереди, верификации координат и поэтапной выплаты за уничтоженный лут."""
    logger.info("[LOOT_DROP] Служба автоматической сдачи лута запущена.")

    last_rpt_file = (await db.get_setting("last_loot_rpt_file")) or None
    last_rpt_hash = (await db.get_setting("last_loot_rpt_hash")) or None

    while True:
        try:
            drop_x, drop_z, drop_r = await db.get_sell_zone()

            # 0. Отслеживаем вход/выход игроков в торговую зону по самой последней записи в .ADM
            inside_nicks = await _track_trade_zone_presence(bot, db, nitrado, drop_x, drop_z, drop_r)

            # 1. Двигаем очередь и проверяем координаты первого игрока
            await _advance_queue_and_check_position(bot, db, inside_nicks, drop_x, drop_z, drop_r)

            # 2. Закрываем просроченные фоновые сессии
            await _cleanup_expired_background_sessions(bot, db)

            # 3. Проверяем уничтожение предметов в .ADM логе для активных и фоновых заявок
            watching_sessions = await db.get_cleanup_watching_sessions()
            sell_items = await db.get_sell_items(all_items=True)
            allowed_classnames = {row["game_classname"].lower() for row in sell_items}

            if allowed_classnames:
                cleanups, updated_file, updated_hash = await nitrado.get_new_rpt_cleanups(
                    center_x=drop_x,
                    center_z=drop_z,
                    radius=drop_r,
                    allowed_classnames=allowed_classnames,
                    last_rpt_file=last_rpt_file,
                    last_rpt_hash=last_rpt_hash,
                )

                if updated_file and (updated_file != last_rpt_file or updated_hash != last_rpt_hash):
                    last_rpt_file = updated_file
                    last_rpt_hash = updated_hash
                    await db.set_setting("last_loot_rpt_file", updated_file)
                    if updated_hash:
                        await db.set_setting("last_loot_rpt_hash", updated_hash)

                if cleanups and watching_sessions:
                    for item_ev in cleanups:
                        ev_class_lower = item_ev["classname"].lower()
                        ev_nick_lower = item_ev.get("nick", "").strip().lower()

                        # Ищем заявку именно того игрока, который уничтожил предмет
                        for sess in watching_sessions:
                            if ev_nick_lower and sess["game_nick"].strip().lower() != ev_nick_lower:
                                continue

                            items_dict: dict = json.loads(sess["items_json"])
                            matched_key = next(
                                (
                                    k for k, v in items_dict.items()
                                    if v.get("classname", "").lower() == ev_class_lower and int(v.get("qty", 0)) > 0
                                ),
                                None,
                            )
                            if not matched_key:
                                continue

                            entry = items_dict[matched_key]
                            reward = int(entry["price"])
                            display_name = entry["display_name"]
                            entry["qty"] = int(entry["qty"]) - 1

                            remaining_list = [
                                f"• {v['display_name']} × {v['qty']} шт."
                                for v in items_dict.values()
                                if int(v.get("qty", 0)) > 0
                            ]
                            is_all_done = len(remaining_list) == 0

                            new_status = "completed" if is_all_done else sess["status"]
                            new_paid = int(sess["paid_amount"]) + reward
                            new_json = json.dumps(items_dict, ensure_ascii=False)

                            async with db.connect() as conn:
                                await conn.execute("BEGIN IMMEDIATE")
                                await conn.execute(
                                    "UPDATE users SET balance = balance + ? WHERE id = ?",
                                    (reward, sess["user_id"]),
                                )
                                await conn.execute(
                                    "INSERT INTO transactions (receiver_id, amount, kind) VALUES (?, ?, 'sell_loot')",
                                    (sess["user_id"], reward),
                                )
                                if is_all_done:
                                    await conn.execute(
                                        """UPDATE sell_sessions
                                           SET items_json = ?, paid_amount = ?, status = 'completed',
                                               finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
                                           WHERE id = ?""",
                                        (new_json, new_paid, sess["id"]),
                                    )
                                else:
                                    await conn.execute(
                                        """UPDATE sell_sessions
                                           SET items_json = ?, paid_amount = ?
                                           WHERE id = ?""",
                                        (new_json, new_paid, sess["id"]),
                                    )
                                await conn.commit()

                            # Обновляем локальный снимок сессии в цикле
                            sess["items_json"] = new_json
                            sess["paid_amount"] = new_paid
                            sess["status"] = new_status

                            if is_all_done:
                                status_footer = (
                                    f"🎉 <b>Все предметы по заявке сданы!</b>\n"
                                    f"Итого выплачено: <b>{new_paid} 🪙</b>. Точка сдачи освобождена."
                                )
                            else:
                                status_footer = (
                                    f"⏳ <b>Ожидают сдачи:</b>\n" + "\n".join(remaining_list)
                                )

                            await _send_safe(
                                bot=bot,
                                chat_id=sess["telegram_id"],
                                text=(
                                    f"💰 <b>Засчитан предмет:</b> {html.escape(display_name)} (+<b>{reward} 🪙</b>)\n\n"
                                    f"{status_footer}"
                                ),
                            )
                            break

        except asyncio.CancelledError:
            logger.info("[LOOT_DROP] Служба автоматической сдачи лута остановлена.")
            raise
        except Exception as e:
            logger.error("[LOOT_DROP] Ошибка в цикле службы сдачи лута: %s", e, exc_info=True)

        await asyncio.sleep(poll_interval)
