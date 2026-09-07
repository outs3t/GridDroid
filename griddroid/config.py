"""Configurazione globale dell'applicazione."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


CONFIG_DIR = Path.home() / ".griddroid"
CONFIG_FILE = CONFIG_DIR / "config.json"
LABELS_FILE = CONFIG_DIR / "labels.json"
TAGS_FILE = CONFIG_DIR / "tags.json"
PLAYED_FILE = CONFIG_DIR / "played.json"
SKIPPED_FILE = CONFIG_DIR / "skipped.json"
BALANCES_FILE = CONFIG_DIR / "balances.csv"
LEDGER_FILE = CONFIG_DIR / "saldi_ledger.csv"
KNOWN_FILE = CONFIG_DIR / "known.json"


def _find_bundled_adb() -> str:
    """Cerca adb.exe: preferisce il bundled nel .exe portatile, altrimenti sistema."""
    # Nel .exe PyInstaller usiamo SEMPRE l'adb bundled, altrimenti sui PC
    # clienti puo' capitare un adb di sistema vecchio/non funzionante e
    # risultare "0 dispositivi".
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)
        bundled = base / "tools" / "adb.exe"
        if bundled.exists():
            return str(bundled)

    # In sviluppo: se c'è un adb nel PATH (es. Android SDK) usiamo quello
    found = shutil.which("adb")
    if found:
        return found

    # Fallback sorgente
    base = Path(__file__).parent.parent
    bundled = base / "tools" / "adb.exe"
    if bundled.exists():
        return str(bundled)

    return "adb"


def _find_running_adb(exclude: str = "") -> str:
    """Percorso di un adb.exe di terzi gia' in esecuzione (es. Panda).

    Due adb.exe di versioni diverse si uccidono il server a vicenda sulla
    porta 5037 ("adb server version doesn't match, killing..."): a ogni
    kill i device USB si ri-enumerano e spariscono per qualche secondo —
    e' il flapping visto nei log. Usare lo stesso binario dell'altro
    programma significa parlare con lo STESSO server: niente guerra.
    """
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='adb.exe'\" "
                "| Select-Object -ExpandProperty ExecutablePath",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return ""
    excl = str(Path(exclude).resolve()).lower() if exclude else ""
    temp = str(Path(tempfile.gettempdir()).resolve()).lower()
    for line in out.splitlines():
        p = line.strip()
        if not p.lower().endswith("adb.exe") or not Path(p).exists():
            continue
        low = str(Path(p).resolve()).lower()
        # Salta il nostro bundled e i residui in temp (vecchie sessioni
        # PyInstaller): ci interessa solo l'adb di un'altra app.
        if low == excl or low.startswith(temp):
            continue
        return p
    return ""


class StreamSettings(BaseModel):
    """Parametri di streaming video."""
    max_fps: int = Field(default=30, ge=1, le=60)
    max_size: int = Field(default=1080, ge=240, le=1920)
    bit_rate: int = Field(default=8_000_000, ge=500_000, le=20_000_000)
    video_codec: str = Field(default="h264")
    max_concurrent_stream_starts: int = Field(default=4, ge=1, le=32)


class AppSettings(BaseSettings):
    """Impostazioni principali dell'applicazione."""
    host: str = "0.0.0.0"
    port: int = 8470
    adb_path: str = Field(default_factory=_find_bundled_adb)
    scrcpy_server_path: str = ""
    update_url: str = "https://outs3t.github.io/GridDroid/version.json"
    poll_interval_s: float = 5.0
    max_concurrent_installs: int = 6
    grid_columns: int = 5
    start_with_windows: bool = False
    start_minimized: bool = False
    minimize_to_tray: bool = False
    stream: StreamSettings = Field(default_factory=StreamSettings)

    class Config:
        env_prefix = "GRIDDROID_"


