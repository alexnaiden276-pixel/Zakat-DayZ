import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

from database.db import Database

logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))


def _plural(n: int, form1: str, form2: str, form5: str) -> str:
    """Склонение числительных на русском языке (1 убийство, 2 убийства, 5 убийств)."""
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 10 < n_abs < 20:
        return form5
    if 1 < n1 < 5:
        return form2
    if n1 == 1:
        return form1
    return form5


def _format_kd_ratio(kills: int, deaths: int) -> str:
    """Форматирует соотношение убийств на смерть с запятой (например: 2,7 убийства на смерть)."""
    ratio = kills / deaths
    rounded = round(ratio, 1)
    if rounded.is_integer():
        word = _plural(int(rounded), "убийство", "убийства", "убийств")
    else:
        last_digit = int(round(rounded * 10)) % 10
        word = _plural(last_digit, "убийство", "убийства", "убийств")
    ratio_str = f"{rounded:.1f}".replace(".", ",")
    return f"{ratio_str} {word} на смерть"


def _format_top_row(idx: int, nick: str, kills: int, deaths: int, with_kd: bool = True) -> str:
    """Формирует одну строку рейтинга в точности как в примере."""
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    prefix = medals.get(idx, f"{idx}.")
    safe_nick = html.escape(nick)

    kills_part = f"{kills} {_plural(kills, 'убийство', 'убийства', 'убийств')}"
    if deaths == 0:
        deaths_part = "без смертей"
        kd_part = ""
    else:
        deaths_part = f"{deaths} {_plural(deaths, 'смерть', 'смерти', 'смертей')}"
        kd_part = f" · {_format_kd_ratio(kills, deaths)}" if with_kd else ""

    return f"{prefix} {safe_nick} — {kills_part} · {deaths_part}{kd_part}"


def _get_period_bounds_msk(now_msk: datetime) -> tuple[datetime, datetime, bool]:
    """
    Возвращает (start_msk, end_msk, is_weekly_summary).
    Если сегодня понедельник (weekday == 0) — подводим итоги за ПРОШЛУЮ полную неделю (Пн–Вс).
    В остальные дни (Вт–Вс) — показываем ТЕКУЩУЮ неделю с понедельника 00:00 МСК.
    """
    today_midnight = datetime(now_msk.year, now_msk.month, now_msk.day, 0, 0, 0, tzinfo=MSK)
    weekday = now_msk.weekday()  # 0 = Понедельник, 6 = Воскресенье

    if weekday == 0:
        start_msk = today_midnight - timedelta(days=7)
        end_msk = today_midnight
        return start_msk, end_msk, True

    start_msk = today_midnight - timedelta(days=weekday)
    end_msk = start_msk + timedelta(days=7)
    return start_msk, end_msk, False
