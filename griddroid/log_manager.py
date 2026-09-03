"""Sistema di log centralizzato per GridDroid."""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional


class LogLevel(str, enum.Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    SUCCESS = "success"


@dataclass
class LogEntry:
    timestamp: float
    level: LogLevel
    serial: str  # vuoto per log globali
    message: str

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp,
            "level": self.level.value,
            "serial": self.serial,
            "message": self.message,
        }


class LogManager:
    """Raccoglie log e li distribuisce ai client WebSocket."""

    MAX_HISTORY = 2000

    def __init__(self) -> None:
        self._entries: Deque[LogEntry] = deque(maxlen=self.MAX_HISTORY)
        self._subscribers: List[asyncio.Queue] = []

    def log(
        self,
        message: str,
        level: LogLevel = LogLevel.INFO,
        serial: str = "",
    ) -> LogEntry:
        entry = LogEntry(
            timestamp=time.time(),
            level=level,
            serial=serial,
            message=message,
        )
        self._entries.append(entry)
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass
        return entry

    def info(self, message: str, serial: str = "") -> LogEntry:
        return self.log(message, LogLevel.INFO, serial)

    def warn(self, message: str, serial: str = "") -> LogEntry:
        return self.log(message, LogLevel.WARN, serial)

    def error(self, message: str, serial: str = "") -> LogEntry:
        return self.log(message, LogLevel.ERROR, serial)

    def success(self, message: str, serial: str = "") -> LogEntry:
        return self.log(message, LogLevel.SUCCESS, serial)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def history(self, limit: int = 200) -> List[dict]:
        entries = list(self._entries)[-limit:]
        return [e.to_dict() for e in entries]


# Singleton globale
logs = LogManager()
