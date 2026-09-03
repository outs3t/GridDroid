"""Operazioni di massa: installazione APK, shell commands, push file, riavvio."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .adb_manager import AdbManager
from .device import DeviceStatus
from .log_manager import logs


class BulkProgress:
    """Traccia l'avanzamento di un'operazione di massa."""

    def __init__(self, serials: List[str]) -> None:
        self.total = len(serials)
        self.completed = 0
        self.results: Dict[str, str] = {}  # serial -> "ok" | messaggio errore
        self._lock = asyncio.Lock()

    async def report(self, serial: str, result: str) -> None:
        async with self._lock:
            self.completed += 1
            self.results[serial] = result

    @property
    def progress_pct(self) -> float:
        return (self.completed / self.total * 100) if self.total else 0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "completed": self.completed,
            "progress": round(self.progress_pct, 1),
            "results": self.results,
        }


class BulkActionRunner:
    """Esegue operazioni batch in parallelo con concorrenza limitata."""

    def __init__(self, adb: AdbManager, max_concurrent: int = 6) -> None:
        self._adb = adb
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def _selected_serials(self) -> List[str]:
        return [
            s for s, d in self._adb.devices.items()
            if d.status == DeviceStatus.ONLINE and d.selected
        ]

    # ------------------------------------------------------------------
    # Installazione APK
    # ------------------------------------------------------------------

    async def install_apk(
        self,
        apk_path: str,
        serials: Optional[List[str]] = None,
        on_progress: Optional[Callable] = None,
    ) -> BulkProgress:
        targets = serials or self._selected_serials()
        progress = BulkProgress(targets)
        logs.info(f"Installazione APK su {len(targets)} dispositivi: {apk_path}")

        if not os.path.isfile(apk_path):
            logs.error(f"File APK non trovato: {apk_path}")
            return progress

        async def _install(serial: str) -> None:
            async with self._semaphore:
                logs.info(f"Installazione in corso...", serial=serial)
                rc, out, err = await self._adb.adb_command(
                    "install", "-r", "-d", apk_path,
                    serial=serial, timeout=120.0,
                )
                if rc == 0 and "Success" in out:
                    await progress.report(serial, "ok")
                    logs.success("APK installato", serial=serial)
                else:
                    msg = err.strip() or out.strip() or "errore sconosciuto"
                    await progress.report(serial, msg)
                    logs.error(f"Installazione fallita: {msg}", serial=serial)
                if on_progress:
                    on_progress(progress)

        tasks = [_install(s) for s in targets]
        await asyncio.gather(*tasks, return_exceptions=True)
        return progress

    # ------------------------------------------------------------------
    # Shell command di massa
    # ------------------------------------------------------------------

    async def run_shell(
        self,
        command: str,
        serials: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        targets = serials or self._selected_serials()
        logs.info(f"Esecuzione comando su {len(targets)} dispositivi: {command}")
        results: Dict[str, str] = {}

        async def _run(serial: str) -> None:
            async with self._semaphore:
                out = await self._adb.shell(serial, command, timeout=60.0)
                results[serial] = out
                logs.info(f"Output: {out[:200]}", serial=serial)

        tasks = [_run(s) for s in targets]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results

    # ------------------------------------------------------------------
    # Push file
    # ------------------------------------------------------------------

    async def push_file(
        self,
        local_path: str,
        remote_path: str = "/sdcard/",
        serials: Optional[List[str]] = None,
    ) -> BulkProgress:
        targets = serials or self._selected_serials()
        progress = BulkProgress(targets)
        logs.info(f"Push file su {len(targets)} dispositivi: {local_path} → {remote_path}")

        async def _push(serial: str) -> None:
            async with self._semaphore:
                rc, out, err = await self._adb.adb_command(
                    "push", local_path, remote_path,
                    serial=serial, timeout=120.0,
                )
                if rc == 0:
                    await progress.report(serial, "ok")
                    logs.success("File trasferito", serial=serial)
                else:
                    msg = err.strip() or "errore"
                    await progress.report(serial, msg)
                    logs.error(f"Push fallito: {msg}", serial=serial)

        tasks = [_push(s) for s in targets]
        await asyncio.gather(*tasks, return_exceptions=True)
        return progress

    # ------------------------------------------------------------------
    # Riavvio / Wake / Spegnimento
    # ------------------------------------------------------------------

    async def reboot_all(self, serials: Optional[List[str]] = None) -> None:
        targets = serials or self._selected_serials()
        logs.info(f"Riavvio di {len(targets)} dispositivi")
        tasks = [self._adb.reboot(s) for s in targets]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wake_all(self, serials: Optional[List[str]] = None) -> None:
        targets = serials or self._selected_serials()
        tasks = [self._adb.screen_on(s) for s in targets]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def sleep_all(self, serials: Optional[List[str]] = None) -> None:
        targets = serials or self._selected_serials()
        tasks = [self._adb.screen_off(s) for s in targets]
        await asyncio.gather(*tasks, return_exceptions=True)
