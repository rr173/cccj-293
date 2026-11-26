"""Server-side clock.

Every time decision in the service goes through a ``Clock`` so that tests can
drive a deterministic virtual clock instead of sleeping. Production uses
``RealClock`` (UTC wall clock); tests inject ``VirtualClock`` through
``create_app(..., clock=...)``.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone


class Clock:
    def now(self) -> datetime:  # pragma: no cover - interface
        raise NotImplementedError

    def now_iso(self) -> str:
        return self.now().isoformat()

    def iso(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    def parse(self, raw: str | None) -> datetime | None:
        if raw is None:
            return None
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def micros(self, dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1_000_000)

    def from_micros(self, value: int) -> datetime:
        return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class VirtualClock(Clock):
    """Deterministic clock whose time only moves when a test advances it."""

    def __init__(self, start: datetime | None = None):
        if start is None:
            start = datetime(2026, 11, 26, 9, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start
        self._lock = threading.RLock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float = 0, *, microseconds: int = 0) -> datetime:
        with self._lock:
            self._now = self._now + timedelta(
                seconds=seconds, microseconds=microseconds
            )
            return self._now

    def set(self, dt: datetime) -> datetime:
        with self._lock:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            self._now = dt
            return self._now