def load_settings() -> AppSettings:
    """Carica le impostazioni dal file di configurazione, se esiste."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        settings = AppSettings(**data)
    else:
        settings = AppSettings()

    # Ricalcola sempre adb_path se quello salvato non è un eseguibile valido
    if not shutil.which(settings.adb_path):
        settings.adb_path = _find_bundled_adb()

    # Se un altro adb.exe e' gia' in esecuzione (es. Panda), usiamo quello:
    # stesso server sulla 5037, niente riavvii che fanno flappare i device.
    running = _find_running_adb(exclude=settings.adb_path)
    if running:
        settings.adb_path = running

    # Forza ascolto su tutte le interfacce: le installazioni esistenti avevano
    # 127.0.0.1 in config.json, che blocca l'accesso da altri PC. La rete
    # locale viene comunque protetta dal firewall Windows, gestibile con il
    # pulsante "Apri porta firewall" nell'interfaccia.
    if settings.host in ("127.0.0.1", "localhost", "::1"):
        settings.host = "0.0.0.0"

    save_settings(settings)
    return settings


def save_settings(settings: AppSettings) -> None:
    """Salva le impostazioni su disco."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        settings.model_dump_json(indent=2), encoding="utf-8"
    )


def load_labels() -> Dict[str, str]:
    """Carica la mappa seriale -> etichetta personalizzata."""
    if LABELS_FILE.exists():
        return json.loads(LABELS_FILE.read_text(encoding="utf-8"))
    return {}


def save_labels(labels: Dict[str, str]) -> None:
    """Salva la mappa etichette su disco."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LABELS_FILE.write_text(
        json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_tags() -> Dict[str, List[str]]:
    """Carica la mappa seriale -> tag."""
    if TAGS_FILE.exists():
        return json.loads(TAGS_FILE.read_text(encoding="utf-8"))
    return {}


def save_tags(tags: Dict[str, List[str]]) -> None:
    """Salva la mappa tag su disco."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TAGS_FILE.write_text(
        json.dumps(tags, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_played() -> List[str]:
    """Carica la lista di seriali segnati come giocati."""
    if PLAYED_FILE.exists():
        data = json.loads(PLAYED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    return []


def save_played(played: List[str]) -> None:
    """Salva la lista dei dispositivi giocati su disco."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PLAYED_FILE.write_text(
        json.dumps(played, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_skipped() -> List[str]:
    """Carica la lista di seriali segnati come non giocati."""
    if SKIPPED_FILE.exists():
        data = json.loads(SKIPPED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    return []


def save_skipped(skipped: List[str]) -> None:
    """Salva la lista dei dispositivi non giocati su disco."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SKIPPED_FILE.write_text(
        json.dumps(skipped, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def append_balances(rows: List[dict]) -> None:
    """Accoda letture di saldo al CSV.

    Colonne: timestamp, serial, nome, bookmaker, username, saldo
    (saldo normalizzato '1234.56' — pronto per l'import nel ledger).
    """
    import csv

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not BALANCES_FILE.exists()
    with BALANCES_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(
                ["timestamp", "serial", "nome", "bookmaker", "username", "saldo"]
            )
        for r in rows:
            w.writerow(
                [
                    r["timestamp"],
                    r["serial"],
                    r["nome"],
                    r.get("bookmaker", ""),
                    r.get("username", ""),
                    r["saldo"],
                ]
            )


def write_ledger_csv(rows: List[dict]) -> Path:
    """Scrive il CSV nel formato del sito ledger: nickname,bookmaker,saldo.

    nickname = etichetta del device (maiuscola), bookmaker maiuscolo senza
    spazi, saldo come numero semplice ('118.00' -> '118', '118.50' -> '118.50').
    Sovrascrive a ogni lettura: e' lo snapshot da importare, non uno storico.
    """
    import csv

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with LEDGER_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["nickname", "bookmaker", "saldo"])
        for r in rows:
            nick = (r.get("nome") or r.get("username") or r["serial"]).upper()
            book = (r.get("bookmaker") or "").upper().replace(" ", "")
            saldo = r["saldo"] or "0"
            if saldo.endswith(".00"):
                saldo = saldo[:-3]
            w.writerow([nick, book, saldo])
    return LEDGER_FILE


def load_known() -> Dict[str, dict]:
    """Carica il registro dei dispositivi mai collegati."""
    if KNOWN_FILE.exists():
        return json.loads(KNOWN_FILE.read_text(encoding="utf-8"))
    return {}


def save_known(known: Dict[str, dict]) -> None:
    """Salva il registro dei dispositivi mai collegati."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KNOWN_FILE.write_text(
        json.dumps(known, indent=2, ensure_ascii=False), encoding="utf-8"
    )
