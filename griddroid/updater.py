"""Aggiornamento automatico di GridDroid.

Scarica un installer remoto, mostra il progresso e delega un processo
esterno per installare il nuovo eseguibile mentre GridDroid si chiude.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_REMOTE = "https://outs3t.github.io/GridDroid/version.json"


def _version_tuple(v: str):
    """Converte una stringa di versione in una tupla di interi."""
    try:
        return tuple(int(x) for x in v.split(".") if x.isdigit())
    except ValueError:
        return (0,)


def is_newer(remote: str, local: str) -> bool:
    """Restituisce True se `remote` è una versione maggiore di `local`."""
    return _version_tuple(remote) > _version_tuple(local)


async def fetch_remote_info(
    url: str = DEFAULT_REMOTE, timeout: float = 10.0,
) -> Optional[Dict[str, Any]]:
    """Scarica il file `version.json` remoto con cache-buster."""

    def _fetch():
        try:
            cache_bust = f"{url}?_={int(__import__('time').time() * 1000)}"
            req = urllib.request.Request(
                cache_bust,
                headers={
                    "User-Agent": "GridDroid-Updater",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    return await asyncio.to_thread(_fetch)


async def download_file(
    url: str,
    dest: Path,
    state: Dict[str, Any],
    chunk: int = 65536,
) -> bool:
    """Scarica `url` in `dest` aggiornando `state['percent']`."""

    def _download():
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": f"GridDroid-Updater/{state.get('version', '0.0.0')}"},
            )
            with urllib.request.urlopen(req, timeout=60.0) as r:
                total = r.headers.get("Content-Length")
                total = int(total) if total else None
                downloaded = 0
                state["status"] = "downloading"
                state["percent"] = 0
                state["error"] = None
                with dest.open("wb") as f:
                    while True:
                        data = r.read(chunk)
                        if not data:
                            break
                        f.write(data)
                        downloaded += len(data)
                        if total:
                            state["percent"] = min(100, int(downloaded * 100 / total))
                        else:
                            # dimensione sconosciuta: avanza gradualmente
                            state["percent"] = min(state["percent"] + 2, 99)
            state["status"] = "ready"
            state["percent"] = 100
            return True
        except Exception as exc:
            state["status"] = "error"
            state["error"] = str(exc)
            return False

    return await asyncio.to_thread(_download)


def _make_windows_bat(
    installer: Path, silent_args: List[str], exe_path: Optional[str] = None,
) -> Path:
    """Crea uno script .bat che esegue l'installer e riavvia GridDroid."""
    bat = Path(tempfile.gettempdir()) / "griddroid_update.bat"
    args = " ".join(silent_args)
    restart = f'start "" "{exe_path}"' if exe_path and exe_path != str(installer) else ""
    text = (
        "@echo off\n"
        "title GridDroid Updater\n"
        f"start /wait \"\" \"{installer}\" {args}\n"
        f"{restart}\n"
        f"del /F /Q \"{installer}\" 2>nul\n"
        "del /F /Q \"%~f0\" 2>nul\n"
    )
    bat.write_text(text, encoding="utf-8")
    return bat


def _make_linux_sh(installer: Path, exe_path: Optional[str] = None) -> Path:
    """Crea uno script .sh che esegue l'installer su Linux."""
    sh = Path(tempfile.gettempdir()) / "griddroid_update.sh"
    restart = f'"{exe_path}"' if exe_path and exe_path != str(installer) else ""
    text = (
        "#!/bin/bash\n"
        f'chmod +x "{installer}"\n'
        f'bash "{installer}"\n'
        f"{restart}\n"
        f'rm -f "{installer}"\n'
        'rm -f "$0"\n'
    )
    sh.write_text(text, encoding="utf-8")
    return sh


def schedule_install(
    installer: Path,
    silent_args: List[str] = (),
    restart_path: Optional[str] = None,
) -> bool:
    """Avvia il processo updater esterno e lo stacca dal padre."""
    system = platform.system()
    if system == "Windows":
        script = _make_windows_bat(installer, silent_args, restart_path)
        flags = 0x00000008  # DETACHED_PROCESS
        subprocess.Popen(
            ["cmd", "/c", "call", str(script)],
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        script = _make_linux_sh(installer, restart_path)
        script.chmod(0o755)
        subprocess.Popen(
            ["nohup", str(script)],
            start_new_session=True,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return True
