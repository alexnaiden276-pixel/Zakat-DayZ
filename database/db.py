import contextlib
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)
MSK_TZ = timezone(timedelta(hours=3))

# Значения экономики по умолчанию (можно менять в админ-панели бота)
DEFAULT_ECONOMY_CONFIG: dict[str, int] = {
    "eco_start_bonus": 100,       # Стартовый баланс при регистрации
    "eco_online_reward": 15,      # Награда за интервал онлайна
    "eco_daily_bonus": 50,        # Ежедневный бонус раз в сутки (по МСК)
    "eco_death_penalty": 25,      # Штраф за любую смерть
    "eco_debt_floor": -300,       # Максимальный уход в минус
    "eco_kill_tier1": 40,         # Обычный выживший (серия 0-2)
    "eco_kill_tier2": 75,         # Опытный боец (серия 3-5 или Топ 11-20)
    "eco_kill_tier3": 120,        # Ветеран (серия 6-9 или Топ 4-10)
    "eco_kill_tier4": 200,        # Цель №1 (серия 10+ или Топ 1-3)
    "eco_kill_cooldown_hrs": 3,   # Защита от фарма одного игрока (часов)

    # Настройки Опасных зон (Кровавый час)
    "eco_hotzone_enabled": 1,        # Автозапуск (1 = вкл, 0 = выкл)
    "eco_hotzone_salary_mult": 3,    # Множитель онлайна внутри зоны (x3)
    "eco_hotzone_kill_mult": 2,      # Множитель награды за убийство в зоне (x2)
    "eco_hotzone_duration_min": 60,  # Длительность ивента в минутах
    "eco_hotzone_announce_min": 15,  # За сколько минут предупреждать до старта
    "eco_hotzone_min_online": 0,     # Мин. онлайн на сервере для старта (0 = любой)
}

DEFAULT_HOTZONE_PRESETS: list[tuple[str, float, float, float]] = [
    ("Северо-Западный Аэродром (СЗ)", 4550.0, 10250.0, 600.0),
    ("Тисы (Военная база)", 1650.0, 14150.0, 500.0),
    ("Зеленогорск (Военка)", 2750.0, 5300.0, 450.0),
    ("Старый Собор", 6050.0, 7750.0, 400.0),
    ("Военная база Павлово", 2150.0, 3350.0, 400.0),
    ("Выбор / Кабанино", 5300.0, 8600.0, 400.0),
]

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    photo_file_id TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    tag TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    leader_id INTEGER NOT NULL,
    flag_name TEXT DEFAULT 'Без флага',
    topic_message_id INTEGER,
    topic_has_photo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    game_nick TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL DEFAULT 'player',
    group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    kills INTEGER NOT NULL DEFAULT 0,
    deaths INTEGER NOT NULL DEFAULT 0,
    headshots INTEGER NOT NULL DEFAULT 0,
    max_kill_distance REAL NOT NULL DEFAULT 0,
    killstreak INTEGER NOT NULL DEFAULT 0,
    last_daily_bonus TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shop_subcategories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE(category, name)
);

CREATE TABLE IF NOT EXISTS shop_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    subcategory_id INTEGER REFERENCES shop_subcategories(id) ON DELETE SET NULL,
    display_name TEXT NOT NULL,
    game_classname TEXT NOT NULL,
    price INTEGER NOT NULL CHECK(price >= 0),
    photo_file_id TEXT
);

