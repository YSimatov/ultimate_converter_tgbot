"""SQLite operations run in threads, with one connection per operation."""

import asyncio
import sqlite3
from contextlib import closing

DEFAULTS = {"image": "90", "audio": "192", "video": "23"}


class Database:
    def __init__(self, path):
        self.path = path

    def _execute(self, sql, args=(), fetch=False):
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            cursor = db.execute(sql, args)
            rows = [dict(row) for row in cursor.fetchall()] if fetch else []
            db.commit()
            return rows

    async def init(self):
        def initialize():
            with closing(sqlite3.connect(self.path, timeout=15)) as db:
                db.executescript("""
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS settings (
                        user_id INTEGER PRIMARY KEY, image TEXT NOT NULL DEFAULT '90',
                        audio TEXT NOT NULL DEFAULT '192', video TEXT NOT NULL DEFAULT '23');
                    CREATE TABLE IF NOT EXISTS history (
                        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL,
                        source TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
                        created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M', 'now')));
                    CREATE INDEX IF NOT EXISTS history_user ON history(user_id, id);
                """)
                db.commit()

        await asyncio.to_thread(initialize)

    async def settings(self, uid):
        rows = await asyncio.to_thread(
            self._execute,
            "SELECT image,audio,video FROM settings WHERE user_id=?",
            (uid,),
            True,
        )
        return rows[0] if rows else DEFAULTS.copy()

    async def set(self, uid, key, value):
        allowed = {
            "image": ("70", "90", "100"),
            "audio": ("128", "192", "320"),
            "video": ("28", "23", "18"),
        }
        if key not in allowed or value not in allowed[key]:
            raise ValueError("Invalid quality")
        await asyncio.to_thread(
            self._execute, "INSERT OR IGNORE INTO settings(user_id) VALUES (?)", (uid,)
        )
        await asyncio.to_thread(
            self._execute, f"UPDATE settings SET {key}=? WHERE user_id=?", (value, uid)
        )

    async def record(self, uid, name, source, target, status):
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO history(user_id,name,source,target,status) VALUES (?,?,?,?,?)",
            (uid, name[:200], source, target, status),
        )
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM history WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM history WHERE user_id=? ORDER BY id DESC LIMIT 100)",
            (uid, uid),
        )

    async def history(self, uid):
        return await asyncio.to_thread(
            self._execute,
            "SELECT * FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,),
            True,
        )

    async def clear(self, uid):
        await asyncio.to_thread(
            self._execute, "DELETE FROM history WHERE user_id=?", (uid,)
        )
