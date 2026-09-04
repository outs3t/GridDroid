"""Funzioni per avvio con Windows e icona di notifica."""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from typing import Callable, Optional

from .log_manager import logs

try:
    from PIL import Image
except Exception:
    Image = None

try:
    import pystray
    from pystray import Menu, MenuItem
except Exception:
    pystray = None


def _logo_path() -> Optional[Path]:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
    else:
        base = Path(__file__).parent
    for rel in ("griddroid/static/logo.png", "static/logo.png", "logo.png"):
        p = base / rel.replace("/", os.sep)
        if p.exists():
            return p
    return None


def _default_icon_image() -> Optional[Image.Image]:
    if Image is None:
        return None
    logo = _logo_path()
    if logo:
        try:
            img = Image.open(logo).convert("RGBA")
            # pystray solitamente lavora con icone piccole, 64x64 va bene
            resample = getattr(Image, "Resampling", Image).LANCZOS
            return img.resize((64, 64), resample)
        except Exception as exc:
            logs.warn(f"Logo tray non caricato: {exc}", throttle_s=60)
    # Fallback: quadrato colorato
    return Image.new("RGBA", (64, 64), (34, 34, 34, 255))


def set_run_at_boot(enabled: bool) -> bool:
    """Aggiunge/rimuove la voce nel registro HKCU Run."""
    if os.name != "nt":
        return False
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0,
            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
        ) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    exe = f'"{sys.executable}"'
                else:
                    exe = f'"{sys.executable}" -m griddroid'
                winreg.SetValueEx(key, "GridDroid", 0, winreg.REG_SZ, exe)
                logs.info("Avvio con Windows abilitato")
            else:
                try:
                    winreg.DeleteValue(key, "GridDroid")
                    logs.info("Avvio con Windows disabilitato")
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        logs.error(f"Errore registro avvio Windows: {exc}")
        return False


def create_tray_icon(
    url: str,
    show_callback: Optional[Callable[[], None]] = None,
    exit_callback: Optional[Callable[[], None]] = None,
) -> Optional[pystray.Icon]:
    """Crea l'icona di notifica, se pystray è disponibile."""
    if pystray is None or Image is None:
        return None
    try:
        image = _default_icon_image()
        if image is None:
            return None

        def on_show(icon, item):
            if show_callback:
                show_callback()
            else:
                webbrowser.open(url)

        def on_exit(icon, item):
            if exit_callback:
                exit_callback()
            icon.stop()
            os._exit(0)

        menu = Menu(
            MenuItem("Mostra", on_show, default=True),
            MenuItem("Esci", on_exit),
        )
        icon = pystray.Icon("GridDroid", image, "GridDroid", menu)
        return icon
    except Exception as exc:
        logs.error(f"Errore creazione icona tray: {exc}", throttle_s=30)
        return None
