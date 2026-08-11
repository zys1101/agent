"""本地 SQLite：待回传结果缓存 + 审计日志（AWF §7）。

云端不可达时，Worker 把已完成结果写入 pending_results；
网络恢复后（任务重新投递）按 task_id 重传，避免重复报价。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class QuoteCache:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_results (
                    task_id   TEXT PRIMARY KEY,
                    payload   TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts  INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts      TEXT NOT NULL,
                    task_id TEXT,
                    stage   TEXT,
                    message TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_cache (
                    key        TEXT PRIMARY KEY,
                    model      TEXT NOT NULL,
                    schema     TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    # ---- 待回传结果 ----

    def save_pending(self, task_id: str, payload: dict) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO pending_results (task_id, payload, created_at, attempts)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(task_id) DO UPDATE SET payload = excluded.payload
                """,
                (task_id, json.dumps(payload, ensure_ascii=False), _now()),
            )

    def load_pending(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT task_id, payload, attempts FROM pending_results"
            ).fetchall()
        return [(task_id, json.loads(payload)) for task_id, payload, _ in rows]

    def pending_for(self, task_id: str) -> dict | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM pending_results WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete_pending(self, task_id: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM pending_results WHERE task_id = ?", (task_id,))

    def count_pending(self) -> int:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM pending_results").fetchone()[0]

    # ---- 审计 ----

    def audit(self, task_id: str | None, stage: str, message: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, task_id, stage, message) VALUES (?, ?, ?, ?)",
                (_now(), task_id, stage, message[:2000]),
            )

    def recent_audit(self, limit: int = 50) -> list[dict]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ts, task_id, stage, message FROM audit_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"ts": ts, "task_id": task_id, "stage": stage, "message": message}
            for ts, task_id, stage, message in rows
        ]

    # ---- LLM 结果缓存 ----

    def get_llm_cache(self, key: str) -> str | None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM llm_cache WHERE key = ?",
                (key,),
            ).fetchone()
        return row[0] if row else None

    def set_llm_cache(self, key: str, model: str, schema: str, payload: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO llm_cache (key, model, schema, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    model = excluded.model,
                    schema = excluded.schema,
                    payload = excluded.payload,
                    created_at = excluded.created_at
                """,
                (key, model, schema, payload, _now()),
            )

    def delete_llm_cache(self, key: str) -> None:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM llm_cache WHERE key = ?", (key,))

    def count_llm_cache(self) -> int:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
