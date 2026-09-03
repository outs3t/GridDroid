"""Relay degli input da PC verso i dispositivi Android: singolo e broadcast.

Usa esclusivamente il canale di controllo scrcpy (eventi binari nativi,
latenza ~1ms). Se il canale non è attivo, l'input non viene inviato.
Niente più `adb shell input` alla cieca.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

# Nessuna finestra di terminale per i processi figli su Windows
if os.name == "nt":
    _SUBPROCESS_KW = {"creationflags": 0x08000000}
else:
    _SUBPROCESS_KW = {}

from . import control_channel as cc
from .adb_manager import AdbManager
from .device import DeviceState, DeviceStatus
from .log_manager import logs


class InputRelay:
    """Gestisce l'invio di comandi di input (tap, swipe, testo, tasti) ai dispositivi."""

    def __init__(self, adb: AdbManager, streams: object = None) -> None:
        self._adb = adb
        self._streams = streams
        self._broadcast_mode = False
        self._focused_serial: Optional[str] = None
        self._recording = False
        self._macro: List[dict] = []
        self._macro_ui: List[Tuple[int, int, int, int]] = []
        self._macro_ui_down: Optional[Tuple[int, int, int, int]] = None
        self._record_start = 0.0
        self._macros: dict[str, dict] = {}
        self._macro_counter = 0
        self._getevent_proc: Optional[asyncio.subprocess.Process] = None
        self._record_device = ""
        self._record_axes = (0, 0, 0, 0)
        self._record_serial = ""

    @property
    def broadcast_mode(self) -> bool:
        return self._broadcast_mode

    @broadcast_mode.setter
    def broadcast_mode(self, value: bool) -> None:
        self._broadcast_mode = value
        mode = "BROADCAST" if value else "SINGOLO"
        logs.info(f"Modalità input: {mode}")

    @property
    def focused_serial(self) -> Optional[str]:
        return self._focused_serial

    @focused_serial.setter
    def focused_serial(self, serial: Optional[str]) -> None:
        self._focused_serial = serial
        if serial:
            logs.info(f"Focus su dispositivo", serial=serial)

    def _get_targets(self) -> List[str]:
        """Ritorna la lista di seriali su cui inviare gli input."""
        if not self._broadcast_mode:
            if self._focused_serial:
                return [self._focused_serial]
            return []

        targets = []
        for serial, dev in self._adb.devices.items():
            if dev.status == DeviceStatus.ONLINE and dev.selected:
                targets.append(serial)
        return targets

    def _control_for(self, serial: str):
        """Ritorna il canale di controllo scrcpy del dispositivo, se attivo."""
        if not self._streams:
            return None
        stream = self._streams.get_stream(serial)
        return stream.control if stream else None

    # ------------------------------------------------------------------
    # Touch nativo (pointer events dal browser)
    # ------------------------------------------------------------------

    async def touch(
        self, action: str, x: int, y: int, width: int, height: int,
        pressure: float = 1.0,
    ) -> None:
        """Inietta un evento touch grezzo: 'down', 'move' o 'up'.

        È il percorso a latenza minima: un singolo messaggio binario sul
        control socket, senza processi adb.
        """
        # Registra la macro anche dai click sullo stream UI (fallback)
        if self._recording and action in ("down", "up"):
            if action == "down":
                self._macro_ui_down = (x, y, width, height)
            elif self._macro_ui_down is not None:
                ux, uy, uw, uh = self._macro_ui_down
                self._macro_ui.append((ux, uy, uw, uh))
                self._macro_ui_down = None

        targets = self._get_targets()
        if not targets:
            return

        action_map = {
            "down": cc.ACTION_DOWN,
            "move": cc.ACTION_MOVE,
            "up": cc.ACTION_UP,
        }
        native_action = action_map.get(action)
        if native_action is None:
            return

        tasks = []
        for serial in targets:
            ctrl = self._control_for(serial)
            if ctrl:
                tasks.append(ctrl.touch(
                    native_action, x, y, width, height, pressure=pressure,
                ))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def tap(self, x: int, y: int, width: int, height: int) -> None:
        """Tap completo: down + up immediato."""
        targets = self._get_targets()
        if not targets:
            return
        tasks = []
        for serial in targets:
            ctrl = self._control_for(serial)
            if ctrl:
                tasks.append(self._native_tap(ctrl, x, y, width, height))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _native_tap(self, ctrl, x: int, y: int, w: int, h: int) -> None:
        await ctrl.touch(cc.ACTION_DOWN, x, y, w, h)
        await ctrl.touch(cc.ACTION_UP, x, y, w, h)

    async def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
        width: int = 0, height: int = 0,
    ) -> None:
        """Swipe interpolato: con canale nativo genera un drag fluido reale."""
        targets = self._get_targets()
        if not targets:
            return
        tasks = []
        for serial in targets:
            ctrl = self._control_for(serial)
            if ctrl:
                tasks.append(self._native_swipe(
                    ctrl, x1, y1, x2, y2, duration_ms, width, height,
                ))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _native_swipe(
        self, ctrl, x1: int, y1: int, x2: int, y2: int,
        duration_ms: int, w: int, h: int,
    ) -> None:
        """Drag nativo interpolato a ~60 step/s per un movimento fluido."""
        steps = max(2, min(60, duration_ms // 16))
        delay = (duration_ms / 1000.0) / steps

        await ctrl.touch(cc.ACTION_DOWN, x1, y1, w, h)
        for i in range(1, steps):
            t = i / steps
            # Ease-out: più naturale, evita che Android lo legga come fling secco
            e = 1 - (1 - t) * (1 - t)
            mx = int(x1 + (x2 - x1) * e)
            my = int(y1 + (y2 - y1) * e)
            await ctrl.touch(cc.ACTION_MOVE, mx, my, w, h)
            await asyncio.sleep(delay)
        await ctrl.touch(cc.ACTION_MOVE, x2, y2, w, h)
        await ctrl.touch(cc.ACTION_UP, x2, y2, w, h)

    async def scroll(
        self, x: int, y: int, width: int, height: int,
        hscroll: float = 0.0, vscroll: float = 0.0,
    ) -> None:
        """Scroll con la rotella del mouse (solo canale nativo)."""
        targets = self._get_targets()
        if not targets:
            return
        tasks = []
        for serial in targets:
            ctrl = self._control_for(serial)
            if ctrl:
                tasks.append(ctrl.scroll(x, y, width, height, hscroll, vscroll))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _scale_coords(self, serial: str, x: int, y: int, w: int, h: int) -> tuple:
        """Scala coordinate dal feed video alla risoluzione nativa del dispositivo."""
        if w <= 0 or h <= 0:
            return (x, y)
        stream = self._streams.get_stream(serial) if self._streams else None
        if stream and stream.native_size[0] > 0:
            nw, nh = stream.native_size
            return (int(x * nw / w), int(y * nh / h))
        return (x, y)

    async def keyevent(self, keycode: int, metastate: int = 0) -> None:
        """Invia un keyevent Android."""
        targets = self._get_targets()
        if not targets:
            return
        tasks = []
        for serial in targets:
            ctrl = self._control_for(serial)
            if ctrl:
                tasks.append(ctrl.key_press(keycode, metastate))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def text(self, text: str) -> None:
        """Invia testo da tastiera. Il canale nativo gestisce UTF-8, quindi
        accenti e caratteri italiani (è é ò à ù) funzionano correttamente."""
        targets = self._get_targets()
        if not targets:
            return

        tasks = []
        for serial in targets:
            ctrl = self._control_for(serial)
            if ctrl:
                tasks.append(ctrl.text(text))
        await asyncio.gather(*tasks, return_exceptions=True)

    async def back(self) -> None:
        await self.keyevent(4)

    async def home(self) -> None:
        await self.keyevent(3)

    async def recent_apps(self) -> None:
        await self.keyevent(187)

    # ------------------------------------------------------------------
    # Macro recorder fisico (getevent -> input tap)
    # ------------------------------------------------------------------

    async def _taps_from_events(
        self, serial: str, device: str, events: List[dict],
        x_min: int, x_max: int, y_min: int, y_max: int
    ) -> List[Tuple[int, int]]:
        """Converte gli eventi grezzi getevent in una lista di coordinate tap
        scalate sulla risoluzione attuale dello schermo."""
        sw, sh = 0, 0
        stream = self._streams.get_stream(serial) if self._streams else None
        if stream and stream.native_size[0] > 0:
            sw, sh = stream.native_size
        else:
            # Fallback: leggi la risoluzione reale dal device
            out = await self._adb.shell(serial, "wm size")
            match = re.search(r"(\d+)x(\d+)", out)
            if match:
                sw, sh = int(match.group(1)), int(match.group(2))
        if sw <= 0 or sh <= 0:
            sw, sh = x_max - x_min, y_max - y_min
        sx = sw / (x_max - x_min) if (x_max - x_min) > 0 else 0
        sy = sh / (y_max - y_min) if (y_max - y_min) > 0 else 0
        x = y = None
        touch_active = False
        taps: List[Tuple[int, int]] = []
        for ev in events:
            if ev["type"] == 0x0003:  # EV_ABS
                if ev["code"] == 0x0035:  # ABS_MT_POSITION_X
                    x = int((ev["value"] - x_min) * sx)
                elif ev["code"] == 0x0036:  # ABS_MT_POSITION_Y
                    y = int((ev["value"] - y_min) * sy)
                elif ev["code"] == 0x0039:  # ABS_MT_TRACKING_ID
                    if ev["value"] == -1 and touch_active and x is not None and y is not None:
                        taps.append((x, y))
                        x = y = None
                    touch_active = ev["value"] >= 0
            elif ev["type"] == 0x0001 and ev["code"] == 0x014a and ev["value"] == 0:  # BTN_TOUCH UP
                if not touch_active and x is not None and y is not None:
                    taps.append((x, y))
                    x = y = None
        return taps

    async def find_touch_device(self, serial: str) -> Optional[Tuple[str, int, int, int, int]]:
        """Trova il device touch e i range degli assi X/Y."""
        rc, out, err = await self._adb.adb_command(
            "shell", "getevent -pl", serial=serial, timeout=10.0,
        )
        if rc != 0:
            logs.warn(f"getevent -pl fallito: {err}")
            return None
        devices: Dict[str, str] = {}
        axes: Dict[int, Tuple[int, int]] = {}
        current: Optional[str] = None
        for line in out.splitlines():
            m = re.match(r"add device \d+: (.+)", line)
            if m:
                current = m.group(1).strip()
                continue
            if current is None:
                continue
            nm = re.search(r'name:\s+"([^"]+)"', line)
            if nm:
                devices[current] = nm.group(1)
                continue
            # esadecimale: "      0035  : value 0, min 0, max 1080"
            am = re.match(r"\s+([0-9a-fA-F]+)\s+:\s+value\s+\d+,\s+min\s+(\d+),\s+max\s+(\d+)", line)
            if am:
                code = int(am.group(1), 16)
                amin = int(am.group(2))
                amax = int(am.group(3))
                axes[code] = (amin, amax)
                continue
            # nomi: "      ABS_MT_POSITION_X : value 0, min 0, max 1080"
            an = re.match(r"\s+(ABS_MT_POSITION_[XY])\s+:\s+value\s+\d+,\s+min\s+(\d+),\s+max\s+(\d+)", line)
            if an:
                name = an.group(1)
                amin = int(an.group(2))
                amax = int(an.group(3))
                code = 0x0035 if name == "ABS_MT_POSITION_X" else 0x0036
                axes[code] = (amin, amax)
        for path, name in devices.items():
            if "touch" in name.lower() or any(k in name.lower() for k in ("touchscreen", "synaptics", "sec_")):
                x = axes.get(0x0035)
                y = axes.get(0x0036)
                if x and y:
                    return path, x[0], x[1], y[0], y[1]
        # ultimo fallback: il primo device con assi validi
        for path in devices:
            x = axes.get(0x0035)
            y = axes.get(0x0036)
            if x and y:
                return path, x[0], x[1], y[0], y[1]
        logs.warn(f"Assi touch non trovati in getevent -pl:\n{out[:500]}")
        return None

    async def start_record(self) -> bool:
        if self._recording:
            return True
        serial = self._focused_serial
        if not serial:
            # Fallback: primo dispositivo online selezionato
            for s, d in self._adb.devices.items():
                if d.status == DeviceStatus.ONLINE and d.selected:
                    serial = s
                    break
        if not serial:
            logs.warn("Nessun dispositivo in focus per la registrazione")
            return False
        self._recording = True
        self._macro = []
        self._macro_ui = []
        self._macro_ui_down = None
        self._record_start = time.monotonic()
        self._record_serial = serial
        self._record_device = ""
        self._record_axes = (0, 0, 0, 0)
        logs.info(f"Registrazione macro avviata su {serial}")
        return True

    async def _read_getevent(self) -> None:
        proc = self._getevent_proc
        if not proc:
            return
        while True:
            try:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                parts = text.split()
                if len(parts) < 3:
                    continue
                raw_type = int(parts[0], 16)
                raw_code = int(parts[1], 16)
                raw_value = int(parts[2], 16)
                # getevent restituisce interi a 32 bit con segno (complemento a 2)
                if raw_value > 0x7FFFFFFF:
                    raw_value -= 0x100000000
                ev = {
                    "type": raw_type,
                    "code": raw_code,
                    "value": raw_value,
                }
                self._macro.append(ev)
            except Exception as exc:
                logs.error(f"Errore lettura getevent: {exc}")
                break

    async def stop_record(self, name: str = "") -> Optional[str]:
        if not self._recording:
            return None
        self._recording = False

        if self._getevent_proc:
            try:
                self._getevent_proc.terminate()
                try:
                    await asyncio.wait_for(self._getevent_proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    self._getevent_proc.kill()
            except Exception:
                pass
            self._getevent_proc = None

        if not self._record_serial:
            logs.info("Registrazione fermata: nessun dispositivo in focus")
            return None

        taps: List[Tuple[int, int]] = []

        # Prima prova con getevent (tocco fisico), altrimenti usa i click dalla UI
        if self._macro and self._record_device:
            taps = await self._taps_from_events(
                self._record_serial, self._record_device, self._macro, *self._record_axes
            )

        if not taps and self._macro_ui:
            taps = [
                self._scale_coords(self._record_serial, x, y, w, h)
                for (x, y, w, h) in self._macro_ui
            ]

        if not taps:
            logs.info("Registrazione fermata: nessun tocco valido trovato")
            return None

        if not name:
            self._macro_counter += 1
            name = f"Macro {self._macro_counter}"

        self._macros[name] = {
            "serial": self._record_serial,
            "device": self._record_device,
            "events": self._macro,
            "taps": taps,
        }
        count = len(self._macro)
        ntaps = len(taps)
        self._macro = []
        logs.info(f"Macro salvata: {name} ({count} eventi, {ntaps} tap)")
        return name

    def list_macros(self) -> List[str]:
        return list(self._macros.keys())

    def delete_macro(self, name: str) -> None:
        if name in self._macros:
            del self._macros[name]
            logs.info(f"Macro eliminata: {name}")

    def clear_macros(self) -> None:
        self._macros = {}

    @property
    def is_recording(self) -> bool:
        return self._recording

    async def replay_macro(self, name: str) -> bool:
        """Ripete i tap registrati con il canale di controllo scrcpy."""
        macro = self._macros.get(name)
        if not macro:
            logs.warn(f"Replay macro: '{name}' non trovata")
            return False

        serial = macro["serial"]
        taps = macro.get("taps", [])
        if not taps:
            logs.warn(f"Replay macro: '{name}' non ha tap validi")
            return False

        ctrl = self._control_for(serial)
        if not ctrl:
            logs.warn(f"Replay macro: nessun controllo attivo per {serial}")
            return False

        # Usa la risoluzione nativa attuale per i parametri w/h del touch
        w, h = 0, 0
        stream = self._streams.get_stream(serial) if self._streams else None
        if stream and stream.native_size[0] > 0:
            w, h = stream.native_size
        if w <= 0 or h <= 0:
            out = await self._adb.shell(serial, "wm size")
            match = re.search(r"(\d+)x(\d+)", out)
            if match:
                w, h = int(match.group(1)), int(match.group(2))

        logs.info(f"Replay {len(taps)} tap su {serial} (nativo {w}x{h})")
        for i, (x, y) in enumerate(taps):
            try:
                await ctrl.touch(cc.ACTION_DOWN, x, y, w, h)
                await ctrl.touch(cc.ACTION_UP, x, y, w, h)
                if i < len(taps) - 1:
                    await asyncio.sleep(0.05)
            except Exception as exc:
                logs.error(f"Errore tap: {exc}")
                return False
        logs.info(f"Replay macro '{name}' completato")
        return True
