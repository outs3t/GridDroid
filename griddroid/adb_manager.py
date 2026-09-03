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

from .config import (
    AppSettings,
    load_labels,
    save_labels,
    load_tags,
    save_tags,
    load_played,
    save_played,
)
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
        self._played_serials: set = set(load_played())
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

    def set_played(self, serial: str, played: bool = True) -> None:
        """Segna un dispositivo come giocato e lo salva su disco."""
        if played:
            self._played_serials.add(serial)
        else:
            self._played_serials.discard(serial)
        if serial in self._devices:
            self._devices[serial].played = played
        save_played(sorted(self._played_serials))
        logs.info("Dispositivo segnato come giocato" if played else "Dispositivo rimosso dai giocati", serial=serial)

    def reset_played(self) -> None:
        """Ripristina tutti i dispositivi giocati."""
        self._played_serials.clear()
        for dev in self._devices.values():
            dev.played = False
        save_played([])
        logs.info("Ripristinati tutti i dispositivi giocati")

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
        # Avvia il daemon ADB (idempotente: se gia' attivo non fa nulla)
        try:
            await self.adb_command("start-server", timeout=15.0)
            await asyncio.sleep(0.5)
        except Exception as exc:
            logs.warn(f"Impossibile avviare ADB server: {exc}")
        # Forza reconnect dei dispositivi offline senza killare il server
        try:
            await self.adb_command("reconnect", "offline", timeout=10.0)
        except Exception:
            pass
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
                out_str = stdout.decode("utf-8", errors="replace")
                err_str = stderr.decode("utf-8", errors="replace")
                # Se un dispositivo specifico sparisce da ADB, sincronizziamo subito lo stato
                if serial and serial in self._devices:
                    combined = (out_str + err_str).lower()
                    if "not found" in combined or "device offline" in combined or "no devices" in combined:
                        dev = self._devices[serial]
                        if dev.status != DeviceStatus.DISCONNECTED:
                            dev.status = DeviceStatus.DISCONNECTED
                            dev.streaming = False
                            logs.warn("Dispositivo segnato offline da ADB", serial=serial)
                return (
                    proc.returncode or 0,
                    out_str,
                    err_str,
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
            dev.played = serial in self._played_serials
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
            dev = DeviceState(
                info=info,
                label=label,
                tags=tags,
                status=status,
                played=serial in self._played_serials,
            )
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
        # Rilevazione robusta per grandi farm: `adb devices -l` puo' troncare con
        # molti dispositivi, quindi usiamo `adb devices` per lo stato e fondiamo
        # piu' tentativi; poi arricchiamo con `adb devices -l`.
        seen_serials: set = set()
        has_offline = False
        for attempt in range(5):
            rc, out, _ = await self.adb_command("devices")
            if rc == 0 and out:
                for match in _DEVICE_RE_PLAIN.finditer(out):
                    serial = match.group("serial")
                    if serial == "List":
                        continue
                    if serial not in seen_serials:
                        seen_serials.add(serial)
                        self._upsert_device(serial, match.group("state"))
                    if match.group("state") == "offline":
                        has_offline = True
            # Se ci sono dispositivi offline, prova a forzare il reconnect
            if has_offline and attempt == 1:
                try:
                    await self.adb_command("reconnect", "offline", timeout=10.0)
                except Exception:
                    pass
            if attempt < 4:
                await asyncio.sleep(0.5)

        # Arricchisce con i dettagli di `adb devices -l` per i seriali gia' trovati
        rc_l, out_l, _ = await self.adb_command("devices", "-l")
        if rc_l == 0 and out_l:
            for match in _DEVICE_RE.finditer(out_l):
                serial = match.group("serial")
                if serial == "List" or serial not in seen_serials:
                    continue
                self._upsert_device(
                    serial,
                    match.group("state"),
                    model=match.group("model"),
                    product=match.group("product"),
                    usb=match.group("usb"),
                    tid=match.group("tid"),
                )

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
        """Riavvia il daemon ADB: kill + start per forzare re-enumerazione USB."""
        logs.warn("Riavvio daemon ADB richiesto dall'utente")
        try:
            await self.adb_command("kill-server", timeout=10.0)
            await asyncio.sleep(1.0)
            rc, out, err = await self.adb_command("start-server", timeout=15.0)
            if rc == 0:
                logs.success("Daemon ADB riavviato")
                await asyncio.sleep(1.0)
                await self._refresh_devices()
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
