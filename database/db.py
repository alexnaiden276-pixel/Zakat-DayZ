import os
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite


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
                    balance INTEGER NOT NULL DEFAULT 1000,
                    kills INTEGER NOT NULL DEFAULT 0,
                    deaths INTEGER NOT NULL DEFAULT 0,
                    headshots INTEGER NOT NULL DEFAULT 0,
                    max_kill_distance REAL NOT NULL DEFAULT 0,
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

                PRAGMA foreign_keys = ON;
                """
            )
            # Миграция 1: добавляем headshots в users
            cursor = await db.execute("PRAGMA table_info(users)")
            columns = [row["name"] for row in await cursor.fetchall()]
            if "headshots" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN headshots INTEGER NOT NULL DEFAULT 0")

            # Миграция 2: добавляем subcategory_id в shop_items
            cursor_shop = await db.execute("PRAGMA table_info(shop_items)")
            shop_columns = [row["name"] for row in await cursor_shop.fetchall()]
            if "subcategory_id" not in shop_columns:
                await db.execute(
                    "ALTER TABLE shop_items ADD COLUMN subcategory_id INTEGER REFERENCES shop_subcategories(id) ON DELETE SET NULL"
                )

            # Миграция 3: одноразовый перенос старых убийств/смертей из users в pvp_kills
            cur_mig = await db.execute(
                "SELECT value FROM settings WHERE key = 'migrated_legacy_pvp_stats'"
            )
            if not await cur_mig.fetchone():
                cur_users = await db.execute(
                    "SELECT game_nick, kills, deaths FROM users WHERE kills > 0 OR deaths > 0"
                )
                legacy_users = await cur_users.fetchall()
                for u in legacy_users:
                    nick = str(u["game_nick"]).strip()
                    for _ in range(int(u["kills"] or 0)):
                        await db.execute(
                            """INSERT INTO pvp_kills (killer_nick, victim_nick, weapon, distance, is_headshot, created_at)
                               VALUES (?, '__legacy_victim__', '', 0, 0, '2020-01-01 00:00:00')""",
                            (nick,),
                        )
                    for _ in range(int(u["deaths"] or 0)):
                        await db.execute(
                            """INSERT INTO pvp_kills (killer_nick, victim_nick, weapon, distance, is_headshot, created_at)
                               VALUES ('__legacy_killer__', ?, '', 0, 0, '2020-01-01 00:00:00')""",
                            (nick,),
                        )
                await db.execute(
                    "INSERT INTO settings (key, value) VALUES ('migrated_legacy_pvp_stats', '1')"
                )
            # Миграция 4: добавляем флаг, время обновления и ID поста в теме для groups
            cur_grp = await db.execute("PRAGMA table_info(groups)")
            grp_cols = [row["name"] for row in await cur_grp.fetchall()]
            if "flag_name" not in grp_cols:
                await db.execute("ALTER TABLE groups ADD COLUMN flag_name TEXT NOT NULL DEFAULT 'Без флага'")
            if "updated_at" not in grp_cols:
                await db.execute("ALTER TABLE groups ADD COLUMN updated_at TEXT")
            if "topic_message_id" not in grp_cols:
                await db.execute("ALTER TABLE groups ADD COLUMN topic_message_id INTEGER")
            if "topic_has_photo" not in grp_cols:
                await db.execute("ALTER TABLE groups ADD COLUMN topic_has_photo INTEGER NOT NULL DEFAULT 0")

            # Миграция 5: обновляем структуру group_flags (id + разрешаем NULL для photo_file_id)
            cur_gf = await db.execute("PRAGMA table_info(group_flags)")
            gf_cols = [row["name"] for row in await cur_gf.fetchall()]
            if "id" not in gf_cols:
                await db.execute("ALTER TABLE group_flags RENAME TO group_flags_old")
                await db.execute(
                    """CREATE TABLE group_flags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        photo_file_id TEXT
                    )"""
                )
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
        return await self.execute(
            """INSERT INTO users (telegram_id, game_nick, role)
               VALUES (?, ?, ?)""",
            (telegram_id, nick, role),
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

    async def delete_group_flag(self, flag_id: int) -> str | None:
        """Удаляет флаг, сбрасывает его у кланов на 'Без флага' и возвращает старое название."""
        row = await self.get_group_flag_by_id(flag_id)
        if not row:
            return None
        old_name = str(row["name"])
        await self.execute("DELETE FROM group_flags WHERE id = ?", (flag_id,))
        await self.execute(
            "UPDATE groups SET flag_name = 'Без флага' WHERE flag_name = ? COLLATE NOCASE",
            (old_name,),
        )
        return old_name

    async def get_group_flag_photo(self, flag_name: str) -> str | None:
        row = await self.fetchone(
            "SELECT photo_file_id FROM group_flags WHERE name = ? COLLATE NOCASE",
            (flag_name.strip(),),
        )
        return str(row["photo_file_id"]) if (row and row["photo_file_id"]) else None
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