async def build_leaderboard_text(db: Database, now_msk: datetime | None = None) -> tuple[str, bool]:
    """
    Собирает готовый HTML-текст таблицы лидеров RUSSVET из таблицы pvp_kills.
    Возвращает (text, is_weekly_summary).
    """
    current_msk = now_msk or datetime.now(MSK)
    start_msk, end_msk, is_summary = _get_period_bounds_msk(current_msk)

    start_utc_str = start_msk.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc_str = end_msk.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    date_from_str = start_msk.strftime("%d.%m.%Y")
    date_to_str = (end_msk - timedelta(seconds=1)).strftime("%d.%m.%Y")

    # 1. Получаем все PvP-убийства за период из pvp_kills
    period_kills = await db.fetchall(
        """SELECT killer_nick, victim_nick, weapon, distance, is_headshot
           FROM pvp_kills
           WHERE created_at >= ? AND created_at < ?
             AND killer_nick IS NOT NULL AND victim_nick IS NOT NULL
             AND LOWER(killer_nick) != LOWER(victim_nick)""",
        (start_utc_str, end_utc_str),
    )

    weekly_stats: dict[str, dict] = {}
    participants: set[str] = set()
    headshots_by_player: dict[str, int] = {}

    for row in period_kills:
        killer = str(row["killer_nick"]).strip()
        victim = str(row["victim_nick"]).strip()
        k_key = killer.lower()
        v_key = victim.lower()

        participants.add(k_key)
        participants.add(v_key)

        if k_key not in weekly_stats:
            weekly_stats[k_key] = {"nick": killer, "kills": 0, "deaths": 0}
        weekly_stats[k_key]["nick"] = killer
        weekly_stats[k_key]["kills"] += 1

        if v_key not in weekly_stats:
            weekly_stats[v_key] = {"nick": victim, "kills": 0, "deaths": 0}
        weekly_stats[v_key]["deaths"] += 1

        if int(row["is_headshot"] or 0) == 1:
            headshots_by_player[killer] = headshots_by_player.get(killer, 0) + 1

    killers_sorted = sorted(
        [s for s in weekly_stats.values() if s["kills"] > 0],
        key=lambda x: (-x["kills"], x["deaths"], x["nick"].lower()),
    )

    top_10_weekly = killers_sorted[:10]
    extra_killers_count = max(0, len(killers_sorted) - 10)

    if top_10_weekly:
        weekly_lines = [
            _format_top_row(i, p["nick"], p["kills"], p["deaths"], with_kd=True)
            for i, p in enumerate(top_10_weekly, start=1)
        ]
        if extra_killers_count > 0:
            weekly_lines.append(f"Ещё участников с убийствами: {extra_killers_count}")
        weekly_block = "\n".join(weekly_lines)
    else:
        weekly_block = "<i>За этот период PvP-убийств пока не зафиксировано.</i>"

    # 2. Топ-3 дальних попадания недели
    long_shots = await db.fetchall(
        """SELECT killer_nick, weapon, CAST(distance AS REAL) AS dist
           FROM pvp_kills
           WHERE created_at >= ? AND created_at < ?
             AND killer_nick IS NOT NULL AND victim_nick IS NOT NULL
             AND LOWER(killer_nick) != LOWER(victim_nick)
             AND CAST(distance AS REAL) > 0
           ORDER BY dist DESC
           LIMIT 3""",
        (start_utc_str, end_utc_str),
    )

    if long_shots:
        ls_lines = []
        for idx, shot in enumerate(long_shots, start=1):
            nick = html.escape(str(shot["killer_nick"]).strip())
            dist_m = int(round(float(shot["dist"])))
            weapon = html.escape(str(shot["weapon"] or "Неизвестно").strip())
            ls_lines.append(f"{idx}. {nick} — {dist_m} м · {weapon}")
        longshot_block = "\n".join(ls_lines)
    else:
        longshot_block = "<i>Нет зафиксированных дальних попаданий.</i>"

    # Лидер по хедшотам за неделю
    if headshots_by_player:
        best_hs_nick, best_hs_cnt = max(headshots_by_player.items(), key=lambda x: x[1])
        hs_word = _plural(best_hs_cnt, "попадание", "попадания", "попаданий")
        hs_line = f"🎯 Убийства в голову: {html.escape(best_hs_nick)} — {best_hs_cnt} {hs_word}"
    else:
        hs_line = "🎯 Убийства в голову: <i>пока нет</i>"

    # 3. Общий рейтинг сервера за всё время
    all_kills = await db.fetchall(
        """SELECT killer_nick, victim_nick
           FROM pvp_kills
           WHERE killer_nick IS NOT NULL AND victim_nick IS NOT NULL
             AND LOWER(killer_nick) != LOWER(victim_nick)"""
    )
    overall_stats: dict[str, dict] = {}
    for row in all_kills:
        killer = str(row["killer_nick"]).strip()
        victim = str(row["victim_nick"]).strip()
        k_key = killer.lower()
        v_key = victim.lower()

        if k_key not in overall_stats:
            overall_stats[k_key] = {"nick": killer, "kills": 0, "deaths": 0}
        overall_stats[k_key]["nick"] = killer
        overall_stats[k_key]["kills"] += 1

        if v_key not in overall_stats:
            overall_stats[v_key] = {"nick": victim, "kills": 0, "deaths": 0}
        overall_stats[v_key]["deaths"] += 1

    overall_sorted = sorted(
        [s for s in overall_stats.values() if s["kills"] > 0],
        key=lambda x: (-x["kills"], x["deaths"], x["nick"].lower()),
    )[:10]

    if overall_sorted:
        overall_block = "\n".join(
            _format_top_row(i, p["nick"], p["kills"], p["deaths"], with_kd=False)
            for i, p in enumerate(overall_sorted, start=1)
        )
    else:
        overall_block = "<i>Статистика сервера пока пуста.</i>"

    total_pvp = len(period_kills)
    total_part = len(participants)
    pvp_word = _plural(total_pvp, "PvP-убийство", "PvP-убийства", "PvP-убийств")
    part_word = _plural(total_part, "участник", "участника", "участников")

    if is_summary:
        header_sub = "🏁 <b>Итоги прошедшей недели</b> — обновляется каждый день в 12:00 МСК, итоги — в понедельник."
    else:
        header_sub = "Текущая неделя — обновляется каждый день в 12:00 МСК, итоги — в понедельник."

    text = (
        f"🎮 <b>RUSSVET</b>\n"
        f"{header_sub}\n\n"
        f"🏆 <b>Лидеры RUSSVET</b>\n"
        f"📅 {date_from_str} – {date_to_str} (МСК)\n\n"
        f"⚔️ <b>Бойцы недели</b>\n"
        f"{weekly_block}\n\n"
        f"🎯 <b>Дальние попадания недели</b>\n"
        f"{longshot_block}\n"
        f"{hs_line}\n\n"
        f"🌐 <b>Общий рейтинг сервера</b>\n"
        f"{overall_block}\n\n"
        f"📊 За неделю: {total_pvp} {pvp_word} · {total_part} {part_word}\n"
        f"ℹ️ При равенстве убийств выше игрок с меньшим числом PvP-смертей."
    )
    return text, is_summary


