"""Entry point per GridDroid: avvia il server e opzionalmente la finestra nativa."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import uvicorn

from .config import load_settings

# Configurazione logging che funziona anche senza console (console=False)
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "logging.Formatter",
            "format": "%(asctime)s - %(levelname)s - %(message)s",
        },
    },
    "handlers": {
        "default": {
            "class": "logging.NullHandler",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "WARNING"},
        "uvicorn.error": {"handlers": ["default"], "level": "WARNING"},
        "uvicorn.access": {"handlers": ["default"], "level": "WARNING"},
    },
}


def _get_log_path() -> Path:
    """Restituisce il percorso del file di log persistente."""
    try:
        config_dir = Path.home() / ".griddroid"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir / "griddroid.log"
    except Exception:
        if getattr(sys, "frozen", False):
            return Path(sys._MEIPASS) / "griddroid.log"  # type: ignore
        return Path("griddroid.log")


def _log(msg: str) -> None:
    """Scrive un messaggio di log nel file."""
    try:
        log_path = _get_log_path()
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
    except Exception:
        pass


def _port_in_use(host: str, port: int) -> bool:
    """Verifica se una porta è già occupata."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _find_free_port(host: str, start_port: int, count: int = 50) -> int:
    """Trova una porta libera a partire da start_port."""
    check_host = "127.0.0.1" if host in ("localhost", "127.0.0.1", "0.0.0.0") else host
    for p in range(start_port, start_port + count):
        if not _port_in_use(check_host, p):
            return p
    raise RuntimeError(f"Nessuna porta libera tra {start_port} e {start_port + count}")


def _wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    """Attende che il server risponda su http://host:port."""
    url = f"http://{host}:{port}"
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urlopen(url, timeout=1) as _:
                return True
        except HTTPError:
            # Il server risponde, anche se con 404
            return True
        except URLError:
            pass
        time.sleep(0.25)
    return False


def _open_browser(url: str) -> None:
    """Apre il browser predefinito senza finestre di terminale."""
    if os.name == "nt":
        try:
            os.startfile(url)
            _log("Browser aperto con os.startfile")
            return
        except Exception as exc:
            _log(f"os.startfile fallito: {exc}")
    try:
        if webbrowser.open(url):
            _log("Browser aperto con webbrowser.open")
            return
    except Exception as exc:
        _log(f"webbrowser.open fallito: {exc}")
    _show_message(f"Impossibile aprire il browser automaticamente.\nApri manualmente:\n{url}")


def _show_message(text: str) -> None:
    """Mostra un messaggio a schermo se possibile."""
    if os.name == "nt":
        try:
            # MB_OK | MB_ICONINFORMATION | MB_TOPMOST (0x40040)
            ctypes.windll.user32.MessageBoxW(0, text, "GridDroid", 0x40040)
            return
        except Exception:
            pass
    _log(text)


_server_state: dict = {}


class _NoSignalServer(uvicorn.Server):
    """Server uvicorn che non installa signal handler (evita errori in thread secondari)."""

    def install_signal_handlers(self) -> None:
        pass


