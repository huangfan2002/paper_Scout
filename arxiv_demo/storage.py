"""Small SQLite store for cache, metadata and search history."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn, conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, value BLOB NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, value TEXT NOT NULL, saved REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS searches (
                    id TEXT PRIMARY KEY, value TEXT NOT NULL, saved REAL NOT NULL
                );
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def cached(self, key: str) -> bytes | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value, expires FROM cache WHERE key=?", (key,)).fetchone()
        if row and row[1] > time.time():
            return bytes(row[0])
        return None

    def cache(self, key: str, value: bytes, ttl: float = 86400):
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                         (key, value, time.time() + ttl))
            conn.execute("DELETE FROM cache WHERE expires < ?", (time.time(),))

    def save_papers(self, papers: list[dict]):
        with closing(self.connect()) as conn, conn:
            conn.executemany("INSERT OR REPLACE INTO papers VALUES (?, ?, ?)",
                             [(p["id"], json.dumps(p, ensure_ascii=False), time.time()) for p in papers])

    def papers(self, ids: list[str]) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = [conn.execute("SELECT value FROM papers WHERE id=?", (key,)).fetchone() for key in ids]
        return [json.loads(row[0]) for row in rows if row]

    def save_search(self, search_id: str, value: dict):
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO searches VALUES (?, ?, ?)",
                         (search_id, json.dumps(value, ensure_ascii=False), time.time()))

    def search(self, search_id: str) -> dict | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM searches WHERE id=?", (search_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def history(self) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute("SELECT id,value FROM searches ORDER BY saved DESC LIMIT 20").fetchall()
        items = []
        for key, value in rows:
            data = json.loads(value)
            items.append({"id": key, "query": data.get("query", ""), "options": data.get("options", {}),
                          "count": len(data.get("papers", [])), "created_at": data.get("created_at"),
                          "source": data.get("source", "arxiv"), "warnings": data.get("warnings", [])})
        return items
