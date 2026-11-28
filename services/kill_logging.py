import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database
from services.hotzone_service import HotzoneService
from services.nitrado_api import NitradoAPI
from utils.ui import notify_balance_change

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))
SERVER_TZ = ZoneInfo("Europe/Berlin")  # Франкфурт (автоучёт зимнего/летнего времени)
SERVER_NAME_CACHE_TTL = 1800  # Кэшируем имя сервера на 30 минут

ADM_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _convert_kill_dt_to_msk(time_str: str, file_name: str | None) -> tuple[str, str]:
    """Переводит время и дату убийства из часового пояса сервера (Франкфурт) в МСК."""
    now_msk = datetime.now(MSK)
    try:
        h, m, s = map(int, time_str.split(":"))
        base_date = now_msk.astimezone(SERVER_TZ).date()

        if file_name:
            m_date = ADM_DATE_PATTERN.search(file_name)
            if m_date:
                base_date = datetime.strptime(m_date.group(1), "%Y-%m-%d").date()

        server_dt = datetime(
            base_date.year,
            base_date.month,
            base_date.day,
            h,
            m,
            s,
            tzinfo=SERVER_TZ,
        )
        # Если файл создан до полуночи (например, 23:50), а убийство произошло после (00:15)
        if file_name and server_dt.hour < 4:
            m_time = re.search(r"\d{4}-\d{2}-\d{2}_(\d{2})-", file_name)
            if m_time and int(m_time.group(1)) >= 20:
                server_dt += timedelta(days=1)

        msk_dt = server_dt.astimezone(MSK)
        return msk_dt.strftime("%H:%M:%S"), msk_dt.strftime("%Y-%m-%d")
    except Exception:
        return time_str, now_msk.strftime("%Y-%m-%d")


async def _send_telegram_safe(
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    text: str,
) -> bool:
    """Безопасная отправка сообщения с обработкой флуд-контроля."""
    for _ in range(2):
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
            )
            return True
        except TelegramRetryAfter as retry_err:
            logger.warning("[KILL_LOG] Флуд-контроль ТГ, ждём %s сек...", retry_err.retry_after)
            await asyncio.sleep(retry_err.retry_after + 0.5)
        except TelegramAPIError as tg_err:
            logger.error("[KILL_LOG] Ошибка Telegram API при отправке килл-лога: %s", tg_err)
            return False
        except Exception as err:
            logger.error("[KILL_LOG] Непредвиденная ошибка при отправке килл-лога: %s", err)
            return False
    return False


def _calculate_kill_reward(
    victim_rank: int | None,
    victim_streak: int,
    eco: dict[str, int],
) -> tuple[int, str, bool]:
    """
    Возвращает (награда, титул_жертвы, это_топ_цель) на основе актуального конфига экономики.
    """
    is_top3 = victim_rank is not None and 1 <= victim_rank <= 3
    if is_top3 or victim_streak >= 10:
        return eco["eco_kill_tier4"], "Цель №1", True

    if (victim_rank is not None and 4 <= victim_rank <= 10) or 6 <= victim_streak <= 9:
        return eco["eco_kill_tier3"], "Ветеран", False

    if (victim_rank is not None and 11 <= victim_rank <= 20) or 3 <= victim_streak <= 5:
        return eco["eco_kill_tier2"], "Опытный боец", False

    return eco["eco_kill_tier1"], "Обычный выживший", False


def _get_hotzone_kill_mult(hotzone_service: HotzoneService | None, kill: dict) -> int:
    """Безопасно вычисляет максимальный множитель Опасной зоны по координатам убийцы и жертвы."""
    if hotzone_service is None:
        return 1

    hz_mult = 1
    for key_x, key_z in (("killer_x", "killer_z"), ("victim_x", "victim_z")):
        raw_x, raw_z = kill.get(key_x), kill.get(key_z)
        if raw_x is None or raw_z is None:
            continue
        try:
            mult = int(round(hotzone_service.get_kill_multiplier(float(raw_x), float(raw_z))))
            if mult > hz_mult:
                hz_mult = mult
        except (TypeError, ValueError):
            continue
    return hz_mult


