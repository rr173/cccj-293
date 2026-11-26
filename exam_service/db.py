"""SQLite access layer.

A single process-wide write lock serializes every read-modify-write
transaction, so optimistic-revision checks, idempotency inserts and publish
conflict checks are atomic even when FastAPI runs sync endpoints in its
thread pool. Reads use their own WAL connections and never take the lock.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_WRITE_LOCK = threading.RLock()


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Initialize the file (and WAL mode) up front.
        conn = self._connect()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=30, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run a mutating block inside the global write lock."""
        with _WRITE_LOCK:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()
