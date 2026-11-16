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

                -- ТАБЛИЦА ПОДКАТЕГОРИЙ (ГРУПП КАТАЛОГА) --
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

    async def transfer(self, sender_id: int, receiver_nick: str, amount: int):
        """Атомарный перевод: при ошибке баланс не меняется."""
        if amount <= 0:
            return False, "Сумма перевода должна быть больше нуля."

        async with self.connect() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")

                cursor_sender = await db.execute(
                    "SELECT id, balance FROM users WHERE id = ?",
                    (sender_id,),
                )
                sender = await cursor_sender.fetchone()

                cursor_receiver = await db.execute(
                    "SELECT id FROM users WHERE game_nick = ? COLLATE NOCASE",
                    (receiver_nick,),
                )
                receiver = await cursor_receiver.fetchone()

                if not sender:
                    await db.rollback()
                    return False, "Отправитель не найден."
                if not receiver:
                    await db.rollback()
                    return False, "Игрок с таким ником не найден."
                if sender["id"] == receiver["id"]:
                    await db.rollback()
                    return False, "Нельзя перевести валюту самому себе."
                if sender["balance"] < amount:
                    await db.rollback()
                    return False, "Недостаточно средств."

                await db.execute(
                    "UPDATE users SET balance = balance - ? WHERE id = ?",
                    (amount, sender["id"]),
                )
                await db.execute(
                    "UPDATE users SET balance = balance + ? WHERE id = ?",
                    (amount, receiver["id"]),
                )
                await db.execute(
                    """INSERT INTO transactions (sender_id, receiver_id, amount, kind)
                       VALUES (?, ?, ?, 'transfer')""",
                    (sender["id"], receiver["id"], amount),
                )
                await db.commit()
                return True, f"✅ Переведено {amount} 🪙 игроку {receiver_nick}."
            except Exception as e:
                await db.rollback()
                return False, f"Ошибка при переводе: {e}"

    # ==========================================
    # ПОДКАТЕГОРИИ (ГРУППЫ КАТАЛОГА) И ТОВАРЫ
    # ==========================================

    async def get_subcategories(self, category: str) -> list[Any]:
        """Возвращает список подкатегорий для выбранного раздела."""
        return await self.fetchall(
            "SELECT * FROM shop_subcategories WHERE category = ? ORDER BY name",
            (category,),
        )

    async def get_subcategory(self, sub_id: int) -> Any:
        """Возвращает одну подкатегорию по ID."""
        return await self.fetchone("SELECT * FROM shop_subcategories WHERE id = ?", (sub_id,))

    async def add_subcategory(self, category: str, name: str) -> int:
        """Создаёт новую подкатегорию внутри основного раздела."""
        return await self.execute(
            "INSERT INTO shop_subcategories (category, name) VALUES (?, ?)",
            (category, name.strip()),
        )

    async def delete_subcategory(self, sub_id: int) -> None:
        """Удаляет подкатегорию (товары из неё переносятся в корень основного раздела)."""
        await self.execute("UPDATE shop_items SET subcategory_id = NULL WHERE subcategory_id = ?", (sub_id,))
        await self.execute("DELETE FROM shop_subcategories WHERE id = ?", (sub_id,))

    async def get_shop_items(self, category: str | None = None, subcategory_id: int | None = None) -> list[Any]:
        """Возвращает товары (все, только из корня раздела или из конкретной подкатегории)."""
        if category is None:
            return await self.fetchall("SELECT * FROM shop_items ORDER BY category, display_name")
        if subcategory_id is not None:
            return await self.fetchall(
                "SELECT * FROM shop_items WHERE category = ? AND subcategory_id = ? ORDER BY display_name",
                (category, subcategory_id),
            )
        return await self.fetchall(
            "SELECT * FROM shop_items WHERE category = ? AND subcategory_id IS NULL ORDER BY display_name",
            (category,),
        )

    async def get_shop_item(self, item_id: int) -> Any:
        """Возвращает один товар по его ID."""
        return await self.fetchone("SELECT * FROM shop_items WHERE id = ?", (item_id,))

    async def add_shop_item(
        self,
        category: str,
        display_name: str,
        game_classname: str,
        price: int,
        subcategory_id: int | None = None,
    ) -> int:
        """Добавляет новый товар в таблицу shop_items и возвращает его ID."""
        return await self.execute(
            """INSERT INTO shop_items (category, subcategory_id, display_name, game_classname, price)
               VALUES (?, ?, ?, ?, ?)""",
            (category, subcategory_id, display_name, game_classname, price),
        )

    async def update_shop_item_field(self, item_id: int, field: str, value: Any) -> None:
        """Обновляет одно из разрешённых полей товара."""
        allowed_fields = {"display_name", "game_classname", "price", "category", "subcategory_id"}
        if field not in allowed_fields:
            raise ValueError(f"Недопустимое поле для обновления: {field}")

        await self.execute(
            f"UPDATE shop_items SET {field} = ? WHERE id = ?",
            (value, item_id),
        )

    async def delete_shop_item(self, item_id: int) -> None:
        """Удаляет товар из магазина по его ID."""
        await self.execute("DELETE FROM shop_items WHERE id = ?", (item_id,))

    # ==========================================
    # ЗАКАЗЫ МАГАЗИНА (SHOP_ORDERS)
    # ==========================================

    async def create_shop_order(
        self,
        user_id: int,
        telegram_id: int,
        items_summary: str,
        x: float,
        z: float,
    ) -> int:
        """Сохраняет оформленный заказ со статусом pending для отправки уведомления после рестарта."""
        return await self.execute(
            """INSERT INTO shop_orders (user_id, telegram_id, items_summary, x, z, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (user_id, telegram_id, items_summary, x, z),
        )

    async def pop_pending_shop_orders(self) -> list[Any]:
        """Атомарно забирает все ожидающие заказы и переводит их в статус delivered."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT * FROM shop_orders WHERE status = 'pending'")
            rows = await cursor.fetchall()
            if rows:
                await db.execute("UPDATE shop_orders SET status = 'delivered' WHERE status = 'pending'")
            await db.commit()
            return rows
