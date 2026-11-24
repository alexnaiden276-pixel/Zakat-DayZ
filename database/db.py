import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

MSK_TZ = timezone(timedelta(hours=3))

DEFAULT_ECONOMY_CONFIG: dict[str, int] = {
    "eco_start_bonus": 100,           # Стартовый бонус при регистрации
    "eco_online_reward": 15,          # Награда за 15 мин онлайна (60/час)
    "eco_death_penalty": 150,         # Штраф за любую смерть
    "eco_debt_floor": -300,           # Максимальный минус (долг)
    "eco_daily_bonus": 30,            # Ежедневный бонус
    "eco_kill_tier1": 150,            # Обычный выживший (0–2 килла / вне топа)
    "eco_kill_tier2": 350,            # Опытный боец (3–5 киллов / Топ 11–20)
    "eco_kill_tier3": 600,            # Ветеран (6–9 киллов / Топ 4–10)
    "eco_kill_tier4": 1000,           # Цель №1 (10+ киллов / Топ 1–3)
    "eco_kill_cooldown_hrs": 2,       # Кулдаун на повторное убийство одного игрока (часов)
    "eco_hotzone_enabled": 1,         # 1 = автозапуск Опасных зон включен, 0 = выключен
    "eco_hotzone_salary_mult": 3,     # Множитель онлайна в Опасной зоне (x3)
    "eco_hotzone_kill_mult": 2,       # Множитель награды за убийство в Опасной зоне (x2)
    "eco_hotzone_duration_min": 60,   # Длительность ивента Опасной зоны (мин)
    "eco_hotzone_announce_min": 15,   # Анонс до начала Опасной зоны (мин)
    "eco_hotzone_min_online": 4,      # Мин. онлайн игроков для рандомного запуска (0 = без проверки)
}