CREATE TABLE IF NOT EXISTS shop_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_id INTEGER NOT NULL,
    items_summary TEXT NOT NULL,
    x REAL NOT NULL,
    z REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    receiver_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    amount INTEGER NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    sender_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    admin_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pvp_kills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    killer_nick TEXT NOT NULL,
    victim_nick TEXT NOT NULL,
    weapon TEXT,
    distance REAL NOT NULL DEFAULT 0,
    is_headshot INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS autoban_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    x REAL NOT NULL,
    z REAL NOT NULL,
    radius REAL NOT NULL CHECK(radius > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hotzone_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    x REAL NOT NULL,
    z REAL NOT NULL,
    radius REAL NOT NULL CHECK(radius > 0),
    is_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS hotzone_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    preset_id INTEGER NOT NULL REFERENCES hotzone_presets(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'announced',
    salary_mult REAL NOT NULL DEFAULT 3.0,
    kill_mult REAL NOT NULL DEFAULT 2.0,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pvp_kills_created_at ON pvp_kills(created_at);
CREATE INDEX IF NOT EXISTS idx_pvp_kills_pair ON pvp_kills(killer_nick, victim_nick, created_at);
CREATE INDEX IF NOT EXISTS idx_shop_orders_status ON shop_orders(status);
CREATE INDEX IF NOT EXISTS idx_hotzone_events_status ON hotzone_events(status);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    @contextlib.asynccontextmanager
    async def connect(self):
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON;")
        await conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield conn
        finally:
            await conn.close()
    async def init(self) -> None:
        async with self.connect() as db:
            await db.executescript(SCHEMA)
            await self._migrate_legacy_tables(db)

            # Наполняем базовые настройки экономики, если их ещё нет
            for key, default_val in DEFAULT_ECONOMY_CONFIG.items():
                await db.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, str(default_val)),
                )

            # Наполняем базовые локации Опасных зон при первом запуске
            cur = await db.execute("SELECT COUNT(*) AS cnt FROM hotzone_presets")
            row = await cur.fetchone()
            if row and int(row["cnt"]) == 0:
                await db.executemany(
                    "INSERT INTO hotzone_presets (name, x, z, radius, is_enabled) VALUES (?, ?, ?, ?, 1)",
                    DEFAULT_HOTZONE_PRESETS,
                )

            # Базовый набор флагов при первом запуске
            cur_f = await db.execute("SELECT COUNT(*) AS cnt FROM group_flags")
            row_f = await cur_f.fetchone()
            if row_f and int(row_f["cnt"]) == 0:
                default_flags = ["Красный", "Синий", "Зелёный", "Жёлтый", "Чёрный", "Белый"]
                await db.executemany(
                    "INSERT OR IGNORE INTO group_flags (name, photo_file_id) VALUES (?, NULL)",
                    [(name,) for name in default_flags],
                )

            await db.commit()

    async def _migrate_legacy_tables(self, db: aiosqlite.Connection) -> None:
        """Мягкое обновление старых таблиц без потери данных."""
        async def _get_columns(table: str) -> set[str]:
            cur = await db.execute(f"PRAGMA table_info({table})")
            rows = await cur.fetchall()
            return {r["name"] for r in rows}

        user_cols = await _get_columns("users")
        if "headshots" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN headshots INTEGER NOT NULL DEFAULT 0")
        if "max_kill_distance" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN max_kill_distance REAL NOT NULL DEFAULT 0")
        if "killstreak" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN killstreak INTEGER NOT NULL DEFAULT 0")
        if "last_daily_bonus" not in user_cols:
            await db.execute("ALTER TABLE users ADD COLUMN last_daily_bonus TEXT")

        group_cols = await _get_columns("groups")
        if "flag_name" not in group_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN flag_name TEXT DEFAULT 'Без флага'")
        if "topic_message_id" not in group_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN topic_message_id INTEGER")
        if "topic_has_photo" not in group_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN topic_has_photo INTEGER NOT NULL DEFAULT 0")
        if "updated_at" not in group_cols:
            await db.execute("ALTER TABLE groups ADD COLUMN updated_at TEXT")
            await db.execute("UPDATE groups SET updated_at = created_at WHERE updated_at IS NULL")

        item_cols = await _get_columns("shop_items")
        if "subcategory_id" not in item_cols:
            await db.execute(
                "ALTER TABLE shop_items ADD COLUMN subcategory_id INTEGER REFERENCES shop_subcategories(id) ON DELETE SET NULL"
            )
        if "photo_file_id" not in item_cols:
            await db.execute("ALTER TABLE shop_items ADD COLUMN photo_file_id TEXT")

    async def execute(self, query: str, params: tuple = ()) -> int:
        async with self.connect() as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.lastrowid or 0

    async def fetchone(self, query: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self.connect() as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self.connect() as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchall()
    # ==================== НАСТРОЙКИ И КОНФИГ ЭКОНОМИКИ ====================

    async def get_setting(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )

    async def get_economy_config(self) -> dict[str, int]:
        cfg = dict(DEFAULT_ECONOMY_CONFIG)
        rows = await self.fetchall("SELECT key, value FROM settings WHERE key LIKE 'eco_%'")
        for r in rows:
            k = str(r["key"])
            if k in cfg:
                try:
                    cfg[k] = int(r["value"])
                except (TypeError, ValueError):
                    pass
        return cfg

    async def update_economy_setting(self, key: str, value: int) -> None:
        if key not in DEFAULT_ECONOMY_CONFIG:
            raise ValueError(f"Неизвестный параметр экономики: {key}")
        await self.set_setting(key, str(int(value)))

    async def get_hotzone_hours(self) -> list[int]:
        raw = await self.get_setting("hotzone_prime_hours")
        if not raw:
            return [16, 19, 22]
        hours = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                h = int(part)
                if 0 <= h <= 23 and h not in hours:
                    hours.append(h)
        return sorted(hours) if hours else [16, 19, 22]

    async def set_hotzone_hours(self, hours: list[int]) -> None:
        clean = sorted({int(h) for h in hours if 0 <= int(h) <= 23})
        if not clean:
            clean = [19]
        await self.set_setting("hotzone_prime_hours", ",".join(str(h) for h in clean))
    # ==================== ПОЛЬЗОВАТЕЛИ ====================

    async def get_user(self, telegram_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))

    async def get_user_by_nick(self, game_nick: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM users WHERE game_nick = ? COLLATE NOCASE",
            (game_nick.strip(),),
        )

    async def create_user(self, telegram_id: int, game_nick: str, role: str = "player") -> int:
        eco = await self.get_economy_config()
        start_bonus = int(eco.get("eco_start_bonus", DEFAULT_ECONOMY_CONFIG["eco_start_bonus"]))
        return await self.execute(
            """INSERT INTO users (telegram_id, game_nick, role, balance)
               VALUES (?, ?, ?, ?)""",
            (telegram_id, game_nick.strip(), role, start_bonus),
        )

    async def wipe_all_players_data(self) -> tuple[int, int]:
        """
        Обнуляет статистику, лидерборд (pvp_kills) и балансы всех игроков
        при полном вайпе сервера, начисляя стартовый бонус.
        """
        eco = await self.get_economy_config()
        start_bonus = int(eco.get("eco_start_bonus", DEFAULT_ECONOMY_CONFIG["eco_start_bonus"]))

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    """
                    UPDATE users
                    SET balance = ?,
                        kills = 0,
                        deaths = 0,
                        headshots = 0,
                        max_kill_distance = 0,
                        killstreak = 0
                    """,
                    (start_bonus,),
                )
                affected = cur.rowcount or 0
                await db.execute("DELETE FROM pvp_kills")
                await db.commit()
                return affected, start_bonus
            except Exception:
                await db.rollback()
                raise
    # ==================== ЭКОНОМИКА И ТРАНЗАКЦИИ ====================

    async def claim_daily_bonus(self, telegram_id: int) -> tuple[bool, str]:
        today_msk = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
        eco = await self.get_economy_config()
        bonus = int(eco.get("eco_daily_bonus", DEFAULT_ECONOMY_CONFIG["eco_daily_bonus"]))

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT id, last_daily_bonus FROM users WHERE telegram_id = ?",
                    (telegram_id,),
                )
                user = await cur.fetchone()
                if not user:
                    await db.rollback()
                    return False, "Профиль не найден. Нажмите /start."

                if user["last_daily_bonus"] == today_msk:
                    await db.rollback()
                    return False, "Вы уже забирали ежедневный бонус сегодня (обновляется в 00:00 МСК)."

                await db.execute(
                    "UPDATE users SET balance = balance + ?, last_daily_bonus = ? WHERE id = ?",
                    (bonus, today_msk, user["id"]),
                )
                await db.execute(
                    "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (NULL, ?, ?, 'daily_bonus')",
                    (user["id"], bonus),
                )
                await db.commit()
                return True, f"Ежедневный бонус +{bonus} 🪙 начислен!"
            except Exception:
                await db.rollback()
                raise

    async def transfer(self, sender_db_id: int, target_nick: str, amount: int) -> tuple[bool, str]:
        if amount <= 0:
            return False, "Сумма перевода должна быть больше 0."

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur_s = await db.execute("SELECT id, balance FROM users WHERE id = ?", (sender_db_id,))
                sender = await cur_s.fetchone()
                if not sender:
                    await db.rollback()
                    return False, "Отправитель не найден."

                if int(sender["balance"] or 0) < amount:
                    await db.rollback()
                    return False, f"Недостаточно средств (ваш баланс: {sender['balance']} 🪙)."

                cur_r = await db.execute(
                    "SELECT id, game_nick FROM users WHERE game_nick = ? COLLATE NOCASE",
                    (target_nick.strip(),),
                )
                receiver = await cur_r.fetchone()
                if not receiver:
                    await db.rollback()
                    return False, f"Игрок «{target_nick}» не найден."

                if int(receiver["id"]) == sender_db_id:
                    await db.rollback()
                    return False, "Нельзя переводить монеты самому себе."

                await db.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, sender_db_id))
                await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, receiver["id"]))
                await db.execute(
                    "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, ?, ?, 'transfer')",
                    (sender_db_id, receiver["id"], amount),
                )
                await db.commit()
                return True, f"✅ Переведено {amount} 🪙 игроку {receiver['game_nick']}."
            except Exception:
                await db.rollback()
                raise

    async def reward_online_players_map(self, rewards_map: dict[str, int]) -> int:
        """Индивидуально начисляет зарплату за онлайн (с учётом буста x3 в Опасной зоне)."""
        if not rewards_map:
            return 0

        rewarded_count = 0
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                for nick, amount in rewards_map.items():
                    if amount <= 0:
                        continue
                    cur = await db.execute(
                        "SELECT id FROM users WHERE game_nick = ? COLLATE NOCASE",
                        (nick.strip(),),
                    )
                    row = await cur.fetchone()
                    if not row:
                        continue
                    uid = int(row["id"])
                    await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, uid))
                    await db.execute(
                        "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (NULL, ?, ?, 'online_salary')",
                        (uid, amount),
                    )
                    rewarded_count += 1
                await db.commit()
                return rewarded_count
            except Exception:
                await db.rollback()
                raise
    # ==================== PVP-ЭКОНОМИКА И СТАТИСТИКА ====================

    async def get_player_rank_and_streak(self, game_nick: str) -> tuple[int | None, int]:
        """Возвращает (место_в_общем_топе_или_None, текущая_серия_убийств)."""
        clean_nick = game_nick.strip()
        user_row = await self.fetchone(
            "SELECT killstreak FROM users WHERE game_nick = ? COLLATE NOCASE",
            (clean_nick,),
        )
        streak = int(user_row["killstreak"] or 0) if user_row else 0

        top_rows = await self.fetchall(
            """
            WITH kills_agg AS (
                SELECT LOWER(TRIM(killer_nick)) AS nick_key, COUNT(*) AS kills
                FROM pvp_kills
                WHERE killer_nick IS NOT NULL AND victim_nick IS NOT NULL
                  AND LOWER(TRIM(killer_nick)) != LOWER(TRIM(victim_nick))
                  AND LOWER(TRIM(killer_nick)) != '__legacy_killer__'
                GROUP BY LOWER(TRIM(killer_nick))
            ),
            deaths_agg AS (
                SELECT LOWER(TRIM(victim_nick)) AS nick_key, COUNT(*) AS deaths
                FROM pvp_kills
                WHERE killer_nick IS NOT NULL AND victim_nick IS NOT NULL
                  AND LOWER(TRIM(killer_nick)) != LOWER(TRIM(victim_nick))
                  AND LOWER(TRIM(victim_nick)) != '__legacy_victim__'
                GROUP BY LOWER(TRIM(victim_nick))
            )
            SELECT k.nick_key
            FROM kills_agg k
            LEFT JOIN deaths_agg d ON d.nick_key = k.nick_key
            WHERE k.kills > 0
            ORDER BY k.kills DESC, COALESCE(d.deaths, 0) ASC, k.nick_key ASC
            LIMIT 20
            """
        )
        target_key = clean_nick.lower()
        rank: int | None = None
        for idx, r in enumerate(top_rows, start=1):
            if str(r["nick_key"]) == target_key:
                rank = idx
                break

        return rank, streak

    async def apply_death_penalty(self, victim_nick: str, penalty: int, floor: int) -> None:
        """Списывает штраф за смерть (не ниже floor) и обнуляет серию киллов жертвы."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT id, balance FROM users WHERE game_nick = ? COLLATE NOCASE",
                    (victim_nick.strip(),),
                )
                row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return

                uid = int(row["id"])
                cur_bal = int(row["balance"] or 0)
                new_bal = max(int(floor), cur_bal - max(0, int(penalty)))
                actual_loss = cur_bal - new_bal

                await db.execute(
                    "UPDATE users SET balance = ?, killstreak = 0 WHERE id = ?",
                    (new_bal, uid),
                )
                if actual_loss > 0:
                    await db.execute(
                        "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, NULL, ?, 'death_penalty')",
                        (uid, actual_loss),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def has_recent_kill_pair(self, killer_nick: str, victim_nick: str, hours: int) -> bool:
        """Проверяет, убивал ли killer_nick игрока victim_nick за последние hours часов."""
        if hours <= 0:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(hours))).strftime("%Y-%m-%d %H:%M:%S")
        row = await self.fetchone(
            """SELECT id FROM pvp_kills
               WHERE killer_nick = ? COLLATE NOCASE
                 AND victim_nick = ? COLLATE NOCASE
                 AND created_at >= ?
               LIMIT 1""",
            (killer_nick.strip(), victim_nick.strip(), cutoff),
        )
        return row is not None

    async def reward_pvp_kill(self, killer_nick: str, reward_amount: int) -> int:
        """Начисляет награду убийце, увеличивает серию киллов и возвращает новую серию."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT id, killstreak FROM users WHERE game_nick = ? COLLATE NOCASE",
                    (killer_nick.strip(),),
                )
                row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return 0

                uid = int(row["id"])
                new_streak = int(row["killstreak"] or 0) + 1
                gain = max(0, int(reward_amount))

                await db.execute(
                    "UPDATE users SET balance = balance + ?, killstreak = ? WHERE id = ?",
                    (gain, new_streak, uid),
                )
                if gain > 0:
                    await db.execute(
                        "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (NULL, ?, ?, 'pvp_kill_reward')",
                        (uid, gain),
                    )
                await db.commit()
                return new_streak
            except Exception:
                await db.rollback()
                raise
    # ==================== МАГАЗИН, ПОДКАТЕГОРИИ И ЗАКАЗЫ ====================

    async def get_subcategories(self, category: str) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM shop_subcategories WHERE category = ? ORDER BY name ASC",
            (category,),
        )

    async def get_subcategory(self, sub_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM shop_subcategories WHERE id = ?", (sub_id,))

    async def add_subcategory(self, category: str, name: str) -> int:
        clean_name = name.strip()
        existing = await self.fetchone(
            "SELECT id FROM shop_subcategories WHERE category = ? AND name = ? COLLATE NOCASE",
            (category, clean_name),
        )
        if existing:
            return int(existing["id"])
        return await self.execute(
            "INSERT INTO shop_subcategories (category, name) VALUES (?, ?)",
            (category, clean_name),
        )

    async def get_shop_items(self, category: str, subcategory_id: int | None = None) -> list[aiosqlite.Row]:
        if subcategory_id is None:
            return await self.fetchall(
                "SELECT * FROM shop_items WHERE category = ? AND subcategory_id IS NULL ORDER BY id ASC",
                (category,),
            )
        return await self.fetchall(
            "SELECT * FROM shop_items WHERE category = ? AND subcategory_id = ? ORDER BY id ASC",
            (category, subcategory_id),
        )

    async def get_shop_item(self, item_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM shop_items WHERE id = ?", (item_id,))

    async def add_shop_item(
        self,
        category: str,
        display_name: str,
        game_classname: str,
        price: int,
        subcategory_id: int | None = None,
        photo_file_id: str | None = None,
    ) -> int:
        return await self.execute(
            """INSERT INTO shop_items (category, subcategory_id, display_name, game_classname, price, photo_file_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (category, subcategory_id, display_name.strip(), game_classname.strip(), price, photo_file_id),
        )

    async def update_shop_item_field(self, item_id: int, field: str, value: str | int | None) -> None:
        if field not in ("display_name", "game_classname", "price", "photo_file_id"):
            raise ValueError(f"Недопустимое поле товара: {field}")
        await self.execute(f"UPDATE shop_items SET {field} = ? WHERE id = ?", (value, item_id))

    async def delete_shop_item(self, item_id: int) -> None:
        await self.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))

    async def create_shop_order(
        self,
        user_id: int,
        telegram_id: int,
        items_summary: str,
        x: float,
        z: float,
    ) -> int:
        return await self.execute(
            """INSERT INTO shop_orders (user_id, telegram_id, items_summary, x, z, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (user_id, telegram_id, items_summary, x, z),
        )

    async def pop_pending_shop_orders(self) -> list[dict]:
        """Атомарно извлекает все pending-заказы и переводит их в статус delivered."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    "SELECT * FROM shop_orders WHERE status = 'pending' ORDER BY id ASC"
                )
                rows = [dict(r) for r in await cur.fetchall()]
                if rows:
                    ids = [r["id"] for r in rows]
                    placeholders = ",".join("?" for _ in ids)
                    await db.execute(
                        f"UPDATE shop_orders SET status = 'delivered' WHERE id IN ({placeholders})",
                        tuple(ids),
                    )
                await db.commit()
                return rows
            except Exception:
                await db.rollback()
                raise
    # ==================== ГРУППИРОВКИ И ФЛАГИ ====================

    async def get_group_members(self, group_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """SELECT id, telegram_id, game_nick, role
               FROM users
               WHERE group_id = ?
               ORDER BY CASE role
                   WHEN 'leader' THEN 1
                   WHEN 'officer' THEN 2
                   ELSE 3
               END, game_nick ASC""",
            (group_id,),
        )

    async def get_group_flags_list(self) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM group_flags ORDER BY id ASC")

    async def get_group_flag_by_id(self, flag_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM group_flags WHERE id = ?", (flag_id,))

    async def get_group_flag_photo(self, flag_name: str) -> str | None:
        if not flag_name or flag_name.strip().lower() == "без флага":
            return None
        row = await self.fetchone(
            "SELECT photo_file_id FROM group_flags WHERE name = ? COLLATE NOCASE",
            (flag_name.strip(),),
        )
        return str(row["photo_file_id"]) if row and row["photo_file_id"] else None

    async def add_group_flag(self, name: str, photo_file_id: str | None = None) -> int:
        return await self.execute(
            "INSERT INTO group_flags (name, photo_file_id) VALUES (?, ?)",
            (name.strip(), photo_file_id),
        )

    async def update_group_flag_photo(self, flag_id: int, photo_file_id: str | None) -> None:
        await self.execute(
            "UPDATE group_flags SET photo_file_id = ? WHERE id = ?",
            (photo_file_id, flag_id),
        )

    async def rename_group_flag(self, flag_id: int, new_name: str) -> str | None:
        clean_new = new_name.strip()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("SELECT name FROM group_flags WHERE id = ?", (flag_id,))
                row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return None
                old_name = str(row["name"])
                await db.execute("UPDATE group_flags SET name = ? WHERE id = ?", (clean_new, flag_id))
                await db.execute(
                    "UPDATE groups SET flag_name = ? WHERE flag_name = ? COLLATE NOCASE",
                    (clean_new, old_name),
                )
                await db.commit()
                return old_name
            except Exception:
                await db.rollback()
                raise

    async def delete_group_flag(self, flag_id: int) -> str | None:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute("SELECT name FROM group_flags WHERE id = ?", (flag_id,))
                row = await cur.fetchone()
                if not row:
                    await db.rollback()
                    return None
                old_name = str(row["name"])
                await db.execute("DELETE FROM group_flags WHERE id = ?", (flag_id,))
                await db.execute(
                    "UPDATE groups SET flag_name = 'Без флага' WHERE flag_name = ? COLLATE NOCASE",
                    (old_name,),
                )
                await db.commit()
                return old_name
            except Exception:
                await db.rollback()
                raise
    # ==================== ЗОНЫ ГЕО-АВТОБАНА ====================

    async def get_autoban_zones(self, kind: str | None = None) -> list[aiosqlite.Row]:
        if kind:
            return await self.fetchall(
                "SELECT * FROM autoban_zones WHERE kind = ? ORDER BY id ASC",
                (kind,),
            )
        return await self.fetchall("SELECT * FROM autoban_zones ORDER BY kind ASC, id ASC")

    async def add_autoban_zone(self, kind: str, name: str, x: float, z: float, radius: float) -> int:
        return await self.execute(
            "INSERT INTO autoban_zones (kind, name, x, z, radius) VALUES (?, ?, ?, ?, ?)",
            (kind, name.strip(), float(x), float(z), float(radius)),
        )

    async def delete_autoban_zone(self, zone_id: int) -> None:
        await self.execute("DELETE FROM autoban_zones WHERE id = ?", (zone_id,))

    # ==================== ОПАСНЫЕ ЗОНЫ (КРОВАВЫЙ ЧАС) ====================

    async def get_hotzone_presets(self, only_enabled: bool = False) -> list[aiosqlite.Row]:
        if only_enabled:
            return await self.fetchall("SELECT * FROM hotzone_presets WHERE is_enabled = 1 ORDER BY id ASC")
        return await self.fetchall("SELECT * FROM hotzone_presets ORDER BY id ASC")

    async def get_hotzone_preset(self, preset_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM hotzone_presets WHERE id = ?", (preset_id,))

    async def add_hotzone_preset(self, name: str, x: float, z: float, radius: float) -> int:
        return await self.execute(
            "INSERT INTO hotzone_presets (name, x, z, radius, is_enabled) VALUES (?, ?, ?, ?, 1)",
            (name.strip(), float(x), float(z), float(radius)),
        )

    async def toggle_hotzone_preset(self, preset_id: int) -> None:
        await self.execute(
            "UPDATE hotzone_presets SET is_enabled = CASE WHEN is_enabled = 1 THEN 0 ELSE 1 END WHERE id = ?",
            (preset_id,),
        )

    async def delete_hotzone_preset(self, preset_id: int) -> bool:
        """Удаляет локацию, если по ней сейчас не идёт активный или анонсированный ивент."""
        active = await self.fetchone(
            "SELECT id FROM hotzone_events WHERE preset_id = ? AND status IN ('announced', 'active') LIMIT 1",
            (preset_id,),
        )
        if active:
            return False
        await self.execute("DELETE FROM hotzone_presets WHERE id = ?", (preset_id,))
        return True
    async def create_hotzone_event(
        self,
        preset_id: int,
        status: str,
        salary_mult: float,
        kill_mult: float,
        starts_at: str,
        ends_at: str,
    ) -> aiosqlite.Row | None:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "UPDATE hotzone_events SET status = 'finished' WHERE status IN ('announced', 'active')"
                )
                cur = await db.execute(
                    """INSERT INTO hotzone_events (preset_id, status, salary_mult, kill_mult, starts_at, ends_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (preset_id, status, float(salary_mult), float(kill_mult), starts_at, ends_at),
                )
                event_id = cur.lastrowid
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        return await self.fetchone(
            """SELECT e.*, p.name, p.x, p.z, p.radius
               FROM hotzone_events e
               JOIN hotzone_presets p ON p.id = e.preset_id
               WHERE e.id = ?""",
            (event_id,),
        )

    async def get_current_hotzone_event(self) -> aiosqlite.Row | None:
        return await self.fetchone(
            """SELECT e.*, p.name, p.x, p.z, p.radius
               FROM hotzone_events e
               JOIN hotzone_presets p ON p.id = e.preset_id
               WHERE e.status IN ('announced', 'active')
               ORDER BY e.id DESC
               LIMIT 1"""
        )

    async def update_hotzone_event_status(self, event_id: int, status: str) -> None:
        await self.execute(
            "UPDATE hotzone_events SET status = ? WHERE id = ?",
            (status, event_id),
        )

    async def finish_all_hotzone_events(self) -> None:
        await self.execute(
            "UPDATE hotzone_events SET status = 'finished' WHERE status IN ('announced', 'active')"
        )
