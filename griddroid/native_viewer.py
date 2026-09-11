"""Gestione finestre native scrcpy per device.

Questo modulo lancia `scrcpy.exe` come player nativo FFmpeg per un singolo
device, garantendo che non ci siano conflitti ADB con lo stream interno di
GridDroid. L'utilizzo e' mutualmente esclusivo: se scrcpy nativo e' attivo,
lo stream web viene fermato e viceversa.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict

from .log_manager import logs


class NativeViewerManager:
    """Lancia e traccia i processi scrcpy.exe per ogni device."""

    def __init__(self) -> None:
        self._procs: Dict[str, subprocess.Popen] = {}

    def _find_scrcpy(self, adb_path: str) -> str:
        """Trova scrcpy.exe nella stessa cartella di adb.exe."""
        scrcpy = Path(adb_path).with_name("scrcpy.exe")
        if scrcpy.exists():
            return str(scrcpy)
        found = shutil.which("scrcpy")
        if not found:
            raise FileNotFoundError("scrcpy.exe non trovato")
        return found

    def _cleanup_dead(self, serial: str) -> bool:
        """Rimuove un processo gia' morto e restituisce se era presente."""
        proc = self._procs.get(serial)
        if proc is None:
            return False
        if proc.poll() is not None:
            self._procs.pop(serial, None)
            return False
        return True

    async def start(
        self,
        serial: str,
        adb_path: str,
        adb_port: int,
        max_size: int = 480,
        max_fps: int = 2,
        bit_rate: int = 50_000,
        video_encoder: str = "OMX.google.h264.encoder",
    ) -> None:
        """Apre una finestra scrcpy nativa per il device.

        Se esiste gia' un processo per questo seriale lo chiude e ne apre
        uno nuovo, evitando doppi server scrcpy sul device.
        """
        if self._cleanup_dead(serial):
            await self.stop(serial)

        scrcpy = self._find_scrcpy(adb_path)

        env = os.environ.copy()
        # Forza scrcpy a usare lo stesso adb e la stessa porta server di GridDroid
        env["ADB"] = adb_path
        env["ANDROID_ADB_SERVER_PORT"] = str(adb_port)

        args = [
            scrcpy,
            "--serial", serial,
            "--max-size", str(max_size),
            "--max-fps", str(max_fps),
            "--video-bit-rate", str(bit_rate),
            "--window-title", f"GridDroid - {serial}",
            "--show-touches",
            "--stay-awake",
            "--no-audio",
        ]
        if video_encoder:
            args += ["--video-encoder", video_encoder]

        try:
            proc = subprocess.Popen(args, env=env)
        except Exception as exc:
            logs.error(f"Errore lancio scrcpy nativo: {exc}", serial=serial)
            raise

        self._procs[serial] = proc
        logs.success("Finestra scrcpy nativa aperta", serial=serial)

    async def stop(self, serial: str) -> None:
        """Chiude la finestra scrcpy per il device."""
        proc = self._procs.pop(serial, None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1)
        except Exception as exc:
            logs.warn(f"Errore chiusura scrcpy: {exc}", serial=serial)

    def is_running(self, serial: str) -> bool:
        """True se la finestra nativa per il seriale e' viva."""
        return self._cleanup_dead(serial)

    async def stop_all(self) -> None:
        """Chiude tutte le finestre native."""
        for serial in list(self._procs.keys()):
            await self.stop(serial)
