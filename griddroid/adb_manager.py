"""Gestione asincrona del daemon ADB: discovery, polling e stato dispositivi."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from typing import Dict, List, Optional, Tuple

# Nessuna finestra di terminale per i processi figli su Windows
if os.name == "nt":
    _SUBPROCESS_KW = {"creationflags": 0x08000000}
else:
    _SUBPROCESS_KW = {}

from .config import AppSettings, load_labels, save_labels, load_tags, save_tags
from .device import DeviceInfo, DeviceState, DeviceStatus
from .log_manager import logs


# Lock globale per serializzare i comandi ADB (più stabile su hub USB).
# Creato lazy per evitare errori in fase di import senza event loop.
_ADB_CMD_LOCK: Optional[asyncio.Lock] = None


def adb_cmd_lock() -> asyncio.Lock:
    global _ADB_CMD_LOCK
    if _ADB_CMD_LOCK is None:
        _ADB_CMD_LOCK = asyncio.Lock()
    return _ADB_CMD_LOCK


# Regex per parsare l'output di `adb devices -l`
_DEVICE_RE = re.compile(
    r"^(?P<serial>\S+)\s+(?P<state>\S+)"
    r"(?:\s+usb:(?P<usb>\S+))?"
    r"(?:\s+product:(?P<product>\S+))?"
    r"(?:\s+model:(?P<model>\S+))?"
    r"(?:\s+device:(?P<device>\S+))?"
    r"(?:\s+transport_id:(?P<tid>\S+))?",
    re.MULTILINE,
)

# Fallback per `adb devices` senza -l: alcune versioni ADB troncano l'output -l
# con molti dispositivi
_DEVICE_RE_PLAIN = re.compile(r"^(?P<serial>\S+)\s+(?P<state>\S+)", re.MULTILINE)


class AdbManager:
    """Worker asincrono per il monitoraggio dei dispositivi ADB."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._adb = settings.adb_path or "adb"
        self._devices: Dict[str, DeviceState] = {}
        self._labels: Dict[str, str] = load_labels()
        self._tags: Dict[str, List[str]] = load_tags()
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._change_callbacks: List = []

    # ------------------------------------------------------------------
    # Proprietà pubbliche
    # ------------------------------------------------------------------

    @property
    def devices(self) -> Dict[str, DeviceState]:
        return self._devices

    def get_device(self, serial: str) -> Optional[DeviceState]:
        return self._devices.get(serial)

    # ------------------------------------------------------------------
    # Etichette
    # ------------------------------------------------------------------

    def set_label(self, serial: str, label: str) -> None:
        self._labels[serial] = label
        if serial in self._devices:
            self._devices[serial].label = label
        save_labels(self._labels)
        logs.info(f"Etichetta '{label}' assegnata a {serial}", serial=serial)

    def set_tags(self, serial: str, tags: List[str]) -> None:
        self._tags[serial] = tags
        if serial in self._devices:
            self._devices[serial].tags = tags
        save_tags(self._tags)
        logs.info(f"Tag {tags} assegnati a {serial}", serial=serial)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if not shutil.which(self._adb):
            logs.error(f"ADB non trovato nel PATH ({self._adb})")
            return
        self._running = True
        logs.info("ADB Manager avviato")
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logs.info("ADB Manager fermato")

    # ------------------------------------------------------------------
    # Comandi ADB
    # ------------------------------------------------------------------

    async def adb_command(
        self, *args: str, serial: Optional[str] = None, timeout: float = 30.0
    ) -> Tuple[int, str, str]:
        """Esegue un comando ADB e ritorna (returncode, stdout, stderr)."""
        async with adb_cmd_lock():
            cmd = [self._adb]
            if serial:
                cmd += ["-s", serial]
            cmd += list(args)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_SUBPROCESS_KW,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                return (
                    proc.returncode or 0,
                    stdout.decode("utf-8", errors="replace"),
                    stderr.decode("utf-8", errors="replace"),
                )
            except asyncio.TimeoutError:
                logs.warn(f"Timeout comando ADB: {' '.join(cmd)}")
                return -1, "", "timeout"
            except Exception as exc:
                logs.error(f"Errore ADB: {exc}")
                return -1, "", str(exc)

    async def shell(self, serial: str, command: str, timeout: float = 30.0) -> str:
        """Esegue un comando shell su un dispositivo specifico."""
        rc, out, err = await self.adb_command(
            "shell", command, serial=serial, timeout=timeout
        )
        return out.strip()

    # ------------------------------------------------------------------
    # Discovery loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_devices()
            except Exception as exc:
                logs.error(f"Errore nel polling ADB: {exc}")
            await asyncio.sleep(self._settings.poll_interval_s)

    def _upsert_device(
        self,
        serial: str,
        state_str: str,
        *,
        model: str = "",
        product: str = "",
        usb: str = "",
        tid: str = "",
    ) -> None:
        """Aggiorna o crea lo stato di un dispositivo da un rigo adb devices."""
        status = {
            "device": DeviceStatus.ONLINE,
            "offline": DeviceStatus.OFFLINE,
            "unauthorized": DeviceStatus.UNAUTHORIZED,
        }.get(state_str, DeviceStatus.OFFLINE)

        if serial in self._devices:
            dev = self._devices[serial]
            old_status = dev.status
            # aggiorna le info senza perdere quelle gia' presenti
            dev.info.model = (model or dev.info.model).replace("_", " ")
            dev.info.product = product or dev.info.product
            dev.info.transport_id = tid or dev.info.transport_id
            dev.info.usb_port = usb or dev.info.usb_port
            dev.status = status
            dev.last_seen = time.time()
            dev.error = ""
            if old_status != status:
                logs.info(
                    f"Stato cambiato: {old_status.value} -> {status.value}",
                    serial=serial,
                )
                if status == DeviceStatus.ONLINE:
                    dev.stream_failures = 0
                    dev.next_stream_attempt = 0.0
        else:
            info = DeviceInfo(
                serial=serial,
                model=(model or "").replace("_", " "),
                product=product or "",
                transport_id=tid or "",
                usb_port=usb or "",
            )
            label = self._labels.get(serial, "")
            tags = self._tags.get(serial, [])
            dev = DeviceState(info=info, label=label, tags=tags, status=status)
            self._devices[serial] = dev
            logs.success(f"Nuovo dispositivo rilevato: {dev.display_name}", serial=serial)

        # Non forziamo mai `adb reconnect` automaticamente.
        if status == DeviceStatus.OFFLINE:
            dev.streaming = False
            dev.error = "offline"
        elif status == DeviceStatus.UNAUTHORIZED:
            dev.streaming = False
            dev.error = "unauthorized"
        else:
            dev.error = ""

    async def _refresh_devices(self) -> None:
        rc, out, err = await self.adb_command("devices", "-l")
        if rc != 0:
            logs.warn(f"adb devices -l fallito: {err}")
            return

        seen_serials: set = set()
        for match in _DEVICE_RE.finditer(out):
            serial = match.group("serial")
            if serial == "List":
                continue
            seen_serials.add(serial)
            self._upsert_device(
                serial,
                match.group("state"),
                model=match.group("model"),
                product=match.group("product"),
                usb=match.group("usb"),
                tid=match.group("tid"),
            )

        # Fallback: alcuni client ADB troncano `adb devices -l` con molti
        # dispositivi, ma `adb devices` (senza -l) e' piu' corto e arriva intero.
        rc2, out2, _ = await self.adb_command("devices")
        if rc2 == 0 and out2:
            for match in _DEVICE_RE_PLAIN.finditer(out2):
                serial = match.group("serial")
                if serial == "List" or serial in seen_serials:
                    continue
                seen_serials.add(serial)
                self._upsert_device(serial, match.group("state"))

        logs.info(f"Dispositivi ADB rilevati: {len(seen_serials)}")

        # Segna come disconnessi i dispositivi non più visibili
        for serial, dev in self._devices.items():
            if serial not in seen_serials and dev.status != DeviceStatus.DISCONNECTED:
                dev.status = DeviceStatus.DISCONNECTED
                dev.streaming = False
                logs.warn(f"Dispositivo disconnesso", serial=serial)


    # ------------------------------------------------------------------
    # Comandi utili
    # ------------------------------------------------------------------

    async def screen_on(self, serial: str) -> None:
        await self.shell(serial, "input keyevent KEYCODE_WAKEUP")
        if serial in self._devices:
            self._devices[serial].screen_on = True
        logs.info("Schermo acceso", serial=serial)

    async def screen_off(self, serial: str) -> None:
        await self.shell(serial, "input keyevent KEYCODE_SLEEP")
        if serial in self._devices:
            self._devices[serial].screen_on = False
        logs.info("Schermo spento", serial=serial)

    async def reboot(self, serial: str) -> None:
        logs.info("Riavvio in corso...", serial=serial)
        await self.adb_command("reboot", serial=serial)

    async def restart_adb_server(self) -> bool:
        """Riavvia il daemon ADB: utile per forzare un nuovo handshake RSA.

        ATTENZIONE: non aggira l'autorizzazione sul telefono, ma obbliga il
        dispositivo a richiedere la fingerprint se l'utente ha revocato le
        autorizzazioni debug USB dalle impostazioni Android.
        """
        logs.warn("Riavvio daemon ADB richiesto dall'utente")
        try:
            await self.adb_command("kill-server", timeout=10.0)
            await asyncio.sleep(0.5)
            rc, out, err = await self.adb_command("start-server", timeout=15.0)
            if rc == 0:
                logs.success("Daemon ADB riavviato")
                return True
            logs.error(f"Start ADB fallito: {err}")
            return False
        except Exception as exc:
            logs.error(f"Errore riavvio ADB: {exc}")
            return False

    async def get_battery(self, serial: str) -> int:
        out = await self.shell(serial, "dumpsys battery | grep level")
        try:
            return int(out.split(":")[-1].strip())
        except (ValueError, IndexError):
            return -1

    async def screenshot(self, serial: str) -> Optional[bytes]:
        """Cattura uno screenshot e ritorna i bytes PNG."""
        rc, out, err = await self.adb_command(
            "exec-out", "screencap", "-p", serial=serial, timeout=15.0
        )
        if rc == 0 and out:
            return out.encode("latin-1")
        return None

    async def take_screenshot_raw(self, serial: str) -> Optional[bytes]:
        """Screenshot come bytes raw via subprocess."""
        cmd = [self._adb, "-s", serial, "exec-out", "screencap", "-p"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_SUBPROCESS_KW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            if proc.returncode == 0 and stdout:
                return stdout
        except Exception:
            pass
        return None
