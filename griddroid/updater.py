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
import ssl
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .log_manager import logs


def _ssl_context() -> ssl.SSLContext:
    """Contesto SSL con i certificati di certifi.

    Nell'exe PyInstaller i certificati CA del sistema non vengono trovati:
    senza certifi ogni chiamata HTTPS fallisce con CERTIFICATE_VERIFY_FAILED.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


DEFAULT_REMOTE = "https://outs3t.github.io/GridDroid/version.json"


def _version_tuple(v: str):
    """Converte una stringa di versione in una tupla di interi."""
    try:
        return tuple(int(x) for x in v.split(".") if x.isdigit())
    except ValueError:
        return (0,)


def is_newer(remote: str, local: str) -> bool:
    """Restituisce True se `remote` è una versione maggiore di `local`.

    La CI pubblica versioni con un quarto segmento (0.1.124.157): non e'
    una nuova release, solo un build dello stesso __version__. Senza il
    confronto troncato a major.minor.patch l'exe installato vedeva un
    "aggiornamento disponibile" a ogni avvio.
    """
    return _version_tuple(remote)[:3] > _version_tuple(local)[:3]


def is_installed() -> bool:
    """True se GridDroid gira da un'installazione Inno Setup.

    Inno crea sempre `unins000.exe` nella cartella dell'app: e' il marker
    piu' affidabile per distinguere installazione da exe portatile.
    """
    if not getattr(sys, "frozen", False):
        return False
    try:
        return (Path(sys.executable).parent / "unins000.exe").exists()
    except Exception:
        return False


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
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
                # utf-8-sig: tollera un eventuale BOM in testa al JSON
                data = r.read().decode("utf-8-sig")
                return json.loads(data)
        except Exception as exc:
            logs.warn(f"Updater: fetch remoto fallito ({exc})")
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
        import time as _time

        max_attempts = 5
        state["status"] = "downloading"
        state["percent"] = 0
        state["error"] = None
        downloaded = dest.stat().st_size if dest.exists() else 0
        total: Optional[int] = None

        for attempt in range(1, max_attempts + 1):
            try:
                headers = {"User-Agent": f"GridDroid-Updater/{state.get('version', '0.0.0')}"}
                # Resume: riprende da dove si e' interrotto
                if downloaded:
                    headers["Range"] = f"bytes={downloaded}-"
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60.0, context=_ssl_context()) as r:
                    # Se il server ignora Range (200 invece di 206), ricomincia da zero
                    if downloaded and r.status == 200:
                        downloaded = 0
                    if total is None:
                        cl = r.headers.get("Content-Length")
                        total = (int(cl) + downloaded) if cl else None
                    mode = "ab" if downloaded else "wb"
                    with dest.open(mode) as f:
                        while True:
                            data = r.read(chunk)
                            if not data:
                                break
                            f.write(data)
                            downloaded += len(data)
                            if total:
                                state["percent"] = min(100, int(downloaded * 100 / total))
                            else:
                                state["percent"] = min(state["percent"] + 2, 99)
                # Download troncato senza eccezione: riprova invece di
                # installare un exe corrotto.
                if total is not None and downloaded < total:
                    raise IOError(f"download incompleto: {downloaded}/{total} byte")
                state["status"] = "ready"
                state["percent"] = 100
                return True
            except Exception as exc:
                state["error"] = str(exc)
                if attempt < max_attempts:
                    _time.sleep(min(2 ** attempt, 10))
                    continue
                state["status"] = "error"
                return False
        return False

    return await asyncio.to_thread(_download)


def _make_windows_bat(
    installer: Path,
    silent_args: List[str],
    exe_path: Optional[str] = None,
    old_pid: Optional[int] = None,
    port: int = 0,
) -> Path:
    """Crea uno script .bat che chiude GridDroid, esegue l'installer e lo riavvia."""
    bat = Path(tempfile.gettempdir()) / "griddroid_update.bat"
    args = " ".join(silent_args)
    kill_pid = f"taskkill /F /T /PID {old_pid} 2>nul\n" if old_pid else ""
    # Riavvio con verifica: l'exe onefile PyInstaller scompatta pythonXY.dll
    # in %TEMP%\_MEIxxxx; se la cartella sparisce (cleanup di un'altra app
    # onefile, antivirus, estrazione interrotta) il processo muore o resta
    # appeso al dialog 'Failed to load Python DLL'. Senza controllo l'app
    # restava semplicemente chiusa: qui si riprova finche' il server non
    # risponde sulla porta (o la presenza del processo, se la porta manca).
    restart = ""
    if exe_path and exe_path != str(installer):
        # 'rem' nei commenti del bat: sono righe ignorate dal cmd.
        if port:
            # Server su = exe sano. Processo vivo ma porta muta per ~12s =
            # dialog d'errore appeso (es. pythonXY.dll mancante): si riprova.
            ready = (
                "powershell -NoProfile -Command \"try{$c=New-Object "
                f"Net.Sockets.TcpClient;$c.Connect('127.0.0.1',{port});$c.Close();"
                'exit 0}catch{exit 1}" >nul 2>&1\n'
                "if errorlevel 1 goto probe_wait\n"
                "goto app_ok\n"
            )
        else:
            # Porta ignota (es. source): ci si accontenta del processo vivo.
            ready = "goto app_ok\n"
        restart = (
            "set TRIES=0\n"
            ":relaunch\n"
            f'start "" "{exe_path}"\n'
            "set /a TRIES+=1\n"
            "set PROBES=0\n"
            ":probe\n"
            "ping -n 3 127.0.0.1 >nul\n"
            'tasklist /FI "IMAGENAME eq GridDroid.exe" 2>nul | find /I "GridDroid.exe" >nul\n'
            "if errorlevel 1 goto probe_dead\n"
            f"{ready}"
            ":probe_wait\n"
            "set /a PROBES+=1\n"
            "if %PROBES% LSS 6 goto probe\n"
            ":probe_dead\n"
            "if %TRIES% GEQ 3 goto last_launch\n"
            "taskkill /F /IM GridDroid.exe >nul 2>&1\n"
            "ping -n 2 127.0.0.1 >nul\n"
            "goto relaunch\n"
            ":last_launch\n"
            f'start "" "{exe_path}"\n'
            ":app_ok\n"
        )
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
        "taskkill /F /T /IM GridDroid.exe 2>nul\n"
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
    port: int = 0,
) -> bool:
    """Avvia il processo updater esterno e lo stacca dal padre."""
    system = platform.system()
    if system == "Windows":
        script = _make_windows_bat(
            installer, silent_args, restart_path, old_pid, port
        )
        # Wrapper VBScript: WScript.Shell.Run con window style 0 esegue il bat
        # completamente invisibile (niente finestra console durante l'update).
        vbs = Path(tempfile.gettempdir()) / "griddroid_update.vbs"
        vbs.write_text(
            'CreateObject("Wscript.Shell").Run '
            f'"cmd /c call ""{script}""", 0, False\n',
            encoding="utf-8",
        )
        subprocess.Popen(
            ["wscript.exe", str(vbs)],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
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
