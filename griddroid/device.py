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
    tags: List[str] = field(default_factory=list)
    status: DeviceStatus = DeviceStatus.DISCONNECTED
    screen_on: bool = True
    battery_level: int = -1
    streaming: bool = False
    stream_pid: Optional[int] = None
    last_seen: float = field(default_factory=time.time)
    selected: bool = True  # selezionato per broadcast di default
    error: str = ""
    stream_failures: int = 0
    next_stream_attempt: float = 0.0

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
            "tags": self.tags,
            "model": self.info.model,
            "product": self.info.product,
            "usb_port": self.info.usb_port,
            "status": self.status.value,
            "screen_on": self.screen_on,
            "battery_level": self.battery_level,
            "streaming": self.streaming,
            "selected": self.selected,
            "display_name": self.display_name,
            "error": self.error,
        }