async def publish_leaderboard(
    bot: Bot,
    db: Database,
    chat_id: int,
    thread_id: int | None = None,
    force_new_message: bool = False,
) -> None:
    """Публикует или обновляет сообщение с таблицей лидеров в теме группы."""
    text, is_summary = await build_leaderboard_text(db)

    saved_msg_id_raw = await db.get_setting("leaderboard_weekly_msg_id")
    saved_msg_id = int(saved_msg_id_raw) if saved_msg_id_raw and saved_msg_id_raw.isdigit() else None

    if saved_msg_id and not force_new_message:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=saved_msg_id,
                text=text,
                parse_mode="HTML",
            )
            if is_summary:
                await db.set_setting("leaderboard_weekly_msg_id", "")
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except TelegramAPIError as e:
            err = str(e).lower()
            if "message is not modified" in err:
                if is_summary:
                    await db.set_setting("leaderboard_weekly_msg_id", "")
                return
            logger.warning("[LEADERBOARD] Не удалось отредактировать пост (%s), отправляем новый...", e)

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
        )
        if is_summary:
            await db.set_setting("leaderboard_weekly_msg_id", "")
        else:
            await db.set_setting("leaderboard_weekly_msg_id", str(msg.message_id))
    except Exception as send_err:
        logger.error("[LEADERBOARD] Ошибка отправки таблицы лидеров: %s", send_err, exc_info=True)


async def start_leaderboard_service(
    bot: Bot,
    db: Database,
    chat_id: int | None,
    thread_id: int | None = None,
) -> None:
    """Фоновая служба ежедневной публикации лидеров в 12:00 МСК."""
    if not chat_id:
        logger.warning("[LEADERBOARD] LEADERBOARD_CHAT_ID не задан — служба таблицы лидеров отключена.")
        return

    logger.info("[LEADERBOARD] Служба лидеров RUSSVET запущена (chat=%s, thread=%s).", chat_id, thread_id)

    while True:
        try:
            now_msk = datetime.now(MSK)
            today_str = now_msk.strftime("%Y-%m-%d")
            last_run_date = await db.get_setting("leaderboard_last_run_date")

            if now_msk.hour >= 12 and last_run_date != today_str:
                logger.info("[LEADERBOARD] Время 12:00 МСК — обновляем таблицу лидеров за %s...", today_str)
                await publish_leaderboard(bot=bot, db=db, chat_id=chat_id, thread_id=thread_id)
                await db.set_setting("leaderboard_last_run_date", today_str)

        except asyncio.CancelledError:
            logger.info("[LEADERBOARD] Служба таблицы лидеров остановлена.")
            raise
        except Exception as e:
            logger.error("[LEADERBOARD] Ошибка в цикле службы лидеров: %s", e, exc_info=True)

        await asyncio.sleep(30)

