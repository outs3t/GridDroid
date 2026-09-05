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
    """Scarica il file `version.json` remoto con cache-buster robusto."""

    def _fetch():
        try:
            import time
            cache_bust = f"{url}?_={int(time.time() * 1000)}"
            req = urllib.request.Request(
                cache_bust,
                headers={
                    "User-Agent": "GridDroid-Updater",
                    "Cache-Control": "no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read().decode("utf-8")
                return json.loads(data)
        except Exception as exc:
            # Logga l'errore per debug
            import sys
            print(f"[updater] fetch error: {exc}", file=sys.stderr)
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
    installer: Path,
    silent_args: List[str],
    exe_path: Optional[str] = None,
    old_pid: Optional[int] = None,
) -> Path:
    """Crea uno script .bat che chiude GridDroid, esegue l'installer e lo riavvia."""
    bat = Path(tempfile.gettempdir()) / "griddroid_update.bat"
    args = " ".join(silent_args)
    restart = f'start "" "{exe_path}"\n' if exe_path and exe_path != str(installer) else ""
    kill_pid = f"taskkill /F /PID {old_pid} 2>nul\n" if old_pid else ""
    # Senza silent_args il file scaricato e' l'exe portatile: va copiato
    # sopra il vecchio exe, non eseguito (altrimenti gira da temp e basta).
    if exe_path and not silent_args and exe_path != str(installer):
        install = f'copy /Y "{installer}" "{exe_path}" >nul\n'
    else:
        install = f'start /wait "" "{installer}" {args}\n'
    text = (
        "@echo off\n"
        "title GridDroid Updater\n"
        f"{kill_pid}"
        "taskkill /F /IM GridDroid.exe 2>nul\n"
        ":wait\n"
        "tasklist /FI \"IMAGENAME eq GridDroid.exe\" 2>nul | find /I \"GridDroid.exe\" >nul\n"
        "if %errorlevel%==0 (\n"
        "    ping -n 2 127.0.0.1 >nul\n"
        "    goto wait\n"
        ")\n"
        f"{install}"
        f"{restart}"
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
    old_pid: Optional[int] = None,
) -> bool:
    """Avvia il processo updater esterno e lo stacca dal padre."""
    system = platform.system()
    if system == "Windows":
        script = _make_windows_bat(installer, silent_args, restart_path, old_pid)
        # CREATE_NO_WINDOW evita la comparsa del terminale nero;
        # DETACHED_PROCESS stacca il processo dal padre.
        flags = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        subprocess.Popen(
            ["cmd", "/c", "call", str(script)],
            creationflags=flags,
            startupinfo=si,
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
