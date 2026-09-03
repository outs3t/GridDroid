"""Canale di controllo scrcpy: iniezione di eventi nativi a latenza minima.

Invece di usare `adb shell input tap` (che avvia un processo per ogni evento,
~200-400ms di latenza), qui parliamo direttamente col control socket di
scrcpy-server usando il suo protocollo binario. La latenza scende a ~1ms e
si ottengono drag fluidi, multi-touch e scroll reali.

Riferimento protocollo: scrcpy ControlMessageReader / control_msg.c
"""

from __future__ import annotations

import asyncio
import struct
from typing import Optional

from .log_manager import logs

# --------------------------------------------------------------------------
# Tipi di messaggio (stabili da scrcpy 1.x)
# --------------------------------------------------------------------------

TYPE_INJECT_KEYCODE = 0
TYPE_INJECT_TEXT = 1
TYPE_INJECT_TOUCH_EVENT = 2
TYPE_INJECT_SCROLL_EVENT = 3
TYPE_BACK_OR_SCREEN_ON = 4
TYPE_EXPAND_NOTIFICATION_PANEL = 5
TYPE_EXPAND_SETTINGS_PANEL = 6
TYPE_COLLAPSE_PANELS = 7

# Azioni MotionEvent Android
ACTION_DOWN = 0
ACTION_UP = 1
ACTION_MOVE = 2

# Azioni KeyEvent Android
KEY_ACTION_DOWN = 0
KEY_ACTION_UP = 1

# Bottone primario del mouse (AMOTION_EVENT_BUTTON_PRIMARY)
BUTTON_PRIMARY = 1

# Pointer ID virtuale per il mouse (come scrcpy: -1 unsigned)
POINTER_ID_MOUSE = 0xFFFFFFFFFFFFFFFF


def _pressure_u16(pressure: float) -> int:
    """Converte una pressione 0.0-1.0 nel fixed point u16 usato da scrcpy."""
    if pressure <= 0.0:
        return 0
    if pressure >= 1.0:
        return 0xFFFF
    return int(pressure * 0xFFFF)


def _scroll_i16(value: float) -> int:
    """Converte uno scroll -1.0..1.0 nel fixed point i16 usato da scrcpy."""
    if value >= 1.0:
        return 0x7FFF
    if value <= -1.0:
        return -0x8000
    return int(value * 0x7FFF)


class ControlChannel:
    """Wrapper sul control socket di scrcpy per un singolo dispositivo."""

    def __init__(self, serial: str, writer: asyncio.StreamWriter) -> None:
        self.serial = serial
        self._writer = writer
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed and not self._writer.is_closing()

    async def _send(self, payload: bytes) -> bool:
        """Invia un messaggio di controllo. Ritorna False se il canale è caduto."""
        if self._closed:
            return False
        try:
            async with self._lock:
                self._writer.write(payload)
                await self._writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._closed = True
            return False

    async def close(self) -> None:
        """Chiude il canale di controllo e attende la liberazione del socket."""
        if self._closed:
            return
        self._closed = True
        try:
            if not self._writer.is_closing():
                self._writer.close()
                try:
                    await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._writer = None

    # ------------------------------------------------------------------
    # Touch
    # ------------------------------------------------------------------

    async def touch(
        self, action: int, x: int, y: int, width: int, height: int,
        pointer_id: int = POINTER_ID_MOUSE, pressure: float = 1.0,
        buttons: int = BUTTON_PRIMARY,
    ) -> bool:
        """Inietta un evento touch.

        Le coordinate sono nello spazio del frame video (`width`x`height`):
        è il server a scalarle sulla risoluzione nativa del dispositivo,
        quindi il broadcast su schermi di dimensioni diverse è automatico.
        """
        if width <= 0 or height <= 0:
            return False

        # Clamp nei limiti del frame
        x = max(0, min(int(x), width - 1))
        y = max(0, min(int(y), height - 1))

        action_button = BUTTON_PRIMARY
        if action == ACTION_UP:
            buttons = 0
            pressure = 0.0

        payload = struct.pack(
            ">BBQiiHHHII",
            TYPE_INJECT_TOUCH_EVENT,
            action,
            pointer_id,
            x, y,
            width, height,
            _pressure_u16(pressure),
            action_button,
            buttons,
        )
        return await self._send(payload)

    async def scroll(
        self, x: int, y: int, width: int, height: int,
        hscroll: float = 0.0, vscroll: float = 0.0, buttons: int = 0,
    ) -> bool:
        """Inietta un evento di scroll (rotella del mouse)."""
        if width <= 0 or height <= 0:
            return False
        x = max(0, min(int(x), width - 1))
        y = max(0, min(int(y), height - 1))

        payload = struct.pack(
            ">BiiHHhhI",
            TYPE_INJECT_SCROLL_EVENT,
            x, y,
            width, height,
            _scroll_i16(hscroll),
            _scroll_i16(vscroll),
            buttons,
        )
        return await self._send(payload)

    # ------------------------------------------------------------------
    # Tastiera
    # ------------------------------------------------------------------

    async def keycode(
        self, keycode: int, action: int = KEY_ACTION_DOWN,
        repeat: int = 0, metastate: int = 0,
    ) -> bool:
        """Inietta un singolo evento tasto (down o up)."""
        payload = struct.pack(
            ">BBIII",
            TYPE_INJECT_KEYCODE,
            action,
            keycode,
            repeat,
            metastate,
        )
        return await self._send(payload)

    async def key_press(self, keycode: int, metastate: int = 0) -> bool:
        """Down + up: pressione completa di un tasto."""
        ok = await self.keycode(keycode, KEY_ACTION_DOWN, 0, metastate)
        if not ok:
            return False
        return await self.keycode(keycode, KEY_ACTION_UP, 0, metastate)

    async def text(self, value: str) -> bool:
        """Inietta testo UTF-8 (supporta accenti e caratteri italiani)."""
        if not value:
            return False
        raw = value.encode("utf-8")
        payload = struct.pack(">BI", TYPE_INJECT_TEXT, len(raw)) + raw
        return await self._send(payload)

    # ------------------------------------------------------------------
    # Comandi di sistema
    # ------------------------------------------------------------------

    async def back_or_screen_on(self, action: int = KEY_ACTION_DOWN) -> bool:
        """BACK se lo schermo è accesso, altrimenti accende lo schermo."""
        payload = struct.pack(">BB", TYPE_BACK_OR_SCREEN_ON, action)
        return await self._send(payload)

    async def expand_notification_panel(self) -> bool:
        return await self._send(struct.pack(">B", TYPE_EXPAND_NOTIFICATION_PANEL))

    async def expand_settings_panel(self) -> bool:
        return await self._send(struct.pack(">B", TYPE_EXPAND_SETTINGS_PANEL))

    async def collapse_panels(self) -> bool:
        return await self._send(struct.pack(">B", TYPE_COLLAPSE_PANELS))
