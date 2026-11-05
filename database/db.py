import os
from typing import Any

import aiosqlite


class Database:
    def __init__(self, path: str):
        self.path = path

    async def connect(self) -> aiosqlite.Connection:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        return db

    async def init(self) -> None:
        async with await self.connect() as db:
            await db.executescript(
                """
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
                """
            )
            await db.commit()

    async def fetchone(self, query: str, params: tuple = ()) -> Any:
        async with await self.connect() as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()) -> list[Any]:
        async with await self.connect() as db:
            cursor = await db.execute(query, params)
            return await cursor.fetchall()

    async def execute(self, query: str, params: tuple = ()) -> int:
        async with await self.connect() as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.lastrowid

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
        async with await self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")

            sender = await (
                await db.execute(
                    "SELECT id, balance FROM users WHERE id = ?",
                    (sender_id,),
                )
            ).fetchone()
            receiver = await (
                await db.execute(
                    "SELECT id FROM users WHERE game_nick = ? COLLATE NOCASE",
                    (receiver_nick,),
                )
            ).fetchone()

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
                """INSERT INTO transactions
                   (sender_id, receiver_id, amount, kind)
                   VALUES (?, ?, ?, 'transfer')""",
                (sender["id"], receiver["id"], amount),
            )
            await db.commit()
            return True, "Перевод выполнен."