class Database:
    def __init__(self, path: str):
        self.path = path

    @asynccontextmanager
    async def connect(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            yield db

    async def init(self) -> None:
        async with self.connect() as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.executescript(
                """
                PRAGMA foreign_keys = OFF;

                -- ТАБЛИЦА ДЛЯ СЛУЖЕБНЫХ НАСТРОЕК БОТА --
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL UNIQUE,
                    game_nick TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    role TEXT NOT NULL DEFAULT 'player',
                    balance INTEGER NOT NULL DEFAULT 100,
                    kills INTEGER NOT NULL DEFAULT 0,
                    deaths INTEGER NOT NULL DEFAULT 0,
                    headshots INTEGER NOT NULL DEFAULT 0,
                    max_kill_distance REAL NOT NULL DEFAULT 0,
                    killstreak INTEGER NOT NULL DEFAULT 0,
                    last_daily_bonus TEXT,
                    group_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    tag TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT NOT NULL DEFAULT '',
                    leader_id INTEGER NOT NULL,
                    flag_name TEXT NOT NULL DEFAULT 'Без флага',
                    updated_at TEXT,
                    topic_message_id INTEGER,
                    topic_has_photo INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(leader_id) REFERENCES users(id)
                );

                -- КАРТИНКИ И СПИСОК ФЛАГОВ ГРУППИРОВОК --
                CREATE TABLE IF NOT EXISTS group_flags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    photo_file_id TEXT
                );

                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS ticket_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender_id INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    admin_message_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(ticket_id) REFERENCES tickets(id)
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER,
                    receiver_id INTEGER,
                    amount INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                -- ТАБЛИЦА ПОДКАТЕГОРИЙ (ГРУПП КАТАЛОГА МАГАЗИНА) --
                CREATE TABLE IF NOT EXISTS shop_subcategories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    UNIQUE(category, name)
                );

                -- ТАБЛИЦА ТОВАРОВ ТЕРМИНАЛА (МАГАЗИНА) --
                CREATE TABLE IF NOT EXISTS shop_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    subcategory_id INTEGER,
                    display_name TEXT NOT NULL,
                    game_classname TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    FOREIGN KEY(subcategory_id) REFERENCES shop_subcategories(id) ON DELETE SET NULL
                );

                -- ТАБЛИЦА ЗАКАЗОВ ДЛЯ УВЕДОМЛЕНИЙ ПОСЛЕ РЕСТАРТА --
                CREATE TABLE IF NOT EXISTS shop_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    items_summary TEXT NOT NULL,
                    x REAL NOT NULL,
                    z REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_shop_orders_status ON shop_orders(status);

                -- ТАБЛИЦА ЛОГА PVP-УБИЙСТВ ДЛЯ НЕДЕЛЬНОГО И ОБЩЕГО РЕЙТИНГА --
                CREATE TABLE IF NOT EXISTS pvp_kills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    killer_nick TEXT NOT NULL,
                    victim_nick TEXT NOT NULL,
                    weapon TEXT NOT NULL DEFAULT '',
                    distance REAL NOT NULL DEFAULT 0,
                    is_headshot INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_pvp_kills_created_at ON pvp_kills(created_at);

                -- ТАБЛИЦА ЗОН АВТОБАНА (СТРОЙКА И УБИЙСТВА) --
                CREATE TABLE IF NOT EXISTS autoban_zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    x REAL NOT NULL,
                    z REAL NOT NULL,
                    radius REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                -- СПРАВОЧНИК ЛОКАЦИЙ ДЛЯ ОПАСНЫХ ЗОН --
                CREATE TABLE IF NOT EXISTS hotzone_presets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    x REAL NOT NULL,
                    z REAL NOT NULL,
                    radius REAL NOT NULL DEFAULT 400.0,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                -- ИСТОРИЯ И ТЕКУЩИЙ СТАТУС ИВЕНТОВ ОПАСНЫХ ЗОН --
                CREATE TABLE IF NOT EXISTS hotzone_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preset_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    salary_mult REAL NOT NULL DEFAULT 3.0,
                    kill_mult REAL NOT NULL DEFAULT 2.0,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(preset_id) REFERENCES hotzone_presets(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_hotzone_events_status ON hotzone_events(status);

                PRAGMA foreign_keys = ON;
                """
            )
            # Миграция 1: добавляем headshots, killstreak, last_daily_bonus в users
            cursor = await db.execute("PRAGMA table_info(users)")
            columns = [row["name"] for row in await cursor.fetchall()]
            if "headshots" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN headshots INTEGER NOT NULL DEFAULT 0")
            if "killstreak" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN killstreak INTEGER NOT NULL DEFAULT 0")
            if "last_daily_bonus" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN last_daily_bonus TEXT")

            # Миграция 3: перенос старой статистики убийств в pvp_kills (однократно)
            cur_mig = await db.execute("SELECT value FROM settings WHERE key = 'migrated_legacy_pvp_stats'")
            if not await cur_mig.fetchone():
                cur_users = await db.execute("SELECT game_nick, kills, deaths FROM users")
                legacy_users = await cur_users.fetchall()
                for u in legacy_users:
                    nick = str(u["game_nick"]).strip()
                    for _ in range(int(u["kills"] or 0)):
                        await db.execute(
                            """INSERT INTO pvp_kills (killer_nick, victim_nick, weapon, distance, is_headshot, created_at)
                               VALUES (?, '__legacy_victim__', '', 0, 0, '2000-01-01 00:00:00')""",
                            (nick,),
                        )
                    for _ in range(int(u["deaths"] or 0)):
                        await db.execute(
                            """INSERT INTO pvp_kills (killer_nick, victim_nick, weapon, distance, is_headshot, created_at)
                               VALUES ('__legacy_killer__', ?, '', 0, 0, '2000-01-01 00:00:00')""",
                            (nick,),
                        )
                await db.execute(
                    "INSERT INTO settings (key, value) VALUES ('migrated_legacy_pvp_stats', '1')"
                )

            # Миграция 4: перенос старых флагов группировок
            cur_old_flags = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='group_flags_old'"
            )
            if await cur_old_flags.fetchone():
                await db.execute(
                    "INSERT OR IGNORE INTO group_flags (name, photo_file_id) SELECT name, photo_file_id FROM group_flags_old"
                )
                await db.execute("DROP TABLE group_flags_old")

            cur_flags_seed = await db.execute(
                "SELECT value FROM settings WHERE key = 'seeded_default_group_flags'"
            )
            if not await cur_flags_seed.fetchone():
                default_names = [
                    "Пираты", "Волки", "Медведи", "Череп", "Щит",
                    "Огонь", "Звезда", "Анархия", "Ворон", "Сталкеры", "Олень",
                ]
                for fname in default_names:
                    await db.execute(
                        "INSERT OR IGNORE INTO group_flags (name, photo_file_id) VALUES (?, NULL)",
                        (fname,),
                    )
                await db.execute(
                    "INSERT INTO settings (key, value) VALUES ('seeded_default_group_flags', '1')"
                )

            # Миграция 5: базовые локации Опасных зон для Чернаруси (однократно)
            cur_hz_seed = await db.execute(
                "SELECT value FROM settings WHERE key = 'seeded_default_hotzones'"
            )
            if not await cur_hz_seed.fetchone():
                default_zones = [
                    ("Аэропорт Выборы (СЗ)", 4500.0, 10200.0, 450.0),
                    ("Военная база Тисы", 1600.0, 14100.0, 400.0),
                    ("Зеленогорск (Военка)", 2700.0, 5300.0, 350.0),
                    ("Военная база Павлово", 2150.0, 3350.0, 350.0),
                    ("Старый Собор", 6050.0, 7750.0, 350.0),
                ]
                for zname, zx, zz, zrad in default_zones:
                    await db.execute(
                        "INSERT OR IGNORE INTO hotzone_presets (name, x, z, radius) VALUES (?, ?, ?, ?)",
                        (zname, zx, zz, zrad),
                    )
                await db.execute(
                    "INSERT INTO settings (key, value) VALUES ('seeded_default_hotzones', '1')"
                )

            await db.commit()

    # ==========================================
    # БАЗОВЫЕ МЕТОДЫ ЗАПРОСОВ И НАСТРОЕК
    # ==========================================

    async def fetchone(self, query: str, params: tuple = ()) -> Any:
        async with self.connect() as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()) -> list[Any]:
        async with self.connect() as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchall()

    async def execute(self, query: str, params: tuple = ()) -> int:
        async with self.connect() as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.lastrowid

    async def get_setting(self, key: str) -> str | None:
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return str(row["value"]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            """INSERT INTO settings (key, value)
               VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )
    # ==========================================
    # ПОЛЬЗОВАТЕЛИ, ГРУППИРОВКИ И ПЕРЕВОДЫ
    # ==========================================

    async def get_user(self, telegram_id: int):
        return await self.fetchone(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )

    async def get_user_by_nick(self, nick: str):
        return await self.fetchone(
            "SELECT * FROM users WHERE game_nick = ? COLLATE NOCASE",
            (nick,),
        )

    async def create_user(self, telegram_id: int, nick: str, role: str = "player"):
        eco = await self.get_economy_config()
        start_bal = eco["eco_start_bonus"]
        return await self.execute(
            """INSERT INTO users (telegram_id, game_nick, role, balance)
               VALUES (?, ?, ?, ?)""",
            (telegram_id, nick, role, start_bal),
        )

    async def get_group_members(self, group_id: int):
        return await self.fetchall(
            """SELECT id, game_nick, role FROM users
               WHERE group_id = ?
               ORDER BY CASE role WHEN 'leader' THEN 1 WHEN 'officer' THEN 2 ELSE 3 END, game_nick ASC""",
            (group_id,),
        )

    async def transfer(self, sender_id: int, receiver_nick: str, amount: int) -> tuple[bool, str]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur_s = await db.execute("SELECT id, balance FROM users WHERE id = ?", (sender_id,))
            sender = await cur_s.fetchone()
            if not sender or sender["balance"] < amount:
                await db.rollback()
                return False, "❌ Недостаточно средств на балансе."

            cur_r = await db.execute(
                "SELECT id, game_nick FROM users WHERE game_nick = ? COLLATE NOCASE",
                (receiver_nick,),
            )
            receiver = await cur_r.fetchone()
            if not receiver:
                await db.rollback()
                return False, "❌ Получатель не найден."

            await db.execute("UPDATE users SET balance = balance - ? WHERE id = ?", (amount, sender["id"]))
            await db.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (amount, receiver["id"]))
            await db.execute(
                "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, ?, ?, 'transfer')",
                (sender["id"], receiver["id"], amount),
            )
            await db.commit()
            return True, f"✅ Переведено {amount} 🪙 игроку {receiver['game_nick']}."

    # ==========================================
    # УПРАВЛЕНИЕ ФЛАГАМИ ГРУППИРОВОК
    # ==========================================

    async def get_group_flags_list(self):
        return await self.fetchall("SELECT id, name, photo_file_id FROM group_flags ORDER BY name ASC")

    async def get_group_flag_by_id(self, flag_id: int):
        return await self.fetchone("SELECT id, name, photo_file_id FROM group_flags WHERE id = ?", (flag_id,))

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
        """Переименовывает флаг и возвращает старое название (для обновления кланов)."""
        row = await self.get_group_flag_by_id(flag_id)
        if not row:
            return None
        old_name = str(row["name"])
        await self.execute(
            "UPDATE group_flags SET name = ? WHERE id = ?",
            (new_name.strip(), flag_id),
        )
        await self.execute(
            "UPDATE groups SET flag_name = ? WHERE flag_name = ? COLLATE NOCASE",
            (new_name.strip(), old_name),
        )
        return old_name

    async def delete_group_flag(self, flag_id: int) -> None:
        row = await self.get_group_flag_by_id(flag_id)
        if row:
            await self.execute(
                "UPDATE groups SET flag_name = 'Без флага' WHERE flag_name = ? COLLATE NOCASE",
                (str(row["name"]),),
            )
        await self.execute("DELETE FROM group_flags WHERE id = ?", (flag_id,))

    # ==========================================
    # МАГАЗИН (ПОДКАТЕГОРИИ, ТОВАРЫ И ЗАКАЗЫ)
    # ==========================================

    async def get_subcategories(self, category: str):
        return await self.fetchall(
            "SELECT * FROM shop_subcategories WHERE category = ? ORDER BY name ASC",
            (category,),
        )

    async def get_subcategory(self, sub_id: int):
        return await self.fetchone("SELECT * FROM shop_subcategories WHERE id = ?", (sub_id,))

    async def add_subcategory(self, category: str, name: str) -> int:
        return await self.execute(
            "INSERT INTO shop_subcategories (category, name) VALUES (?, ?)",
            (category, name.strip()),
        )

    async def delete_subcategory(self, sub_id: int) -> None:
        await self.execute("DELETE FROM shop_subcategories WHERE id = ?", (sub_id,))
    async def get_shop_items(self, category: str | None = None, subcategory_id: int | None = None):
        if category is None:
            return await self.fetchall("SELECT * FROM shop_items ORDER BY category, display_name ASC")
        if subcategory_id is None:
            return await self.fetchall(
                "SELECT * FROM shop_items WHERE category = ? AND subcategory_id IS NULL ORDER BY display_name ASC",
                (category,),
            )
        return await self.fetchall(
            "SELECT * FROM shop_items WHERE category = ? AND subcategory_id = ? ORDER BY display_name ASC",
            (category, subcategory_id),
        )

    async def get_shop_item(self, item_id: int):
        return await self.fetchone("SELECT * FROM shop_items WHERE id = ?", (item_id,))

    async def add_shop_item(
        self,
        category: str,
        display_name: str,
        game_classname: str,
        price: int,
        subcategory_id: int | None = None,
    ) -> int:
        return await self.execute(
            """INSERT INTO shop_items (category, subcategory_id, display_name, game_classname, price)
               VALUES (?, ?, ?, ?, ?)""",
            (category, subcategory_id, display_name.strip(), game_classname.strip(), price),
        )

    async def update_shop_item_field(self, item_id: int, field: str, value: Any) -> None:
        allowed = {"display_name", "game_classname", "price", "category", "subcategory_id"}
        if field not in allowed:
            raise ValueError(f"Недопустимое поле: {field}")
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

    async def get_pending_shop_orders(self):
        return await self.fetchall("SELECT * FROM shop_orders WHERE status = 'pending' ORDER BY id ASC")

    async def mark_shop_orders_delivered(self) -> None:
        await self.execute("UPDATE shop_orders SET status = 'delivered' WHERE status = 'pending'")

    async def pop_pending_shop_orders(self) -> list[Any]:
        """Атомарно забирает все ожидающие заказы и переводит их в статус 'delivered'."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT * FROM shop_orders WHERE status = 'pending' ORDER BY id ASC")
            rows = await cursor.fetchall()
            if rows:
                await db.execute("UPDATE shop_orders SET status = 'delivered' WHERE status = 'pending'")
            await db.commit()
            return rows

    # ==========================================
    # ЛОГ PVP-УБИЙСТВ (ДЛЯ ТАБЛИЦЫ ЛИДЕРОВ)
    # ==========================================

    async def record_pvp_kill(
        self,
        killer_nick: str,
        victim_nick: str,
        weapon: str = "",
        distance: float = 0.0,
        is_headshot: bool = False,
    ) -> int:
        """Сохраняет факт PvP-убийства в историю для недельного и общего рейтинга."""
        return await self.execute(
            """INSERT INTO pvp_kills (killer_nick, victim_nick, weapon, distance, is_headshot)
               VALUES (?, ?, ?, ?, ?)""",
            (
                killer_nick.strip(),
                victim_nick.strip(),
                str(weapon or "").strip(),
                float(distance or 0.0),
                1 if is_headshot else 0,
            ),
        )

    # ==========================================
    # ЗОНЫ АВТОБАНА (СТРОЙКА И УБИЙСТВА)
    # ==========================================

    async def get_autoban_zones(self, kind: str | None = None):
        if kind:
            return await self.fetchall(
                "SELECT * FROM autoban_zones WHERE kind = ? ORDER BY id ASC",
                (kind,),
            )
        return await self.fetchall("SELECT * FROM autoban_zones ORDER BY kind, id ASC")

    async def add_autoban_zone(
        self,
        kind: str,
        name: str,
        x: float,
        z: float,
        radius: float,
    ) -> int:
        return await self.execute(
            """INSERT INTO autoban_zones (kind, name, x, z, radius)
               VALUES (?, ?, ?, ?, ?)""",
            (kind, name.strip(), float(x), float(z), float(radius)),
        )

    async def delete_autoban_zone(self, zone_id: int) -> None:
        await self.execute("DELETE FROM autoban_zones WHERE id = ?", (zone_id,))
    # ==========================================
    # ЭКОНОМИКА: НАСТРОЙКИ, НАГРАДЫ, ШТРАФЫ, СЕРИИ И БОНУСЫ
    # ==========================================

    async def get_economy_config(self) -> dict[str, int]:
        """Возвращает актуальные настройки экономики (из БД или значения по умолчанию)."""
        config = dict(DEFAULT_ECONOMY_CONFIG)
        rows = await self.fetchall("SELECT key, value FROM settings WHERE key LIKE 'eco_%'")
        for row in rows:
            k = str(row["key"])
            if k in config:
                try:
                    config[k] = int(row["value"])
                except (TypeError, ValueError):
                    pass
        return config

    async def update_economy_setting(self, key: str, value: int) -> None:
        if key not in DEFAULT_ECONOMY_CONFIG:
            raise ValueError(f"Неизвестный параметр экономики: {key}")
        await self.set_setting(key, str(int(value)))

    async def get_player_rank_and_streak(self, game_nick: str) -> tuple[int | None, int]:
        """Возвращает (место_в_топе_от_1, текущая_серия_киллов) по формуле leaderboard.py."""
        user = await self.get_user_by_nick(game_nick)
        streak = int(user["killstreak"] or 0) if user else 0

        all_kills = await self.fetchall(
            """SELECT killer_nick, victim_nick
               FROM pvp_kills
               WHERE killer_nick IS NOT NULL AND victim_nick IS NOT NULL
                 AND LOWER(killer_nick) != LOWER(victim_nick)"""
        )
        stats: dict[str, dict] = {}
        for row in all_kills:
            killer = str(row["killer_nick"]).strip()
            victim = str(row["victim_nick"]).strip()
            k_key = killer.lower()
            v_key = victim.lower()

            if k_key != "__legacy_killer__":
                stats.setdefault(k_key, {"nick": killer, "kills": 0, "deaths": 0})
                stats[k_key]["kills"] += 1
            if v_key != "__legacy_victim__":
                stats.setdefault(v_key, {"nick": victim, "kills": 0, "deaths": 0})
                stats[v_key]["deaths"] += 1

        ranked = sorted(
            [s for s in stats.values() if s["kills"] > 0],
            key=lambda x: (-x["kills"], x["deaths"], x["nick"].lower()),
        )[:20]

        rank = None
        nick_lower = game_nick.strip().lower()
        for idx, item in enumerate(ranked, start=1):
            if item["nick"].lower() == nick_lower:
                rank = idx
                break

        return rank, streak

    async def apply_death_penalty(self, victim_nick: str, penalty: int | None = None, floor: int | None = None) -> int:
        """Списывает штраф за смерть (не ниже floor) и обнуляет серию киллов."""
        if penalty is None or floor is None:
            eco = await self.get_economy_config()
            if penalty is None:
                penalty = eco["eco_death_penalty"]
            if floor is None:
                floor = eco["eco_debt_floor"]

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT id, balance FROM users WHERE game_nick = ? COLLATE NOCASE",
                (victim_nick.strip(),),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return 0

            current_bal = int(row["balance"] or 0)
            new_bal = max(floor, current_bal - penalty)
            actual_loss = current_bal - new_bal

            await db.execute(
                "UPDATE users SET balance = ?, killstreak = 0 WHERE id = ?",
                (new_bal, row["id"]),
            )
            if actual_loss > 0:
                await db.execute(
                    "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (?, NULL, ?, 'death_penalty')",
                    (row["id"], actual_loss),
                )
            await db.commit()
            return actual_loss

    async def has_recent_kill_pair(self, killer_nick: str, victim_nick: str, hours: int | None = None) -> bool:
        """Защита от абуза: проверяет, убивал ли killer этого же victim за последние N часов."""
        if hours is None:
            eco = await self.get_economy_config()
            hours = eco["eco_kill_cooldown_hrs"]
        if hours <= 0:
            return False

        row = await self.fetchone(
            """SELECT id FROM pvp_kills
               WHERE killer_nick = ? COLLATE NOCASE
                 AND victim_nick = ? COLLATE NOCASE
                 AND created_at >= datetime('now', ?)
               LIMIT 1""",
            (killer_nick.strip(), victim_nick.strip(), f"-{hours} hours"),
        )
        return row is not None

    async def reward_pvp_kill(self, killer_nick: str, amount: int) -> int:
        """Начисляет награду убийце и увеличивает его серию киллов (+1). Возвращает новую серию."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT id, killstreak FROM users WHERE game_nick = ? COLLATE NOCASE",
                (killer_nick.strip(),),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return 0

            new_streak = int(row["killstreak"] or 0) + 1
            await db.execute(
                "UPDATE users SET balance = balance + ?, killstreak = ? WHERE id = ?",
                (amount, new_streak, row["id"]),
            )
            if amount > 0:
                await db.execute(
                    "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (NULL, ?, ?, 'pvp_reward')",
                    (row["id"], amount),
                )
            await db.commit()
            return new_streak

    async def reward_online_players(self, nicks: list[str], amount: int | None = None) -> int:
        """Начисляет одинаковую зарплату за онлайн всем зарегистрированным игрокам из списка ников."""
        if not nicks:
            return 0
        if amount is None:
            eco = await self.get_economy_config()
            amount = eco["eco_online_reward"]
        if amount <= 0:
            return 0

        count = 0
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            for nick in nicks:
                cur = await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE game_nick = ? COLLATE NOCASE",
                    (amount, nick.strip()),
                )
                if cur.rowcount > 0:
                    count += 1
            await db.commit()
        return count

    async def reward_online_players_map(self, rewards: dict[str, int]) -> int:
        """Начисляет индивидуальную зарплату за онлайн (ключ: game_nick, значение: сумма с учётом Опасных зон)."""
        if not rewards:
            return 0
        count = 0
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            for nick, amount in rewards.items():
                if amount <= 0:
                    continue
                cur = await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE game_nick = ? COLLATE NOCASE",
                    (int(amount), nick.strip()),
                )
                if cur.rowcount > 0:
                    count += 1
            await db.commit()
        return count

    async def claim_daily_bonus(self, telegram_id: int, amount: int | None = None) -> tuple[bool, str]:
        """Выдаёт ежедневный бонус раз в календарные сутки (по МСК)."""
        if amount is None:
            eco = await self.get_economy_config()
            amount = eco["eco_daily_bonus"]

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT id, balance, last_daily_bonus FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False, "❌ Профиль не найден."

            today_msk = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
            if row["last_daily_bonus"] == today_msk:
                await db.rollback()
                return False, "⏳ Вы уже забирали ежедневный бонус сегодня. Приходите завтра!"

            new_bal = int(row["balance"] or 0) + amount
            await db.execute(
                "UPDATE users SET balance = ?, last_daily_bonus = ? WHERE id = ?",
                (new_bal, today_msk, row["id"]),
            )
            if amount > 0:
                await db.execute(
                    "INSERT INTO transactions (sender_id, receiver_id, amount, kind) VALUES (NULL, ?, ?, 'daily_bonus')",
                    (row["id"], amount),
                )
            await db.commit()
            return True, f"🎁 Вы получили ежедневный бонус: <b>+{amount} 🪙</b>\nВаш баланс: <b>{new_bal} 🪙</b>"
    # ==========================================
    # СБРОС ЭКОНОМИКИ И СТАТИСТИКИ ПРИ ВАЙПЕ
    # ==========================================

    async def wipe_all_players_data(self) -> tuple[int, int]:
        """
        Полный сброс экономики и статистики игроков при вайпе сервера:
        - Выставляет всем игрокам стартовый баланс (eco_start_bonus).
        - Обнуляет kills, deaths, headshots, max_kill_distance, killstreak и last_daily_bonus.
        - Очищает таблицы pvp_kills, transactions, shop_orders и hotzone_events (пресеты локаций сохраняются).
        """
        eco = await self.get_economy_config()
        start_bal = int(eco.get("eco_start_bonus", DEFAULT_ECONOMY_CONFIG["eco_start_bonus"]))

        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE users
                SET balance = ?,
                    kills = 0,
                    deaths = 0,
                    headshots = 0,
                    max_kill_distance = 0,
                    killstreak = 0,
                    last_daily_bonus = NULL
                """,
                (start_bal,),
            )
            wiped_count = cursor.rowcount

            await db.execute("DELETE FROM pvp_kills")
            await db.execute("DELETE FROM transactions")
            await db.execute("DELETE FROM shop_orders")
            await db.execute("DELETE FROM hotzone_events")
            await db.commit()
            return wiped_count, start_bal

    # ==========================================
    # ОПАСНЫЕ ЗОНЫ (АНОМАЛЬНАЯ АКТИВНОСТЬ / КРОВАВЫЙ ЧАС)
    # ==========================================

    async def get_hotzone_presets(self, only_enabled: bool = False):
        if only_enabled:
            return await self.fetchall("SELECT * FROM hotzone_presets WHERE is_enabled = 1 ORDER BY id ASC")
        return await self.fetchall("SELECT * FROM hotzone_presets ORDER BY id ASC")

    async def get_hotzone_preset(self, preset_id: int):
        return await self.fetchone("SELECT * FROM hotzone_presets WHERE id = ?", (preset_id,))

    async def add_hotzone_preset(self, name: str, x: float, z: float, radius: float) -> int:
        return await self.execute(
            "INSERT INTO hotzone_presets (name, x, z, radius) VALUES (?, ?, ?, ?)",
            (name.strip(), float(x), float(z), float(radius)),
        )

    async def toggle_hotzone_preset(self, preset_id: int) -> None:
        await self.execute(
            "UPDATE hotzone_presets SET is_enabled = 1 - is_enabled WHERE id = ?",
            (preset_id,),
        )

    async def delete_hotzone_preset(self, preset_id: int) -> None:
        await self.execute("DELETE FROM hotzone_presets WHERE id = ?", (preset_id,))

    async def get_current_hotzone_event(self):
        return await self.fetchone(
            """SELECT e.*, p.name, p.x, p.z, p.radius
               FROM hotzone_events e
               JOIN hotzone_presets p ON p.id = e.preset_id
               WHERE e.status IN ('announced', 'active')
               ORDER BY e.id DESC LIMIT 1"""
        )

    async def create_hotzone_event(
        self,
        preset_id: int,
        status: str,
        salary_mult: float,
        kill_mult: float,
        starts_at: str,
        ends_at: str,
    ):
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "UPDATE hotzone_events SET status = 'finished' WHERE status IN ('announced', 'active')"
            )
            await db.execute(
                """INSERT INTO hotzone_events (preset_id, status, salary_mult, kill_mult, starts_at, ends_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (preset_id, status, float(salary_mult), float(kill_mult), starts_at, ends_at),
            )
            await db.commit()
        return await self.get_current_hotzone_event()

    async def update_hotzone_event_status(self, event_id: int, status: str) -> None:
        await self.execute("UPDATE hotzone_events SET status = ? WHERE id = ?", (status, event_id))

    async def finish_all_hotzone_events(self) -> None:
        await self.execute(
            "UPDATE hotzone_events SET status = 'finished' WHERE status IN ('announced', 'active')"
        )

    async def get_hotzone_hours(self) -> list[int]:
        """Возвращает отсортированный список часов старта ивента по МСК (по умолчанию [16, 19, 22])."""
        raw = await self.get_setting("hotzone_prime_hours")
        if not raw:
            return [16, 19, 22]
        hours = []
        for part in raw.replace(",", " ").split():
            if part.isdigit() and 0 <= int(part) <= 23:
                hours.append(int(part))
        return sorted(set(hours)) or [16, 19, 22]

    async def set_hotzone_hours(self, hours: list[int]) -> None:
        clean = sorted({h for h in hours if 0 <= h <= 23})
        await self.set_setting("hotzone_prime_hours", ",".join(map(str, clean)))