async def _record_battle_stats(
    db: Database,
    killer_raw: str,
    victim_raw: str,
    weapon_raw: str,
    dist: float,
    is_headshot: bool,
) -> None:
    """Атомарно обновляет боевую статистику игроков и записывает факт убийства в pvp_kills."""
    async with db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "UPDATE users SET kills = COALESCE(kills, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                (killer_raw,),
            )
            await conn.execute(
                """
                UPDATE users
                SET max_kill_distance = ?
                WHERE game_nick = ? COLLATE NOCASE
                  AND COALESCE(max_kill_distance, 0) < ?
                """,
                (dist, killer_raw, dist),
            )
            if is_headshot:
                await conn.execute(
                    "UPDATE users SET headshots = COALESCE(headshots, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                    (killer_raw,),
                )
            await conn.execute(
                "UPDATE users SET deaths = COALESCE(deaths, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                (victim_raw,),
            )
            await conn.execute(
                """INSERT INTO pvp_kills (killer_nick, victim_nick, weapon, distance, is_headshot)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    killer_raw,
                    victim_raw,
                    weapon_raw,
                    dist,
                    1 if is_headshot else 0,
                ),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
async def start_kill_logging(
    bot: Bot,
    db: Database,
    nitrado: NitradoAPI,
    chat_id: int,
    thread_id: int | None = None,
    poll_interval: int = 60,
    hotzone_service: HotzoneService | None = None,
) -> None:
    """Фовое отслеживание убийств и смертей по логам DayZ с расчётом экономики и Опасных зон."""
    logger.info("[KILL_LOG] Задача логирования убийств запущена.")

    last_file_name = (await db.get_setting("last_kill_log_file")) or None
    last_line_hash = (await db.get_setting("last_kill_log_hash")) or None

    cached_server_name = "RUSSVET"
    last_name_fetch = 0.0

    while True:
        try:
            new_kills, updated_file, updated_hash = await nitrado.get_new_kills(
                last_file_name=last_file_name,
                last_line_hash=last_line_hash,
            )

            messages_to_send: list[str] = []
            pm_notifications: list[tuple[int, int, str, int]] = []

            if new_kills:
                # Обновляем название сервера не чаще раза в 30 минут
                now_mono = time.monotonic()
                if now_mono - last_name_fetch > SERVER_NAME_CACHE_TTL:
                    server_status = await nitrado.get_server_status()
                    if server_status:
                        raw_name = (
                            server_status.get("query", {}).get("server_name")
                            or cached_server_name
                        )
                        cached_server_name = html.escape(str(raw_name))
                        last_name_fetch = now_mono

                # Загружаем актуальные настройки экономики из БД
                eco = await db.get_economy_config()

                for kill in new_kills:
                    event_type = kill.get("event_type", "pvp_kill")
                    killer_raw = str(kill["killer"]).strip()
                    victim_raw = str(kill["victim"]).strip()
                    weapon_raw = str(kill.get("weapon") or "").strip()
                    killer_safe = html.escape(killer_raw)
                    victim_safe = html.escape(victim_raw)
                    weapon_safe = html.escape(weapon_raw) if weapon_raw else "неизвестного оружия"
                    is_headshot = bool(kill.get("is_headshot", False))

                    # 1. Если это PvE-смерть или суицид (без убийцы-игрока) — только штраф и сброс серии
                    if event_type == "pve_death" or killer_raw.lower() == victim_raw.lower():
                        try:
                            victim_before = await db.get_user_by_nick(victim_raw)
                            bal_before = int(victim_before["balance"] or 0) if victim_before else 0

                            await db.execute(
                                "UPDATE users SET deaths = COALESCE(deaths, 0) + 1 WHERE game_nick = ? COLLATE NOCASE",
                                (victim_raw,),
                            )
                            await db.apply_death_penalty(
                                victim_raw,
                                penalty=eco["eco_death_penalty"],
                                floor=eco["eco_debt_floor"],
                            )

                            if victim_before and victim_before["telegram_id"]:
                                victim_after = await db.get_user_by_nick(victim_raw)
                                bal_after = int(victim_after["balance"] or 0) if victim_after else bal_before
                                actual_loss = max(0, bal_before - bal_after)
                                if actual_loss > 0:
                                    pm_notifications.append((
                                        int(victim_before["telegram_id"]),
                                        -actual_loss,
                                        "Смерть на сервере (PvE / суицид)",
                                        bal_after,
                                    ))
                        except Exception as pve_err:
                            logger.error("[KILL_LOG] Ошибка обработки PvE-смерти в БД: %s", pve_err)
                        continue
                    # 2. Обработка PvP-убийства
                    try:
                        dist = float(kill.get("distance") or 0.0)
                    except (TypeError, ValueError):
                        dist = 0.0

                    reward_line = ""
                    bounty_announcement = None

                    try:
                        victim_before = await db.get_user_by_nick(victim_raw)
                        victim_bal_before = int(victim_before["balance"] or 0) if victim_before else 0

                        # Получаем ранг и серию киллов жертвы ДО обнуления
                        victim_rank, victim_streak = await db.get_player_rank_and_streak(victim_raw)

                        # Списываем штраф с жертвы (не ниже eco_debt_floor) и сбрасываем её серию
                        await db.apply_death_penalty(
                            victim_raw,
                            penalty=eco["eco_death_penalty"],
                            floor=eco["eco_debt_floor"],
                        )

                        # Уведомление погибшему в ЛС
                        if victim_before and victim_before["telegram_id"]:
                            victim_after = await db.get_user_by_nick(victim_raw)
                            victim_bal_after = int(victim_after["balance"] or 0) if victim_after else victim_bal_before
                            actual_loss = max(0, victim_bal_before - victim_bal_after)
                            if actual_loss > 0:
                                pm_notifications.append((
                                    int(victim_before["telegram_id"]),
                                    -actual_loss,
                                    f"Смерть в бою от игрока {killer_raw}",
                                    victim_bal_after,
                                ))

                        # Проверяем кулдаун на повторное убийство одного и того же игрока
                        is_farm_abuse = await db.has_recent_kill_pair(
                            killer_raw,
                            victim_raw,
                            hours=eco["eco_kill_cooldown_hrs"],
                        )
                        if is_farm_abuse:
                            reward_amount = 0
                            tier_title = f"Кулдаун {eco['eco_kill_cooldown_hrs']}ч"
                            is_top_target = False
                            hz_mult = 1
                        else:
                            reward_amount, tier_title, is_top_target = _calculate_kill_reward(
                                victim_rank,
                                victim_streak,
                                eco,
                            )
                            hz_mult = _get_hotzone_kill_mult(hotzone_service, kill)
                            if hz_mult > 1:
                                reward_amount *= hz_mult
                                tier_title = f"{tier_title} • ☢️ Опасная зона x{hz_mult}"

                        # Начисляем награду убийце и увеличиваем серию киллов
                        new_streak = await db.reward_pvp_kill(killer_raw, reward_amount)

                        # Уведомление убийце в ЛС (если награда > 0)
                        killer_after = await db.get_user_by_nick(killer_raw)
                        if killer_after and killer_after["telegram_id"] and reward_amount > 0:
                            killer_bal_after = int(killer_after["balance"] or 0)
                            pm_notifications.append((
                                int(killer_after["telegram_id"]),
                                reward_amount,
                                f"Убийство игрока {victim_raw} ({tier_title} | Серия: {new_streak})",
                                killer_bal_after,
                            ))

                        if reward_amount > 0:
                            reward_line = f"\n💰 <b>Награда:</b> +{reward_amount} 🪙 ({tier_title} | Серия: {new_streak})"
                        else:
                            reward_line = f"\n⚠️ <b>Награда:</b> 0 🪙 ({tier_title})"

                        if is_top_target and reward_amount > 0:
                            rank_info = f"Топ-{victim_rank}" if victim_rank else f"{victim_streak} киллов подряд"
                            zone_badge = " в <b>Опасной зоне</b> ☢️" if hz_mult > 1 else ""
                            bounty_announcement = (
                                f"🎯 <b>ОХОТА ЗАВЕРШЕНА!</b>\n"
                                f"Игрок <code>{killer_safe}</code> устранил лидера топа "
                                f"<code>{victim_safe}</code> ({rank_info}){zone_badge} и сорвал куш <b>{reward_amount} монет</b>! 💰"
                            )

                        # Атомарно обновляем статистику и записываем PvP-убийство в историю
                        await _record_battle_stats(
                            db=db,
                            killer_raw=killer_raw,
                            victim_raw=victim_raw,
                            weapon_raw=weapon_raw,
                            dist=dist,
                            is_headshot=is_headshot,
                        )
                    except Exception as db_err:
                        logger.error("[KILL_LOG] Ошибка обновления статистики и экономики убийства: %s", db_err)

                    # 3. Переводим время в МСК и форматируем дистанцию
                    kill_file = kill.get("file_name") or last_file_name
                    kill_time_msk, kill_date_msk = _convert_kill_dt_to_msk(
                        str(kill["time"]),
                        kill_file,
                    )
                    dist_int = int(round(dist))
                    dist_str = f" с <code>{dist_int} м</code>" if dist_int > 0 else " (в упор)"

                    # 4. Формируем сообщения для отправки в Telegram
                    headshot_badge = "\n🎯 <b>(В ГОЛОВУ!)</b>" if is_headshot else ""
                    message_text = (
                        f"🎮 <b>{cached_server_name}</b>\n"
                        f"Произошло убийство 💀{headshot_badge}\n"
                        f"<code>{kill_time_msk} {kill_date_msk} МСК</code>\n"
                        f"👤 <b>Игрок:</b> <code>{killer_safe}</code>\n"
                        f"⚰️ <b>Убил игрока:</b> <code>{victim_safe}</code>\n"
                        f"🔫 из <b>{weapon_safe}</b>{dist_str}{reward_line}"
                    )
                    messages_to_send.append(message_text)

                    if bounty_announcement:
                        messages_to_send.append(bounty_announcement)

            # Фиксируем курсор файла и хэша в БД ДО отправки в ТГ, чтобы исключить повторное начисление монет
            if updated_file and (updated_file != last_file_name or updated_hash != last_line_hash):
                last_file_name = updated_file
                await db.set_setting("last_kill_log_file", last_file_name)

                last_line_hash = updated_hash
                await db.set_setting("last_kill_log_hash", last_line_hash or "")

            # Отправляем накопленные сообщения в публичный канал
            for msg_text in messages_to_send:
                await _send_telegram_safe(
                    bot=bot,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    text=msg_text,
                )

            # Отправляем персональные уведомления игрокам в ЛС
            for target_tg_id, delta, reason, new_bal in pm_notifications:
                await notify_balance_change(
                    bot=bot,
                    telegram_id=target_tg_id,
                    delta=delta,
                    reason=reason,
                    new_balance=new_bal,
                )
                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            logger.info("[KILL_LOG] Задача логирования убийств остановлена.")
            raise
        except Exception as e:
            logger.error("[KILL_LOG] Ошибка в цикле мониторинга убийств: %s", e, exc_info=True)

        await asyncio.sleep(poll_interval)