def _server_thread(bind_host: str, port: int) -> None:
    """Esegue uvicorn.Server in un loop asyncio separato."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _server_state["loop"] = loop

        config = uvicorn.Config(
            "griddroid.app:create_app",
            host=bind_host,
            port=port,
            factory=True,
            log_level="warning",
            log_config=LOGGING_CONFIG,
        )
        server = _NoSignalServer(config)
        _server_state["server"] = server

        loop.run_until_complete(server.serve())
    except Exception as exc:
        _log(f"Server crash: {exc}")
        _log(traceback.format_exc())
    finally:
        _server_state["server"] = None
        _server_state["loop"] = None


def _stop_server(timeout: float = 15.0) -> None:
    """Richiede la chiusura di uvicorn.Server e attende il termine del thread."""
    server = _server_state.get("server")
    loop = _server_state.get("loop")
    if server and loop:
        try:
            loop.call_soon_threadsafe(setattr, server, "should_exit", True)
        except Exception as exc:
            _log(f"Errore richiesta stop server: {exc}")
    thread = _server_state.get("thread")
    if thread and thread.is_alive():
        try:
            thread.join(timeout=timeout)
        except Exception as exc:
            _log(f"Errore join server thread: {exc}")


def _cleanup_children() -> None:
    """Termina eventuali processi figli rimasti aperti (adb shell, scrcpy, ecc.)."""
    try:
        import psutil
        try:
            parent = psutil.Process()
            for child in parent.children(recursive=True):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            gone, alive = psutil.wait_procs(parent.children(recursive=True), timeout=2)
            for child in alive:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
        except Exception as exc:
            _log(f"Errore psutil cleanup: {exc}")
    except ImportError:
        pass

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/FI", f"PARENT eq {os.getpid()}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            _log(f"Errore taskkill: {exc}")


def _wait_for_user_interrupt() -> None:
    """Mantiene il processo vivo in modalita console fino a Ctrl+C."""
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def _open_browser_when_ready(host: str, port: int) -> None:
    if _wait_for_server(host, port):
        _open_browser(f"http://{host}:{port}")
    else:
        _log("Timeout: impossibile aprire browser, server non raggiungibile")


def _run_with_webview(url: str) -> None:
    """Apre una finestra webview nativa; se non disponibile, apre il browser."""
    try:
        import webview
        _log("Apertura finestra nativa")

        def _on_closing() -> None:
            """Forza la chiusura del processo quando l'utente chiude la finestra."""
            _log("Chiusura finestra richiesta")
            try:
                _stop_server(3.0)
            except Exception:
                pass
            try:
                _cleanup_children()
            except Exception:
                pass
            _log("GridDroid chiuso")
            os._exit(0)

        window = webview.create_window(
            "GridDroid",
            url,
            width=1600,
            height=1000,
            min_size=(1200, 800),
            background_color="#0a0a0a",
        )
        window.events.closing += _on_closing
        webview.start()
    except Exception as exc:
        _log(f"Webview non disponibile: {exc}")
        _log(traceback.format_exc())
        _show_message(f"Finestra nativa non disponibile.\nApri il browser e incolla:\n{url}")


def main() -> None:
    _log("GridDroid avviato")
    if os.name == "nt":
        try:
            mutex = ctypes.windll.kernel32.CreateMutexW(None, 1, "Global\\GridDroid_Mutex")
            if ctypes.windll.kernel32.GetLastError() == 183:
                _log("GridDroid e gia in esecuzione")
                sys.exit(0)
        except Exception as exc:
            _log(f"Mutex non creato: {exc}")
    parser = argparse.ArgumentParser(description="GridDroid - Android Farm Manager")
    parser.add_argument(
        "--browser", action="store_true",
        help="Apre automaticamente il browser",
    )
    parser.add_argument(
        "--message", action="store_true",
        help="Mostra solo l'URL in una finestra di messaggio",
    )
    parser.add_argument(
        "--host", type=str, default=None,
        help="Host su cui ascoltare",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Porta su cui ascoltare",
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
        _log("Configurazione caricata")
    except Exception as exc:
        _log(f"Errore caricamento configurazione: {exc}")
        _log(traceback.format_exc())
        _show_message(f"Errore caricamento configurazione:\n{exc}")
        sys.exit(1)

    bind_host = args.host or settings.host
    base_port = args.port or settings.port

    # Per la finestra/browser locale usiamo sempre un indirizzo raggiungibile
    if bind_host in ("localhost", "127.0.0.1", "0.0.0.0"):
        ui_host = "127.0.0.1"
    else:
        ui_host = bind_host

    # Trova una porta libera; se quella richiesta è occupata, prova le successive
    try:
        port = _find_free_port(bind_host, base_port)
    except RuntimeError as exc:
        _log(str(exc))
        _show_message(str(exc))
        sys.exit(1)

    url = f"http://{ui_host}:{port}"
    _log(f"URL finale: {url}")

    server_thread = threading.Thread(
        target=_server_thread,
        args=(bind_host, port),
        daemon=True,
    )
    _server_state["thread"] = server_thread
    server_thread.start()

    # Attende che il server risponda prima di aprire la finestra
    if not _wait_for_server(ui_host, port):
        _log("ERRORE: il server non si e avviato.")
        _show_message(f"Il server non si e avviato.\nProva ad aprire manualmente:\n{url}")
        _stop_server(5.0)
        sys.exit(1)

    try:
        if args.browser:
            _open_browser(url)
            _wait_for_user_interrupt()
        elif args.message:
            _show_message(f"GridDroid e in esecuzione su:\n{url}\n\nApri il browser e incolla l'indirizzo.")
        else:
            _run_with_webview(url)
    except KeyboardInterrupt:
        pass
    finally:
        _stop_server()
        _cleanup_children()
        _log("GridDroid chiuso")
        os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log(f"Errore fatale: {exc}")
        _log(traceback.format_exc())
        _show_message(f"GridDroid ha riscontrato un errore:\n{exc}")
        os._exit(1)
