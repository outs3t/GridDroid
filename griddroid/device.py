"""Modello del dispositivo Android e relativi stati."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import List, Optional


class DeviceStatus(str, enum.Enum):
    """Stato di connessione del dispositivo."""
    ONLINE = "online"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    DISCONNECTED = "disconnected"


# Seriali che l'utente ha esplicitamente messo a schermo spento.
# Serve allo stream engine: a ogni riavvio di scrcpy manda KEYCODE_WAKEUP
# per garantire un display attivo, e cosi' risvegliava i device appena
# bloccati (il blocco schermo sembrava fallire a caso). Vive qui perche'
# device.py e' importabile sia da adb_manager che da stream_engine senza
# creare cicli di import.
SCREEN_OFF_REQUESTED: set = set()


@dataclass
class DeviceInfo:
    """Informazioni statiche ricavate da ADB."""
    serial: str
    model: str = ""
    product: str = ""
    transport_id: str = ""
    usb_port: str = ""


@dataclass
class DeviceState:
    """Stato runtime di un singolo dispositivo nella farm."""
    info: DeviceInfo
    label: str = ""
    label_color: str = ""
    tags: List[str] = field(default_factory=list)
    order: int = 0
    status: DeviceStatus = DeviceStatus.DISCONNECTED
    screen_on: bool = True
    battery_level: int = -1
    streaming: bool = False
    stream_pid: Optional[int] = None
    last_seen: float = field(default_factory=time.time)
    selected: bool = False
    played: bool = False  # nascosto dalla griglia come "giocato"
    skipped: bool = False  # nascosto dalla griglia come "non giocato"
    autoclick: bool = False  # auto-clicker attivo
    error: str = ""
    stream_failures: int = 0
    next_stream_attempt: float = 0.0
    # Porta del server adb che enumera questo device (5037 standard,
    # 5038 = server QuickForward/Panda). I comandi -s vanno instradati li'.
    adb_port: int = 5037

    @property
    def serial(self) -> str:
        return self.info.serial

    @property
    def display_name(self) -> str:
        return self.label or self.info.model or self.serial

    def to_dict(self) -> dict:
        """Serializza per invio al frontend via WebSocket."""
        return {
            "serial": self.serial,
            "label": self.label,
            "label_color": self.label_color,
            "tags": self.tags,
            "order": self.order,
            "model": self.info.model,
            "product": self.info.product,
            "usb_port": self.info.usb_port,
            "status": self.status.value,
            "screen_on": self.screen_on,
            "battery_level": self.battery_level,
            "streaming": self.streaming,
            "selected": self.selected,
            "played": self.played,
            "skipped": self.skipped,
            "autoclick": self.autoclick,
            "display_name": self.display_name,
            "error": self.error,
        }
