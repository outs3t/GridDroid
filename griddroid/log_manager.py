"""Sistema di log centralizzato per GridDroid."""

from __future__ import annotations

import asyncio
import enum
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple


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

    MAX_SESSION_FILES = 20

    def __init__(self) -> None:
        self._entries: Deque[LogEntry] = deque(maxlen=self.MAX_HISTORY)
        self._subscribers: List[asyncio.Queue] = []
        self._throttle: Dict[Tuple[str, str], float] = {}
        self._file = None
        self._file_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # File di sessione
    # ------------------------------------------------------------------

    def start_session_file(self, logs_dir: Optional[Path] = None) -> Optional[Path]:
        """Apre il file di log per questo avvio.

        Ogni riga viene scritta con flush immediato: se l'app crasha o viene
        chiusa di forza il log resta comunque completo su disco.
        """
        if logs_dir is None:
            from .config import CONFIG_DIR
            logs_dir = CONFIG_DIR / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            self._rotate_session_files(logs_dir)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = logs_dir / f"sessione-{stamp}.log"
            self._file = path.open("a", encoding="utf-8", buffering=1)
            self._file_path = path
            return path
        except Exception:
            self._file = None
            self._file_path = None
            return None

    def _rotate_session_files(self, logs_dir: Path) -> None:
        """Tiene solo i file di sessione piu' recenti."""
        try:
            files = sorted(logs_dir.glob("sessione-*.log"))
            for old in files[: max(0, len(files) - self.MAX_SESSION_FILES + 1)]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    @property
    def session_file(self) -> Optional[Path]:
        return self._file_path

    def close_session_file(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass
            self._file = None

    def _write_line(self, entry: LogEntry) -> None:
        if self._file is None:
            return
        try:
            ts = time.strftime("%H:%M:%S", time.localtime(entry.timestamp))
            serial = entry.serial or "-"
            self._file.write(
                f"{ts} [{entry.level.value.upper():7}] {serial:20} {entry.message}\n"
            )
        except Exception:
            pass

    def log(
        self,
        message: str,
        level: LogLevel = LogLevel.INFO,
        serial: str = "",
        throttle_s: float = 0.0,
    ) -> Optional[LogEntry]:
        if throttle_s > 0:
            key = (message, serial)
            now = time.time()
            if key in self._throttle and now - self._throttle[key] < throttle_s:
                return None
            self._throttle[key] = now
        entry = LogEntry(
            timestamp=time.time(),
            level=level,
            serial=serial,
            message=message,
        )
        self._entries.append(entry)
        self._write_line(entry)
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass
        return entry

    def info(self, message: str, serial: str = "", throttle_s: float = 0.0) -> Optional[LogEntry]:
        return self.log(message, LogLevel.INFO, serial, throttle_s)

    def warn(self, message: str, serial: str = "", throttle_s: float = 0.0) -> Optional[LogEntry]:
        return self.log(message, LogLevel.WARN, serial, throttle_s)

    def error(self, message: str, serial: str = "", throttle_s: float = 0.0) -> Optional[LogEntry]:
        return self.log(message, LogLevel.ERROR, serial, throttle_s)

    def success(self, message: str, serial: str = "", throttle_s: float = 0.0) -> Optional[LogEntry]:
        return self.log(message, LogLevel.SUCCESS, serial, throttle_s)

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

    def save_to_file(self, logs_dir: Optional[Path] = None) -> Optional[Path]:
        """Salva tutti i log accumulati in un file JSONL datato."""
        if logs_dir is None:
            from .config import CONFIG_DIR
            logs_dir = CONFIG_DIR / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            path = logs_dir / f"log-{timestamp}.jsonl"
            with path.open("w", encoding="utf-8") as f:
                for entry in self._entries:
                    f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            return path
        except Exception:
            return None


# Singleton globale
logs = LogManager()
