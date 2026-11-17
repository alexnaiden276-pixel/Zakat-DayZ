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
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(leader_id) REFERENCES users(id)
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

                -- ТАБЛИЦА ГРУПП СДАЧИ ЛУТА --
                CREATE TABLE IF NOT EXISTS sell_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE
                );

                -- ТАБЛИЦА ТОВАРОВ СДАЧИ ЛУТА --
                CREATE TABLE IF NOT EXISTS sell_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER,
                    display_name TEXT NOT NULL,
                    game_classname TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    FOREIGN KEY(group_id) REFERENCES sell_groups(id) ON DELETE SET NULL
                );

                -- ТАБЛИЦА ОЧЕРЕДИ И ЗАЯВОК НА СДАЧУ ЛУТА --
                CREATE TABLE IF NOT EXISTS sell_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    game_nick TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    paid_amount INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    active_since TEXT,
                    finished_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_sell_sessions_status ON sell_sessions(status);
                CREATE INDEX IF NOT EXISTS idx_sell_sessions_user ON sell_sessions(user_id);

                PRAGMA foreign_keys = ON;
                """
            )

            # Миграция 1: добавляем headshots в users, если таблица создана раньше
            cursor = await db.execute("PRAGMA table_info(users)")
            columns = [row["name"] for row in await cursor.fetchall()]
            if "headshots" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN headshots INTEGER NOT NULL DEFAULT 0")

            # Миграция 2: добавляем subcategory_id в shop_items, если таблица создана раньше
            cursor_shop = await db.execute("PRAGMA table_info(shop_items)")
            shop_columns = [row["name"] for row in await cursor_shop.fetchall()]
            if "subcategory_id" not in shop_columns:
                await db.execute(
                    "ALTER TABLE shop_items ADD COLUMN subcategory_id INTEGER REFERENCES shop_subcategories(id) ON DELETE SET NULL"
                )

            await db.commit()

    # ==========================================
    # БАЗОВЫЕ МЕЗОВЫЕ МЕТОДЫ ЗАПРОСОВ И НАСТРОЕК
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
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            """INSERT INTO settings (key, value) VALUES (?, ?)
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
               WHERE group_id = ? ORDER BY role, game_nick""",
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
    # ==========================================
    # СДАЧА ЛУТА: ЗОНА, КАТАЛОГ, ОЧЕРЕДЬ И КУЛДАУН
    # ==========================================

    async def get_sell_zone(self) -> tuple[float, float, float]:
        """Возвращает (x, z, radius) зоны сдачи лута из настроек БД."""
        x = float((await self.get_setting("sell_zone_x")) or 4500.0)
        z = float((await self.get_setting("sell_zone_z")) or 8200.0)
        r = float((await self.get_setting("sell_zone_radius")) or 15.0)
        return x, z, r

    async def set_sell_zone(self, x: float, z: float, radius: float | None = None) -> None:
        await self.set_setting("sell_zone_x", str(round(x, 2)))
        await self.set_setting("sell_zone_z", str(round(z, 2)))
        if radius is not None:
            await self.set_setting("sell_zone_radius", str(round(radius, 1)))

    async def get_sell_groups(self):
        return await self.fetchall("SELECT * FROM sell_groups ORDER BY name ASC")

    async def get_sell_group(self, group_id: int):
        return await self.fetchone("SELECT * FROM sell_groups WHERE id = ?", (group_id,))

    async def add_sell_group(self, name: str) -> int:
        return await self.execute("INSERT INTO sell_groups (name) VALUES (?)", (name.strip(),))

    async def rename_sell_group(self, group_id: int, new_name: str) -> None:
        await self.execute("UPDATE sell_groups SET name = ? WHERE id = ?", (new_name.strip(), group_id))

    async def delete_sell_group(self, group_id: int) -> None:
        await self.execute("DELETE FROM sell_groups WHERE id = ?", (group_id,))

    async def get_sell_items(self, group_id: int | None = None, all_items: bool = False):
        if all_items:
            return await self.fetchall(
                """SELECT si.*, sg.name AS group_name
                   FROM sell_items si
                   LEFT JOIN sell_groups sg ON si.group_id = sg.id
                   ORDER BY sg.name ASC, si.display_name ASC"""
            )
        if group_id is None:
            return await self.fetchall(
                "SELECT * FROM sell_items WHERE group_id IS NULL ORDER BY display_name ASC"
            )
        return await self.fetchall(
            "SELECT * FROM sell_items WHERE group_id = ? ORDER BY display_name ASC",
            (group_id,),
        )

    async def get_sell_item(self, item_id: int):
        return await self.fetchone("SELECT * FROM sell_items WHERE id = ?", (item_id,))

    async def add_sell_item(
        self,
        display_name: str,
        game_classname: str,
        price: int,
        group_id: int | None = None,
    ) -> int:
        return await self.execute(
            """INSERT INTO sell_items (group_id, display_name, game_classname, price)
               VALUES (?, ?, ?, ?)""",
            (group_id, display_name.strip(), game_classname.strip(), price),
        )

    async def update_sell_item_field(self, item_id: int, field: str, value: Any) -> None:
        allowed_fields = {"display_name", "game_classname", "price", "group_id"}
        if field not in allowed_fields:
            raise ValueError(f"Недопустимое поле: {field}")
        await self.execute(f"UPDATE sell_items SET {field} = ? WHERE id = ?", (value, item_id))

    async def delete_sell_item(self, item_id: int) -> None:
        await self.execute("DELETE FROM sell_items WHERE id = ?", (item_id,))

    # --- ОЧЕРЕДЬ ЗАЯВОК И КУЛДАУН ---

    async def get_user_active_sell_session(self, user_id: int):
        """Возвращает текущую незавершённую заявку игрока (в очереди, на вышке или в ожидании деспавна)."""
        return await self.fetchone(
            """SELECT * FROM sell_sessions
               WHERE user_id = ? AND status IN ('queued', 'checking_pos', 'ready_to_drop', 'waiting_cleanup')
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        )

    async def get_sell_cooldown_remaining(self, user_id: int, cooldown_seconds: int = 600) -> int:
        """Возвращает оставшееся время кулдауна (в секундах) после последней успешной сдачи, или 0."""
        row = await self.fetchone(
            """SELECT CAST((julianday('now') - julianday(finished_at)) * 86400 AS INTEGER) AS elapsed
               FROM sell_sessions
               WHERE user_id = ?
                 AND finished_at IS NOT NULL
                 AND (status = 'completed' OR paid_amount > 0)
               ORDER BY id DESC LIMIT 1""",
            (user_id,),
        )
        if not row or row["elapsed"] is None:
            return 0
        elapsed = int(row["elapsed"])
        return max(0, cooldown_seconds - elapsed)

    async def create_sell_session(
        self,
        user_id: int,
        telegram_id: int,
        game_nick: str,
        items_json: str,
    ) -> int:
        return await self.execute(
            """INSERT INTO sell_sessions (user_id, telegram_id, game_nick, items_json, status)
               VALUES (?, ?, ?, ?, 'queued')""",
            (user_id, telegram_id, game_nick, items_json),
        )

    async def get_sell_queue_position(self, session_id: int) -> int:
        """Возвращает позицию заявки в очереди на вышку (1 — сейчас обслуживается)."""
        row = await self.fetchone(
            """SELECT COUNT(*) AS pos FROM sell_sessions
               WHERE status IN ('queued', 'checking_pos', 'ready_to_drop')
                 AND id <= ?""",
            (session_id,),
        )
        return int(row["pos"]) if row else 1

    async def get_tower_occupant(self):
        """Возвращает заявку игрока, который сейчас занимает вышку ('checking_pos' или 'ready_to_drop')."""
        return await self.fetchone(
            """SELECT * FROM sell_sessions
               WHERE status IN ('checking_pos', 'ready_to_drop')
               ORDER BY id ASC LIMIT 1"""
        )

    async def get_next_queued_session(self):
        """Возвращает следующую заявку в очереди ('queued')."""
        return await self.fetchone(
            """SELECT * FROM sell_sessions
               WHERE status = 'queued'
               ORDER BY id ASC LIMIT 1"""
        )

    async def get_cleanup_watching_sessions(self):
        """Возвращает все заявки, для которых нужно отслеживать деспавн предметов в зоне."""
        return await self.fetchall(
            """SELECT * FROM sell_sessions
               WHERE status IN ('ready_to_drop', 'waiting_cleanup')
               ORDER BY id ASC"""
        )
