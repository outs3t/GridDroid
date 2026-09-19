"""Gestione asincrona del daemon ADB: discovery, polling e stato dispositivi."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import re
import shutil
import socket
import time
import subprocess
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Nessuna finestra di terminale per i processi figli su Windows
if os.name == "nt":
    _SUBPROCESS_KW = {"creationflags": 0x08000000}
else:
    _SUBPROCESS_KW = {}

from .config import (
    AppSettings,
    CONFIG_DIR,
    _adb_executable_works,
    _find_running_adb,
    load_labels,
    save_labels,
    load_tags,
    save_tags,
    load_played,
    save_played,
    load_skipped,
    save_skipped,
    load_known,
    save_known,
    load_label_colors,
    save_label_colors,
    load_device_order,
    save_device_order,
    load_balances_state,
    save_balances_state,
)
from .device import (
    DeviceInfo,
    DeviceState,
    DeviceStatus,
    SCREEN_OFF_REQUESTED,
)
from .log_manager import logs


# Lock per categoria di comando ADB. Input (click) ha un lock separato dai
# comandi shell lenti (lettura saldi, install) per non bloccare la UI.
# 'state' per saldi, 'shell' per generico, 'input' per input.
_ADB_CMD_LOCKS: Dict[str, asyncio.Lock] = {}

# Oltre questo numero di poll consecutivi senza vedere un device smettiamo
# di tentare 'adb reconnect': se non e' tornato entro ~5 minuti e' staccato
# fisicamente, e insistere disturba i transport di tutti gli altri.
_RECONNECT_MAX_MISSES = 9


def get_adb_cmd_lock(priority: str = "shell") -> asyncio.Lock:
    """Ritorna il lock ADB per la categoria data."""
    if priority not in _ADB_CMD_LOCKS:
        _ADB_CMD_LOCKS[priority] = asyncio.Lock()
    return _ADB_CMD_LOCKS[priority]


async def run_proc(
    cmd: List[str], timeout: float = 30.0,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[int, bytes, bytes]:
    """Esegue un processo breve e ritorna (rc, stdout, stderr).

    Su Windows asyncio.create_subprocess_exec lancia subprocess.Popen()
    SUL thread dell'event loop (~20-50ms a spawn sotto carico): ogni
    comando adb fermava il loop intero — distribuzione video compresa.
    Qui Popen + communicate girano in un thread worker: il loop non si
    blocca mai. I processi long-lived (scrcpy-server, ffmpeg) restano su
    create_subprocess_exec perche' servono pipe asyncio vere.

    Timeout: uccide il processo e rilancia asyncio.TimeoutError — i
    chiamanti conservano i loro handler esistenti.
    """
    def _run() -> Tuple[int, bytes, bytes]:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **_SUBPROCESS_KW,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode or 0, out or b"", err or b""
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            proc.communicate()
            raise asyncio.TimeoutError()

    return await asyncio.to_thread(_run)


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

# Seriali validi: alfanumerici con - _ . : ammessi (TCP "ip:porta",
# mDNS "adb-xxx._adb-tls-connect._tcp.", emulator-5554). Tutto il resto —
# '(no', '*', 'adb:' — e' rumore dell'output adb/server di terzi che una
# volta finiva in known.json come device fantasma.
_SERIAL_VALID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?::[0-9]+)?$")


def _is_valid_serial(serial: str) -> bool:
    return bool(serial) and bool(_SERIAL_VALID_RE.match(serial))


# Mappa seriale -> porta del server adb che lo enumera (5037 standard,
# 5038 = QuickForward/Panda). Modulo-level perche' stream_engine crea
# subprocess adb propri e deve instradarsi sul server giusto.
_SERIAL_PORT: Dict[str, int] = {}


def _adb_port_listening(port: int) -> bool:
    """True se un server adb e' GIA' in ascolto sulla porta.

    Fondamentale: `adb -P <porta> ...` auto-avvia un daemon se la porta e'
    libera. Un server clone sulla 5038 con Panda chiuso contenderebbe i
    device USB al server 5037, resettando i transport e uccidendo tutti i
    forward (stream morti nello stesso secondo). Quindi le porte extra si
    interrogano solo se qualcuno (Panda) le sta gia' servendo.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def adb_server_args(serial: str) -> List[str]:
    """Argomenti -P da anteporre ai comandi adb per un seriale."""
    port = _SERIAL_PORT.get(serial, 5037)
    if port != 5037 and _adb_port_listening(port):
        return ["-P", str(port)]
    return []


# ------------------------------------------------------------------
# Server adb di terzi (Panda/QuickForward sulla 5038)
# ------------------------------------------------------------------
# Il loro binario ha una versione diversa dalla nostra, e un client adb
# che trova un server di versione diversa LO UCCIDE ("adb server is out
# of date, killing...") e ne auto-avvia uno proprio sulla stessa porta.
# Il clone contende i device USB al server vero: riavvii a catena e
# device che rimbalzano online/offline/unauthorized a raffica — la
# "guerra" osservata nei log con Panda attivo.
# Regola: le porte extra si interrogano SOLO via socket grezzo col
# protocollo smart-host (il check di versione vive nel client, non nel
# server), e i comandi ai device che vivono li' SOLO col binario del
# proprietario — stessa versione del suo server, nessun kill possibile.
_FOREIGN_ADB_CANDIDATES = (
    r"C:\Program Files (x86)\panda_android\tools\adb.exe",
    r"C:\Program Files\panda_android\tools\adb.exe",
)
_FOREIGN_ADB_CACHE: Tuple[float, str] = (0.0, "")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connessione chiusa dal server adb")
        buf += chunk
    return buf


def _adb_host_query(port: int, request: str, timeout: float = 3.0) -> Optional[str]:
    """Query smart-host via socket grezzo ('host:devices', 'host:devices-l').

    Protocollo adb server: 4 cifre esadecimali di lunghezza + comando, poi
    'OKAY' + 4 cifre + payload. None se il server non risponde OKAY.
    """
    payload = request.encode()
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(f"{len(payload):04x}".encode() + payload)
        if _recv_exact(s, 4) != b"OKAY":
            return None
        try:
            total = int(_recv_exact(s, 4), 16)
        except ValueError:
            return None
        return _recv_exact(s, total).decode("utf-8", errors="replace")


def _foreign_adb_path(own_adb: str = "") -> str:
    """Binario adb del server di terzi (es. Panda), '' se non trovato.

    Prima gli adb.exe gia' in esecuzione (il server 5038 di Panda e' esso
    stesso un adb.exe), poi i percorsi noti dei tool di terzi.
    """
    global _FOREIGN_ADB_CACHE
    now = time.monotonic()
    ts, cached = _FOREIGN_ADB_CACHE
    if now - ts < 60.0:
        return cached
    found = _find_running_adb(exclude=own_adb)
    if not found or not _adb_executable_works(found):
        found = ""
        for cand in _FOREIGN_ADB_CANDIDATES:
            if Path(cand).exists() and _adb_executable_works(cand):
                found = cand
                break
    _FOREIGN_ADB_CACHE = (now, found)
    return found


def adb_binary_for_serial(serial: str, default: str) -> str:
    """Binario adb da lanciare per un seriale: quello del server che lo enumera.

    Per i device sul server di terzi serve il LORO binario: il nostro
    client di versione diversa ucciderebbe il loro server a ogni comando.
    Ritorna '' se il seriale e' esterno ma il binario proprietario non
    e' stato trovato — meglio nessun comando che un kill-server.
    """
    port = _SERIAL_PORT.get(serial, 5037)
    if port == 5037:
        return default
    return _foreign_adb_path(default)


# Testi che indicano una pagina NON loggata: l'importo nel DOM e' promo
# ('2.000€ di bonus'), non il saldo reale — quelle pagine si scartano.
_LOGGED_OUT_RE = re.compile(
    r"non sei collegat|non hai un account|accedi|registrati|"
    r"log\s?in|sign\s?in|effettua l'?accesso",
    re.I,
)


def _clean_username(raw: str) -> str:
    """Pulisce lo username estratto dal DOM del bookmaker.

    Il testo grezzo puo' contenere righe multiple e parole di UI
    ('LOGOUT\nKekkopag', 'ESCI', 'Ciao Mario'): teniamo le righe che non
    sono comandi/saluti e togliamo i prefissi di cortesia.
    """
    parts = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(r"(?i)\b(log\s?out|sign\s?out|esci|disconnett)\b", line):
            line = re.sub(
                r"(?i)\b(log\s?out|sign\s?out|esci|disconnett)\b", "", line
            ).strip(" ,;:-")
            if not line:
                continue
        line = re.sub(
            r"(?i)^(?:ciao|benvenut[oa]|welcome|hello|hi)[,!\s]+", "", line
        ).strip()
        if line:
            parts.append(line)
    return " ".join(parts)[:40]


class AdbManager:
    """Worker asincrono per il monitoraggio dei dispositivi ADB."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._adb = settings.adb_path or "adb"
        # Cache dell'env ADB_VENDOR_KEYS (60s): il collect fa glob sul
        # filesystem e rifarlo a ogni comando era I/O ripetuto sul loop.
        self._adb_env_cache = None
        self._devices: Dict[str, DeviceState] = {}
        # Riferimento al StreamManager, iniettato da app.py dopo la
        # costruzione: serve a screen_off/screen_on per usare il canale
        # scrcpy (set_display_power) che spegne il pannello senza bloccare.
        self._streams = None
        self._labels: Dict[str, str] = load_labels()
        self._label_colors: Dict[str, str] = load_label_colors()
        self._order: Dict[str, int] = load_device_order()
        self._tags: Dict[str, List[str]] = load_tags()
        self._played_serials: set = set(load_played())
        self._skipped_serials: set = set(load_skipped())
        self._known: Dict[str, dict] = load_known()
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        # True se l'ultimo poll di _refresh_devices ha visto almeno una
        # porta adb rispondere (rc==0). Usato da _poll_loop per il backoff
        # quando adb e' del tutto irraggiungibile (es. WinError 5 persistente
        # sul binario altrui): senza questo flag il poll continuerebbe a
        # martellare adb ogni 5s generando un flood di errori identici.
        self._last_poll_ok: bool = True
        self._change_callbacks: List = []
        # Auto-clicker per device: serial -> task asyncio
        self._autoclick_tasks: Dict[str, asyncio.Task] = {}
        # Throttle per `adb reconnect` automatico su device bloccati
        self._last_reconnect: Dict[str, float] = {}
        # Reload chiavi vendor per i device 'unauthorized': il server gira
        # con una chiave che i telefoni non hanno autorizzato (tipico dopo
        # il passaggio al binario adb di un'altra app, es. Panda).
        self._key_reload_attempts = 0
        self._keys_loaded: set = set()
        # Contatore poll consecutivi in cui un device non appare in adb devices
        self._missing: Dict[str, int] = {}
        # Stato saldi corrente: serial -> {saldo, bookmaker, username, nome, timestamp}
        # Caricato da disco all'avvio, aggiornato in background a ogni lettura.
        self._balances: Dict[str, dict] = load_balances_state()
        # Task di auto-lettura saldi in background per device (serial -> task)
        self._balance_tasks: Dict[str, asyncio.Task] = {}
        # Semaforo globale sulle letture CDP: ogni lettura fa 2 spawn adb
        # (forward + remove) piu' HTTP+WS; senza limite le ~25 letture si
        # risincronizzano a raffica ogni 30s e la congestione del loop fa
        # desincronizzare TUTTI i subscriber video nello stesso istante —
        # e' la causa dei "Video capture reset" di massa nel log.
        self._balance_sem = asyncio.Semaphore(2)
        # Forward CDP persistenti per serial: un 'adb forward' creato una
        # sola volta viene riusato a ogni lettura invece di essere
        # creato+distrutto a ciclo (2 comandi adb in meno per lettura).
        # Viene dimenticato solo quando la lettura fallisce.
        self._cdp_fwd: Dict[str, int] = {}
        # Timestamp ultima auto-lettura per device (throttle: non ripetere
        # prima di 60s per non saturare adb)
        self._last_balance_read: Dict[str, float] = {}
        # Cache saldi: serial -> {data, timestamp}; TTL 300s
        self._balance_cache: Dict[str, dict] = {}
        self._balance_cache_ttl: float = 300.0
        # Tabella conti Ledger caricata da CSV: usata per sync manuale
        # utente/bookmaker -> accountId
        self._ledger_accounts: List[dict] = []
        self._ledger_account_map: Dict[tuple, str] = {}
        self._load_ledger_csv()

    def _load_ledger_csv(self) -> None:
        """Carica il CSV esportato da Ledger se esiste in .griddroid."""
        try:
            csv_name = self._settings.ledger_accounts_csv
            if not csv_name:
                return
            csv_path = CONFIG_DIR / csv_name
            if not csv_path.is_absolute():
                csv_path = CONFIG_DIR / csv_path
            if not csv_path.exists():
                return
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                self._ledger_accounts = list(reader)
            for row in self._ledger_accounts:
                nick = row.get("nickname", "").strip().lower()
                book = row.get("bookmaker", "").strip().lower()
                if nick and book:
                    self._ledger_account_map[(nick, book)] = row.get("accountId", "").strip()
            logs.info(
                f"Caricati {len(self._ledger_accounts)} conti Ledger da CSV"
            )
        except Exception as exc:
            logs.warn(f"Errore caricamento CSV Ledger: {exc}")

    def reload_state(self) -> None:
        """Ricarica da disco tutto lo stato persistito (dopo un import).

        Aggiorna labels, colori, ordine, tag, giocati, skipati, seriali
        noti, saldi e il CSV Ledger, poi ri-applica label/played/skipped
        ai device gia' visti cosi' la griglia si allinea subito.
        """
        self._labels = load_labels()
        self._label_colors = load_label_colors()
        self._order = load_device_order()
        self._tags = load_tags()
        self._played_serials = set(load_played())
        self._skipped_serials = set(load_skipped())
        self._known = load_known()
        self._balances = load_balances_state()
        self._drop_invalid_serials()
        self._ledger_account_map = {}
        self._ledger_accounts = []
        self._load_ledger_csv()
        for serial, dev in self._devices.items():
            dev.label = self._labels.get(serial, dev.label)
            dev.label_color = self._label_colors.get(serial, "")
            dev.tags = list(self._tags.get(serial, []))
            dev.played = serial in self._played_serials
            dev.skipped = serial in self._skipped_serials

    @property
    def devices(self) -> Dict[str, DeviceState]:
        return self._devices

    def get_device(self, serial: str) -> Optional[DeviceState]:
        return self._devices.get(serial)

    @property
    def balances(self) -> Dict[str, dict]:
        """Stato saldi corrente: serial -> {saldo, bookmaker, username, nome, timestamp}."""
        return self._balances

    def record_balance(
        self,
        serial: str,
        saldo: str,
        bookmaker: str = "",
        username: str = "",
        nome: str = "",
        timestamp: str = "",
    ) -> None:
        """Registra un saldo letto: aggiorna il corrente e lo storico per-book.

        books[bookmaker] conserva l'ultima lettura per OGNI book del
        telefono: la matrice saldi mostra cosi' una cella per ciascun
        book, non solo per quello rilevato piu' di recente.
        """
        ts = timestamp or time.strftime("%Y-%m-%d %H:%M:%S")
        if not nome:
            dev = self._devices.get(serial)
            nome = dev.display_name if dev else serial
        entry = self._balances.setdefault(serial, {})
        entry.update(
            {
                "saldo": saldo,
                "bookmaker": bookmaker,
                "username": username,
                "nome": nome,
                "timestamp": ts,
            }
        )
        if bookmaker:
            old_rec = entry.get("books", {}).get(bookmaker) or {}
            old_saldo = old_rec.get("saldo")
            # diff = variazione rispetto all'ultimo valore CAMBIATO: se il
            # saldo e' identico conserviamo il diff gia' visto, cosi' la
            # matrice continua a mostrare la direzione dell'ultima mossa
            # invece di azzerarla a ogni rilettura uguale.
            diff = old_rec.get("diff")
            if old_saldo is None:
                diff = None
            elif old_saldo != saldo:
                try:
                    diff = round(float(saldo) - float(old_saldo), 2)
                except (TypeError, ValueError):
                    diff = None
            entry.setdefault("books", {})[bookmaker] = {
                "saldo": saldo,
                "username": username,
                "timestamp": ts,
                "diff": diff,
            }
        save_balances_state(self._balances)

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

    def set_skipped(self, serial: str, skipped: bool = True) -> None:
        """Segna un dispositivo come non giocato e lo salva su disco."""
        if skipped:
            self._skipped_serials.add(serial)
        else:
            self._skipped_serials.discard(serial)
        if serial in self._devices:
            self._devices[serial].skipped = skipped
        save_skipped(sorted(self._skipped_serials))
        logs.info("Dispositivo segnato come non giocato" if skipped else "Dispositivo rimosso dai non giocati", serial=serial)

    def reset_skipped(self) -> None:
        """Ripristina tutti i dispositivi non giocati."""
        self._skipped_serials.clear()
        for dev in self._devices.values():
            dev.skipped = False
        save_skipped([])
        logs.info("Ripristinati tutti i dispositivi non giocati")

    def _drop_invalid_serials(self) -> None:
        """Rimuove seriali spurie (es. '(no' da output adb corrotto o da
        un backup importato) da tutto lo stato persistito."""
        bad = {s for s in self._known if not _is_valid_serial(s)}
        if not bad:
            return
        for s in bad:
            self._devices.pop(s, None)
            self._known.pop(s, None)
            self._labels.pop(s, None)
            self._label_colors.pop(s, None)
            self._order.pop(s, None)
            self._tags.pop(s, None)
            self._played_serials.discard(s)
            self._skipped_serials.discard(s)
            self._balances.pop(s, None)
        save_known(self._known)
        save_labels(self._labels)
        save_label_colors(self._label_colors)
        save_device_order(self._order)
        save_tags(self._tags)
        save_played(sorted(self._played_serials))
        save_skipped(sorted(self._skipped_serials))
        save_balances_state(self._balances)
        logs.warn(f"Rimossi seriali non validi: {sorted(bad)}")

    def remove_device(self, serial: str) -> bool:
        """Elimina un device: memoria + tutti i file persistiti.

        Per telefoni vecchi/venduti e card duplicate che restano in
        griglia. Lo storico CSV dei saldi resta (e' l'audit di Ledger);
        gli override stream li rimuove StreamManager.remove_device_override.
        """
        existed = serial in self._devices or serial in self._known
        self.stop_autoclick(serial)
        task = self._balance_tasks.pop(serial, None)
        if task and not task.done():
            task.cancel()
        self._devices.pop(serial, None)
        self._known.pop(serial, None)
        self._labels.pop(serial, None)
        self._label_colors.pop(serial, None)
        self._order.pop(serial, None)
        self._tags.pop(serial, None)
        self._played_serials.discard(serial)
        self._skipped_serials.discard(serial)
        self._balances.pop(serial, None)
        self._missing.pop(serial, None)
        self._last_reconnect.pop(serial, None)
        self._last_balance_read.pop(serial, None)
        self._balance_cache.pop(serial, None)
        _SERIAL_PORT.pop(serial, None)
        save_known(self._known)
        save_labels(self._labels)
        save_label_colors(self._label_colors)
        save_device_order(self._order)
        save_tags(self._tags)
        save_played(sorted(self._played_serials))
        save_skipped(sorted(self._skipped_serials))
        save_balances_state(self._balances)
        if existed:
            logs.info("Dispositivo eliminato", serial=serial)
        return existed

    # ------------------------------------------------------------------
    # Auto-clicker (anti-rilevamento)
    # ------------------------------------------------------------------

    def autoclick_active(self, serial: str) -> bool:
        task = self._autoclick_tasks.get(serial)
        return bool(task and not task.done())

    def start_autoclick(
        self,
        serial: str,
        x: int,
        y: int,
        interval_ms: int = 1000,
        jitter_px: int = 8,
        count: int = 0,
    ) -> bool:
        """Avvia l'auto-clicker su un device in (x, y) con timing umano."""
        if serial not in self._devices:
            return False
        self.stop_autoclick(serial)
        dev = self._devices[serial]
        dev.autoclick = True
        self._autoclick_tasks[serial] = asyncio.create_task(
            self._autoclick_loop(serial, x, y, max(150, interval_ms), jitter_px, count)
        )
        logs.info(
            f"Auto-click avviato ({x},{y} ogni ~{interval_ms}ms, count={count or 'inf'})", serial=serial
        )
        return True

    def stop_autoclick(self, serial: str) -> None:
        """Ferma l'auto-clicker di un device."""
        task = self._autoclick_tasks.pop(serial, None)
        if task and not task.done():
            task.cancel()
        if serial in self._devices:
            self._devices[serial].autoclick = False
            logs.info("Auto-click fermato", serial=serial)

    async def _autoclick_loop(
        self, serial: str, x: int, y: int, interval_ms: int, jitter_px: int, count: int = 0
    ) -> None:
        """Loop di click con pattern il piu' possibile umano.

        Anti-rilevamento:
        - intervallo randomizzato attorno alla media (+-40%)
        - posizione con jitter casuale entro jitter_px
        - pressione simulata con 'input swipe' a durata variabile (60-140ms)
          invece di 'input tap' (troppo istantaneo/meccanico)
        - ~4% di probabilita' di una pausa lunga (2-6s), come farebbe una persona
        """
        import random

        try:
            i = 0
            while True:
                if count and i >= count:
                    break
                i += 1
                dev = self._devices.get(serial)
                if not dev or dev.status != DeviceStatus.ONLINE:
                    break

                jx = x + random.randint(-jitter_px, jitter_px)
                jy = y + random.randint(-jitter_px, jitter_px)
                duration = random.randint(60, 140)
                await self.shell(
                    serial,
                    f"input swipe {jx} {jy} {jx} {jy} {duration}",
                    timeout=10.0,
                )

                # Pausa lunga occasionale: rompe la regolarita' del pattern
                if random.random() < 0.04:
                    await asyncio.sleep(random.uniform(2.0, 6.0))

                wait = interval_ms / 1000.0 * random.uniform(0.6, 1.4)
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logs.warn(f"Auto-click interrotto: {exc}", serial=serial)
        finally:
            if serial in self._devices:
                self._devices[serial].autoclick = False
            self._autoclick_tasks.pop(serial, None)

    # ------------------------------------------------------------------
    # Lettura saldo a schermo
    # ------------------------------------------------------------------

    # Package -> nome bookmaker (app di gioco piu' diffuse)
    _BOOKMAKERS = {
        "bet365": "Bet365", "snai": "SNAI", "eurobet": "Eurobet",
        "goldbet": "Goldbet", "planetwin": "PlanetWin365", "sisal": "Sisal",
        "lottomatica": "Lottomatica", "betflag": "Betflag",
        "betfair": "Betfair", "starcasino": "StarCasino",
        "williamhill": "William Hill", "bwin": "Bwin",
        "pokerstars": "PokerStars", "888": "888", "unibet": "Unibet",
        "betway": "Betway", "leovegas": "LeoVegas", "admiral": "AdmiralBet",
        "betsson": "Betsson", "daznbet": "DaznBet", "netbet": "NetBet", "fantasyteam": "FantasyTeam",
        "betclic": "Betclic", "novibet": "Novibet", "stake": "Stake",
        # Book della sezione Bookmakers senza package nella mappa
        "betpassion": "BetPassion", "betwin": "BetWin360",
        "betpoint": "Betpoint", "domusbet": "DomusBet",
        "eplay24": "Eplay24", "fastbet": "Fastbet",
        "gioca7": "Gioca7", "giocodigitale": "Gioco Digitale",
        "marathon": "MarathonBet", "mylottery": "MyLottery",
        "quigioco": "QuiGioco", "sportbet": "SportBet",
        "sportium": "Sportium", "stanleybet": "StanleyBet",
        "starvegas": "StarVegas", "staryes": "StarYes",
        "sunbet": "Sunbet", "totosi": "Totosì",
        "vincitu": "VinciTu", "zonagioco": "ZonaGioco",
    }

    @staticmethod
    def _normalize_amount(raw: str) -> Optional[str]:
        """Normalizza un importo in formato canonico '1234.56'.

        Gestisce sia il formato italiano (1.234,56) sia quello anglosassone
        (1,234.56): il separatore decimale e' quello seguito da 1-2 cifre
        finali, gli altri sono separatori delle migliaia.
        """
        s = re.sub(r"[^\d.,]", "", raw)
        if not s:
            return None
        # Ultimo separatore seguito da 1-2 cifre a fine stringa = decimale
        m = re.search(r"([.,])(\d{1,2})$", s)
        if m:
            int_part = re.sub(r"[.,]", "", s[: m.start()])
            dec = m.group(2).ljust(2, "0")
            return f"{int_part}.{dec}" if int_part else f"0.{dec}"
        # Nessun decimale: intero puro
        digits = re.sub(r"[.,]", "", s)
        return f"{digits}.00" if digits else None

    async def _foreground_package(self, serial: str) -> str:
        """Package dell'app in foreground (per identificare il bookmaker)."""
        try:
            out = await self.shell(
                serial,
                "dumpsys activity activities | grep -i resumed",
                timeout=10.0,
            )
            m = re.search(r"(?:mResumedActivity|topResumedActivity)[^\s]*\s+([\w.]+)/", out)
            if not m:
                m = re.search(r"([\w.]+)/[\w.]+", out)
            return m.group(1) if m else ""
        except Exception:
            return ""

    def _bookmaker_from_package(self, package: str) -> str:
        low = package.lower()
        for key, name in self._BOOKMAKERS.items():
            if key in low:
                return name
        return ""

    # ------------------------------------------------------------------
    # Auto-lettura saldi in background
    # ------------------------------------------------------------------

    def _schedule_balance_read(self, serial: str) -> None:
        """Schedula una lettura saldo in background per un device appena ONLINE.

        Non blocca il poll loop ne' l'uso del telefono: usa solo CDP
        (Chrome DevTools Protocol), che legge il DOM via WebSocket senza
        congelare la UI del device. Se Chrome non e' in foreground il
        saldo non viene letto in automatico — l'utente puo' sempre
        forzarlo col bottone 'Leggi saldi' (che usa anche uiautomator).

        Throttle 60s per device: non ripete la lettura se e' appena stata
        fatta, per non saturare adb con forward ripetuti.
        """
        if not self._running:
            return
        dev = self._devices.get(serial)
        if not dev or dev.status != DeviceStatus.ONLINE:
            return
        now = time.time()
        # Jitter deterministico per device (0-4.5s): senza fase propria le
        # letture restano allineate dopo ogni raffica e la congestione si
        # ripete a cadenza fissa. hash() del seriale e' stabile in processo.
        jitter = (hash(serial) % 10) * 0.5
        if now - self._last_balance_read.get(serial, 0.0) < 30.0 + jitter:
            return
        # Una task per device alla volta
        existing = self._balance_tasks.get(serial)
        if existing and not existing.done():
            return
        self._last_balance_read[serial] = now
        self._balance_tasks[serial] = asyncio.ensure_future(
            self._auto_read_balance(serial)
        )

    async def _auto_read_balance(self, serial: str) -> None:
        """Lettura saldo background: CDP-only, non blocca il device."""
        try:
            # Solo CDP: niente uiautomator (congela la UI). Se Chrome non
            # c'e', il saldo non si aggiorna in automatico — ma il telefono
            # resta usabile dall'utente, che e' il requisito.
            async with self._balance_sem:
                cdp = await self._saldo_via_cdp(serial)
            self._record_cdp(serial, cdp)
        except Exception as exc:
            logs.warn(f"Auto-lettura saldo fallita: {exc}", serial=serial, throttle_s=60)

    def _record_cdp(self, serial: str, cdp: dict) -> int:
        """Persiste il risultato CDP: il saldo della tab migliore piu'
        quelli di OGNI tab su un bookmaker noto (un telefono con piu'
        book aperti aggiorna tutta la sua riga della matrice).
        Ritorna quanti record sono stati scritti."""
        written = 0
        if cdp.get("saldo"):
            self.record_balance(
                serial,
                cdp["saldo"],
                bookmaker=cdp.get("bookmaker", ""),
                username=cdp.get("username", ""),
            )
            written += 1
        for b in cdp.get("books") or []:
            # record_balance ignora duplicati identici gia' freschi
            self.record_balance(
                serial,
                b["saldo"],
                bookmaker=b["bookmaker"],
                username=b.get("username", ""),
            )
            written += 1
        if written:
            logs.info(
                f"Saldo auto: {cdp.get('saldo') or '-'} "
                f"({cdp.get('bookmaker', '?')}) +{written - 1} book",
                serial=serial,
                throttle_s=30,
            )
        return written

    async def read_account_info(
        self,
        serial: str,
        timeout: float = 15.0,
        priority: str = "state",
        force_refresh: bool = False,
    ) -> dict:
        """Legge saldo, bookmaker e username visibili a schermo.

        Saldo: prima i nodi con parole chiave (saldo/balance/totale), poi gli
        importi con simbolo di valuta — sempre normalizzati a '1234.56'.
        Bookmaker: dal package dell'app in foreground.
        Username: nodi vicino a 'ciao'/'benvenuto'/'account'/'profilo'.
        """
        info = {"saldo": None, "bookmaker": "", "username": ""}
        t0 = time.monotonic()

        # Cache: se i saldi sono recenti, non rompere il device.
        if not force_refresh and serial in self._balance_cache:
            cached = self._balance_cache[serial]
            age = time.monotonic() - cached.get("timestamp", 0)
            if age < self._balance_cache_ttl:
                logs.info(
                    f"Saldo da cache ({int(age)}s)",
                    serial=serial,
                    throttle_s=60,
                )
                return cached["data"]

        # --- Canale 1: CDP/DOM (Chrome in foreground) ---
        # Se il device ha Chrome aperto, il saldo si legge direttamente dal
        # DOM via DevTools Protocol: precisione assoluta, niente parsing
        # dell'albero accessibility. Fallisce in fretta se Chrome non c'e'.
        cdp = await self._saldo_via_cdp(serial)
        if cdp.get("bookmaker"):
            info["bookmaker"] = cdp["bookmaker"]
        if cdp.get("saldo"):
            info["saldo"] = cdp["saldo"]
            info["username"] = cdp.get("username", "")
            logs.info(
                f"Saldo {info['saldo']} via CDP "
                f"in {time.monotonic() - t0:.1f}s",
                serial=serial,
            )
            await self._sync_balance(serial, info)
            return info

        # --- Canale 2: accessibility tree (uiautomator dump) ---
        # Una sola chiamata adb: dump diretto su stdout (niente file
        # intermedio + cat). Il messaggio "UI hierchary dumped to:" va
        # tolto: l'XML vero sta tra <hierarchy> e </hierarchy>.
        xml = ""
        try:
            out = await self.shell(
                serial, "uiautomator dump /dev/stdout",
                timeout=timeout, priority=priority,
            )
            start = out.find("<hierarchy")
            end = out.rfind("</hierarchy>")
            if start >= 0 and end > start:
                xml = out[start : end + len("</hierarchy>")]
        except Exception:
            pass
        if not xml:
            # Fallback: metodo file (device dove /dev/stdout non va)
            try:
                await self.shell(
                    serial, "uiautomator dump /sdcard/griddroid_ui.xml",
                    timeout=timeout, priority=priority,
                )
                xml = await self.shell(
                    serial, "cat /sdcard/griddroid_ui.xml",
                    timeout=timeout, priority=priority,
                )
            except Exception as exc:
                logs.warn(f"Lettura saldo fallita: {exc}", serial=serial)
                return info
        if not xml:
            return info

        texts = [t for t in re.findall(r'text="([^"]+)"', xml) if t.strip()]

        # --- Bookmaker: il package dell'app in foreground e' gia' nei nodi
        # del dump (attributo package=) — niente dumpsys separato.
        pkgs = re.findall(r'package="([^"]+)"', xml)
        if pkgs and not info.get("bookmaker"):
            # Il package piu' frequente e' quello dell'app a schermo
            pkg = max(set(pkgs), key=pkgs.count)
            bm = self._bookmaker_from_package(pkg)
            if bm:
                info["bookmaker"] = bm

        # --- Saldo ---
        # 0) Posizione: il numero accanto al simbolo € sulla stessa riga.
        #    I bounds dei nodi danno le coordinate — come leggere lo
        #    screenshot, ma senza OCR.
        info["saldo"] = self._saldo_from_position(xml)

        kw = re.compile(
            r"saldo|balance|totale|available|disponibil|conto|wallet|fondi",
            re.IGNORECASE,
        )
        money = re.compile(
            r"(?:€|eur|usd|\$|£)\s*([0-9][0-9.,\s]*[0-9])"
            r"|([0-9][0-9.,]*[0-9])\s*(?:€|eur|usd|\$|£)",
            re.IGNORECASE,
        )
        num = re.compile(r"[0-9]+(?:[.,][0-9]+)*[.,][0-9]{1,2}\b")
        # 1) numero nel nodo con keyword, o nei 2 nodi successivi
        if not info["saldo"]:
            for i, t in enumerate(texts):
                if kw.search(t):
                    for t2 in [t] + texts[i + 1 : i + 3]:
                        m = num.search(t2) or money.search(t2)
                        if m:
                            raw = m.group(0) if m.re is num else (m.group(1) or m.group(2))
                            val = self._normalize_amount(raw)
                            if val:
                                info["saldo"] = val
                                break
                    if info["saldo"]:
                        break
        # 2) fallback: primo importo con simbolo di valuta
        if not info["saldo"]:
            for t in texts:
                m = money.search(t)
                if m:
                    val = self._normalize_amount(m.group(1) or m.group(2))
                    if val:
                        info["saldo"] = val
                        break

        # --- Username: nodo dopo 'ciao'/'benvenuto' o vicino ad account ---
        user_kw = re.compile(
            r"ciao|benvenut|salve|account|profilo|utente|user", re.IGNORECASE
        )
        for i, t in enumerate(texts):
            if user_kw.search(t):
                # 'Ciao Mario' -> 'Mario'; altrimenti il nodo successivo
                m = re.search(
                    r"(?:ciao|benvenut\w*|salve)\s+([A-Za-z0-9_.'-]{2,30})",
                    t, re.IGNORECASE,
                )
                cand = m.group(1) if m else (texts[i + 1] if i + 1 < len(texts) else "")
                cand = cand.strip()
                if cand and not kw.search(cand) and not money.search(cand) and len(cand) <= 40:
                    info["username"] = cand
                    break

        dur = time.monotonic() - t0
        if info["saldo"]:
            logs.info(
                f"Saldo {info['saldo']} via uiautomator in {dur:.1f}s",
                serial=serial,
            )
        else:
            logs.warn(
                f"Saldo non trovato dopo {dur:.1f}s "
                f"(CDP fallito, dump senza importi)",
                serial=serial,
            )
        # Salva in cache solo se abbiamo trovato un saldo, per non
        # ritenere memorizzati valori mancanti.
        if info.get("saldo"):
            self._balance_cache[serial] = {
                "data": info,
                "timestamp": time.monotonic(),
            }
            await self._sync_balance(serial, info)
        return info

    async def _sync_balance(
        self, serial: str, info: dict, account_id: Optional[str] = None
    ) -> None:
        """Spedisce il saldo letto a Ledger se configurato.

        Priorita':
        1. account_id passato esplicitamente (sync manuale da CSV).
        2. ledger_account_map[serial] -> aggiornamento diretto per accountId.
        3. ledger_user_id + nome telefono + bookmaker -> ricerca su Ledger.
        """
        url = self._settings.ledger_sync_url
        token = self._settings.ledger_sync_token
        if not url or not token:
            return
        if not info.get("saldo"):
            return

        if account_id is None:
            account_id = self._settings.ledger_account_map.get(serial)
        user_id = self._settings.ledger_user_id
        if not account_id and not user_id:
            return

        dev = self._devices.get(serial)
        nome = dev.display_name if dev else serial

        payload: dict = {
            "saldo": float(info["saldo"]),
            "bookmaker": info.get("bookmaker", ""),
            "username": info.get("username", ""),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if account_id:
            payload["accountId"] = account_id
        else:
            payload["userId"] = user_id
            payload["nome"] = nome

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": token,
            },
            method="POST",
        )
        try:
            await asyncio.to_thread(
                urllib.request.urlopen, req, timeout=10.0
            )
            logs.info("Saldo sincronizzato con Ledger", serial=serial)
        except Exception as exc:
            logs.warn(f"Sincronizzazione Ledger fallita: {exc}", serial=serial)

    def get_ledger_nicknames(self) -> List[str]:
        """Restituisce i nickname unici caricati dal CSV Ledger."""
        nicks = {r.get("nickname", "").strip() for r in self._ledger_accounts}
        return sorted(n for n in nicks if n)

    def _serial_by_display_name(self, name: str) -> Optional[str]:
        """Trova il serial del device attualmente online con quel nome."""
        target = name.strip().lower()
        for serial, dev in self._devices.items():
            if dev.display_name and dev.display_name.strip().lower() == target:
                return serial
        return None

    async def sync_ledger_user(self, nickname: str) -> dict:
        """Legge il saldo dallo schermo del telefono di 'nickname' e lo sincronizza
        con l'account Ledger corrispondente al bookmaker attualmente aperto."""
        serial = self._serial_by_display_name(nickname)
        if not serial:
            return {
                "ok": False,
                "error": f"Telefono '{nickname}' non online o nome non trovato",
                "nickname": nickname,
            }

        dev = self._devices.get(serial)
        if not dev or dev.status.name != "ONLINE":
            return {
                "ok": False,
                "error": f"Telefono '{nickname}' non online",
                "nickname": nickname,
                "serial": serial,
            }

        info = await self.read_account_info(serial, force_refresh=True)
        if not info.get("saldo"):
            return {
                "ok": False,
                "error": "Saldo non trovato a schermo",
                "nickname": nickname,
                "serial": serial,
            }

        bookmaker = (info.get("bookmaker") or "").strip().lower()
        account_id = self._ledger_account_map.get((nickname.strip().lower(), bookmaker))
        if not account_id:
            return {
                "ok": False,
                "error": f"Nessun conto CSV per {nickname} + {bookmaker}",
                "nickname": nickname,
                "serial": serial,
                "bookmaker": bookmaker,
            }

        await self._sync_balance(serial, info, account_id=account_id)
        return {
            "ok": True,
            "nickname": nickname,
            "serial": serial,
            "bookmaker": bookmaker,
            "saldo": info["saldo"],
            "accountId": account_id,
        }

    async def sync_ledger_all(self) -> List[dict]:
        """Sincronizza tutti i telefoni online il cui nome è un nickname del CSV.

        Ogni telefono viene letto una sola volta: viene sincronizzato il bookmaker
        che l'utente ha aperto sullo schermo in quel momento.
        """
        nicks = self.get_ledger_nicknames()
        results: List[dict] = []
        for nickname in nicks:
            serial = self._serial_by_display_name(nickname)
            if not serial:
                results.append({
                    "ok": False,
                    "error": "Telefono non online",
                    "nickname": nickname,
                })
                continue
            res = await self.sync_ledger_user(nickname)
            results.append(res)
        return results

    def _saldo_from_position(self, xml: str) -> Optional[str]:
        """Saldo per posizione: il numero accanto al simbolo €.

        Ogni nodo del dump ha bounds="[x1,y1][x2,y2]": trovo il nodo che
        contiene la valuta e prendo l'importo nel nodo stesso, oppure nel
        nodo piu' vicino sulla stessa riga a destra (label e importo sono
        spesso elementi separati e affiancati).
        """
        num = re.compile(r"[0-9]+(?:[.,][0-9]+)*[.,][0-9]{1,2}\b")
        nodes = []
        for m in re.finditer(r"<node\b[^>]*>", xml):
            tag = m.group(0)
            tm = re.search(r'text="([^"]*)"', tag)
            bm = re.search(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag
            )
            if tm and bm:
                nodes.append(
                    (tm.group(1),) + tuple(int(g) for g in bm.groups())
                )
        for text, x1, y1, x2, y2 in nodes:
            if "€" not in text and "eur" not in text.lower():
                continue
            # Caso 1: importo nello stesso nodo ("€ 118,00")
            m = num.search(text)
            if m:
                val = self._normalize_amount(m.group(0))
                if val:
                    return val
            # Caso 2: nodo solo '€' -> numero piu' vicino a destra, stessa riga
            cy = (y1 + y2) / 2
            height = max(y2 - y1, 1)
            best = None
            for t2, a1, b1, a2, b2 in nodes:
                if a1 <= x2:
                    continue  # deve stare a destra del simbolo
                if abs((b1 + b2) / 2 - cy) > height:
                    continue  # non sulla stessa riga
                m2 = num.search(t2)
                if m2:
                    dist = a1 - x2
                    if best is None or dist < best[0]:
                        best = (dist, m2.group(0))
            if best:
                val = self._normalize_amount(best[1])
                if val:
                    return val
        return None

    async def read_balance(self, serial: str) -> Optional[str]:
        """Compatibilita': restituisce solo il saldo normalizzato."""
        return (await self.read_account_info(serial))["saldo"]

    # ------------------------------------------------------------------
    # Saldo via Chrome DevTools Protocol (DOM, precisione assoluta)
    # ------------------------------------------------------------------

    # JS eseguito nella pagina: cerca il saldo nel DOM per selettori
    # mirati, poi per keyword, poi per primo importo con valuta.
    _CDP_JS = r"""
(() => {
  const money = /(?:€|EUR|USD|\$|£)\s*[0-9][0-9.,\s]*[0-9]|[0-9][0-9.,]*[0-9]\s*(?:€|EUR|USD|\$|£)/i;
  const kw = /saldo|balance|totale|available|disponibil|conto|wallet|fondi|credit/i;
  const pick = t => { const m = t.match(money); return m ? m[0] : null; };
  // Testo VISIBILE: innerText e' vuoto su display:none, ma textContent no —
  // il fallback va usato solo se l'elemento e' davvero visibile, altrimenti
  // si leggono saldi nascosti (es. 'bonus 0,00') al posto di quello reale.
  const vis = el => {
    const it = (el.innerText || '').trim();
    if (it) return it;
    const visible = el.checkVisibility ? el.checkVisibility() : el.offsetParent !== null;
    return visible ? (el.textContent || '').trim() : '';
  };
  // Username: classi tipiche dei widget account, o testo 'Ciao X' /
  // 'Benvenuto X' nei nodi foglia. Il valore resta grezzo — il trim dei
  // prefissi saluto e' nel parsing python.
  const userSels = ['[class*="username" i]','[class*="user-name" i]',
                    '[class*="nickname" i]','[class*="account" i]',
                    '[class*="profile" i]','[data-testid*="user" i]'];
  const greet = /^(?:ciao|benvenut[oa]|welcome|hello|hi)[,!\s]+(.{1,30})$/i;
  const findUser = () => {
    for (const s of userSels) {
      for (const el of document.querySelectorAll(s)) {
        const t = vis(el);
        if (t && t.length > 2 && t.length < 40 && !money.test(t)) {
          const g = t.match(greet); return g ? g[1].trim() : t;
        }
      }
    }
    for (const el of document.querySelectorAll('body *')) {
      if (el.children.length) continue;
      const t = vis(el);
      if (t && t.length < 60) { const g = t.match(greet); if (g) return g[1].trim(); }
    }
    return '';
  };
  // Picker di importi (deposito/ricarica): se il genitore contiene >=3
  // figli con solo un importo, l'elemento e' un'opzione da scegliere —
  // '50000 €' NON e' il saldo, e' un bottone dell'importo da versare.
  const isPicker = el => {
    const p = el.parentElement;
    if (!p) return false;
    let m = 0;
    for (const k of p.children) {
      const kt = vis(k);
      if (kt && kt.length < 20 && pick(kt)) m++;
    }
    return m >= 3;
  };
  // CTA di login visibile (Accedi/Registrati): pagina NON loggata —
  // ogni importo nel DOM e' promo o widget di versamento, mai il saldo.
  // Il match e' sul testo ESATTO del bottone: una promo "Registrati al
  // torneo" (testo piu' lungo) non fa scattare il flag.
  const cta = /^(?:accedi|registrati(?:\s+ora)?|entra|iscriviti|log\s?in|sign\s?in)$/i;
  const ctaHit = el => {
    const t = (vis(el) || el.value || '').trim();
    return t && t.length < 25 && cta.test(t);
  };
  let lo = false;
  for (const el of document.querySelectorAll('a,button,[role="button"],input,[class*="btn" i],[class*="button" i],[class*="login" i],[class*="registr" i],[class*="accedi" i]')) {
    if (ctaHit(el)) { lo = true; break; }
  }
  if (!lo) for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue;
    if (ctaHit(el)) { lo = true; break; }
  }
  const ret = v => ({saldo: v, site: location.hostname, user: findUser(), vis: document.visibilityState, lo});
  if (lo) return ret(null);
  const selsStrong = ['[class*="balance" i]','[class*="saldo" i]','[id*="balance" i]',
                '[id*="saldo" i]','[class*="wallet" i]','[class*="credit" i]',
                '[class*="credito" i]','[class*="funds" i]',
                '[aria-label*="saldo" i]','[aria-label*="balance" i]',
                '[data-testid*="balance" i]','[data-testid*="saldo" i]',
                '[id*="wallet" i]','[id*="credit" i]'];
  const selsWeak = ['[class*="amount" i]','[class*="money" i]',
                '[class*="cash" i]','[class*="deposit" i]','[class*="importo" i]'];
  for (const s of selsStrong) {
    for (const el of document.querySelectorAll(s)) {
      const t = vis(el);
      if (t && t.length < 80) {
        const v = pick(t);
        if (v && !isPicker(el)) return ret(v);
      }
    }
  }
  const leaves = document.querySelectorAll('body *');
  for (const el of leaves) {
    if (el.children.length) continue;
    const t = vis(el);
    if (t && t.length < 80 && kw.test(t)) {
      const v = pick(t);
      if (v) return ret(v);
    }
  }
  if (!lo) {
    for (const s of selsWeak) {
      for (const el of document.querySelectorAll(s)) {
        const t = vis(el);
        if (t && t.length < 80) {
          const v = pick(t);
          if (v && !isPicker(el)) return ret(v);
        }
      }
    }
    for (const el of leaves) {
      if (el.children.length) continue;
      const t = vis(el);
      if (t && t.length < 40) {
        const v = pick(t);
        if (v && !isPicker(el)) return ret(v);
      }
    }
  }
  return ret(null);
})()
"""

    async def _saldo_via_cdp(self, serial: str) -> dict:
        """Saldo dal DOM di Chrome via DevTools Protocol su adb forward.

        Chrome espone un socket abstract 'chrome_devtools_remote': con
        'adb forward' lo mappiamo su TCP locale, leggiamo i target da
        /json e valutiamo JS nella pagina attiva. Se Chrome non e' in
        esecuzione il socket non esiste e tutto fallisce in <1s.
        """
        empty = {"saldo": None, "bookmaker": "", "username": ""}
        port = 0
        try:
            import websockets  # dipendenza gia' in requirements
        except Exception:
            return empty
        try:
            # Riusa il forward persistente se esiste; altrimenti creane uno
            # su una porta locale libera e tienilo per le prossime letture.
            port = self._cdp_fwd.get(serial, 0)
            if not port:
                for _ in range(20):
                    candidate = random.randint(39300, 39900)
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(0.2)
                        if s.connect_ex(("127.0.0.1", candidate)) != 0:
                            port = candidate
                            break
                if not port:
                    return empty

                rc, _, _ = await self.adb_command(
                    "forward", f"tcp:{port}",
                    "localabstract:chrome_devtools_remote",
                    serial=serial, timeout=10.0,
                )
                if rc != 0:
                    return empty
                self._cdp_fwd[serial] = port

            async def _cdp() -> dict:
                # Lista target: HTTP minimale su localhost (niente requests).
                # IMPORTANTE: Host DEVE includere la porta — Chrome valida
                # l'header e con 'Host: 127.0.0.1' nudo chiude la connessione
                # senza rispondere (0 byte). Inoltre webSocketDebuggerUrl
                # eredita host:porta dall'Host inviato.
                host = f"127.0.0.1:{port}"
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=3.0
                )
                try:
                    writer.write(
                        f"GET /json HTTP/1.1\r\nHost: {host}\r\n"
                        f"Connection: close\r\n\r\n".encode()
                    )
                    await writer.drain()
                    raw = await asyncio.wait_for(reader.read(65536), timeout=3.0)
                finally:
                    writer.close()
                body = raw.split(b"\r\n\r\n", 1)
                if len(body) < 2:
                    return empty
                targets = json.loads(body[1].decode("utf-8", errors="replace"))
                # TUTTE le pagine web reali (skip chrome:// e about:blank):
                # con piu' tab aperte il primo target non e' quello visibile
                # — leggere la tab sbagliata attribuiva saldi al book di
                # un'altra pagina. Si valuta ogni tab e si preferisce
                # quella con visibilityState 'visible' (tab attiva).
                pages = [
                    t for t in targets
                    if t.get("type") == "page"
                    and t.get("url", "").startswith("http")
                    and t.get("webSocketDebuggerUrl")
                ]
                if not pages:
                    return empty

                async def _eval_page(page: dict):
                    # webSocketDebuggerUrl replica l'host della richiesta:
                    # lo forziamo comunque alla porta del forward — se Chrome
                    # risponde con localhost:9222 o un host suo, il ws
                    # punterebbe nel vuoto e la lettura fallirebbe zitta.
                    ws_url = page["webSocketDebuggerUrl"]
                    ws_url = re.sub(
                        r"^ws://[^/]+", f"ws://127.0.0.1:{port}", ws_url
                    )
                    async with websockets.connect(
                        ws_url,
                        open_timeout=3, close_timeout=1, max_size=2**20,
                    ) as ws:
                        await ws.send(json.dumps({
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": self._CDP_JS,
                                "returnByValue": True,
                            },
                        }))
                        while True:
                            msg = json.loads(
                                await asyncio.wait_for(ws.recv(), timeout=5.0)
                            )
                            if msg.get("id") != 1:
                                continue
                            return (
                                msg.get("result", {})
                                .get("result", {})
                                .get("value")
                            )

                # Valuta ogni tab e scegli la migliore: la tab attiva
                # (visibilityState='visible') con saldo, poi attiva senza
                # saldo, poi la prima con saldo, poi la prima valida.
                # Con decine di tab aperte la valutazione in serie sfora
                # il budget complessivo e la lettura cadeva SEMPRE:
                # valutiamo in parallelo (ogni pagina ha gia' i suoi
                # timeout WS), dando priorita' alle tab su domini di
                # bookmaker noti — sono loro a contenere i saldi.
                def _is_book(t: dict) -> bool:
                    try:
                        host = urlparse(t.get("url", "")).hostname or ""
                    except Exception:
                        return False
                    return bool(self._bookmaker_from_package(host))

                pages.sort(key=lambda t: 0 if _is_book(t) else 1)
                eval_sem = asyncio.Semaphore(6)

                async def _eval_sem(page: dict):
                    async with eval_sem:
                        return await _eval_page(page)

                evals = await asyncio.gather(
                    *(_eval_sem(p) for p in pages), return_exceptions=True
                )
                candidates = []
                for val in evals:
                    if isinstance(val, Exception):
                        continue
                    if not isinstance(val, dict) or not val.get("site"):
                        continue
                    saldo = None
                    saldo_val = val.get("saldo")
                    if saldo_val:
                        num = re.search(
                            r"[0-9]+(?:[.,][0-9]+)*[.,][0-9]{1,2}\b",
                            str(saldo_val),
                        )
                        saldo = (
                            self._normalize_amount(num.group(0)) if num else None
                        )
                    site = (val.get("site") or "").replace("www.", "")
                    user = _clean_username(val.get("user") or "")
                    candidates.append({
                        "saldo": saldo,
                        "bookmaker": (
                            self._bookmaker_from_package(site) if site else ""
                        ),
                        "username": user,
                        "_vis": val.get("vis") == "visible",
                        # Pagina non loggata: l'importo nel DOM e' promo
                        # (es. '2.000€ bonus'), non il saldo reale.
                        # 'lo' = CTA Accedi/Registrati visibile nel DOM:
                        # scarta la pagina solo se non ha un saldo valido
                        # (una promo 'Registrati' puo' convivere col saldo).
                        "_logged_out": bool(_LOGGED_OUT_RE.search(user))
                        or (val.get("lo") and not saldo),
                    })
                if not candidates:
                    return empty
                # Le pagine non loggate non possono mostrare un saldo reale:
                # fuori dalla scelta e dai saldi per-book.
                valid = [c for c in candidates if not c["_logged_out"]]
                if not valid:
                    return empty
                best = (
                    next((c for c in valid if c["_vis"] and c["saldo"]), None)
                    or next((c for c in valid if c["_vis"]), None)
                    or next((c for c in valid if c["saldo"]), None)
                    or valid[0]
                )
                best.pop("_vis", None)
                best.pop("_logged_out", None)
                # Ogni tab su un bookmaker noto aggiorna la sua cella della
                # matrice: non solo la tab visibile. Senza doppioni per book.
                books = []
                seen_bm = set()
                for c in valid:
                    bm = c["bookmaker"]
                    if bm and c["saldo"] and bm not in seen_bm:
                        seen_bm.add(bm)
                        books.append({
                            "bookmaker": bm,
                            "saldo": c["saldo"],
                            "username": c["username"],
                        })
                best["books"] = books
                return best

            result = await asyncio.wait_for(_cdp(), timeout=12.0)
            if result.get("saldo"):
                logs.info(
                    f"Saldo via CDP/DOM: {result['saldo']} ({result['bookmaker']})",
                    serial=serial,
                )
            return result
        except Exception:
            # Chrome non attivo / CDP irraggiungibile / forward morto:
            # dimentica il forward persistente e rimuovilo — alla prossima
            # lettura ne viene creato uno pulito.
            if port:
                self._cdp_fwd.pop(serial, None)
                try:
                    await self.adb_command(
                        "forward", "--remove", f"tcp:{port}",
                        serial=serial, timeout=5.0,
                    )
                except Exception:
                    pass
            return empty

    # ------------------------------------------------------------------
    # Etichette
    # ------------------------------------------------------------------

    def set_label(self, serial: str, label: str) -> None:
        self._labels[serial] = label
        if serial in self._devices:
            self._devices[serial].label = label
        save_labels(self._labels)
        logs.info(f"Etichetta '{label}' assegnata a {serial}", serial=serial)

    def set_label_color(self, serial: str, color: str) -> None:
        if color:
            self._label_colors[serial] = color
        else:
            self._label_colors.pop(serial, None)
        if serial in self._devices:
            self._devices[serial].label_color = color
        save_label_colors(self._label_colors)
        logs.info(f"Colore etichetta '{color}' assegnato a {serial}", serial=serial)

    def set_order(self, serial: str, order: int) -> None:
        if order:
            self._order[serial] = order
        else:
            self._order.pop(serial, None)
        if serial in self._devices:
            self._devices[serial].order = order
        save_device_order(self._order)
        logs.info(f"Ordine {order} assegnato a {serial}", serial=serial)

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
        # Card persistenti (come il registro di Panda): ogni device mai
        # visto resta in griglia marcato 'non rilevato' finche' non torna.
        self._drop_invalid_serials()
        for serial, k in self._known.items():
            if serial in self._devices:
                continue
            dev = DeviceState(
                info=DeviceInfo(
                    serial=serial,
                    model=k.get("model", ""),
                    product=k.get("product", ""),
                    transport_id=k.get("transport_id", ""),
                    usb_port=k.get("usb_port", ""),
                ),
                label=self._labels.get(serial, k.get("label", "")),
                label_color=self._label_colors.get(serial, k.get("label_color", "")),
                order=self._order.get(serial, k.get("order", 0)),
                tags=self._tags.get(serial, k.get("tags", [])),
                status=DeviceStatus.OFFLINE,
                played=serial in self._played_serials,
                skipped=serial in self._skipped_serials,
            )
            dev.error = "non rilevato"
            self._devices[serial] = dev
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

    def _collect_adb_keys(self) -> List[str]:
        """Tutte le chiavi adb trovate sul sistema.

        Oltre a ~/.android cerchiamo vicino al binario adb in uso, nelle
        cartelle tipiche di tool di terzi (Panda puo' avere una adbkey
        propria) e nelle env var che ridirezionano lo storage delle chiavi.
        """
        keys: List[str] = []

        def _add(d: Optional[Path]) -> None:
            if not d:
                return
            try:
                for name in ("adbkey", "adbkey.pub"):
                    p = d / name
                    if p.exists() and str(p) not in keys:
                        keys.append(str(p))
            except Exception:
                pass

        # 1. Posizioni standard
        _add(Path.home() / ".android")
        for var in ("ANDROID_SDK_HOME", "ANDROID_USER_HOME"):
            v = os.environ.get(var)
            if v:
                _add(Path(v) / ".android")
                _add(Path(v))
        # Chiavi gia' indicate da altri tool via env
        v = os.environ.get("ADB_VENDOR_KEYS")
        if v:
            for k in v.split(";" if os.name == "nt" else ":"):
                if k and Path(k).exists() and k not in keys:
                    keys.append(k)
        # 2. Vicino al binario adb in uso (es. tools/ di Panda)
        if self._adb:
            adb_dir = Path(self._adb).parent
            _add(adb_dir)
            _add(adb_dir / ".android")
            _add(adb_dir.parent / ".android")
            _add(adb_dir.parent)
        # 3. Glob depth-limitato: home e cartelle app — copre tool di
        #    terzi che tengono la chiave in una sottodir propria
        for base in (
            Path.home(),
            Path(os.environ.get("LOCALAPPDATA", "")),
            Path(os.environ.get("APPDATA", "")),
        ):
            try:
                if not base.exists():
                    continue
                for p in base.glob("*/*/adbkey*"):
                    if p.is_file() and str(p) not in keys:
                        keys.append(str(p))
                for p in base.glob("*/adbkey*"):
                    if p.is_file() and str(p) not in keys:
                        keys.append(str(p))
            except Exception:
                continue
        return keys

    def _adb_env(self) -> Dict[str, str]:
        """Env per i subprocess adb: ADB_VENDOR_KEYS con tutte le chiavi."""
        # Il server adb le carica solo al suo avvio: per questo quando
        # compaiono device 'unauthorized' facciamo un kill-server una tantum
        # cosi' il prossimo comando riparte con l'env completo.
        now = time.monotonic()
        if self._adb_env_cache and now - self._adb_env_cache[0] < 60.0:
            return self._adb_env_cache[1]
        env = dict(os.environ)
        keys = self._collect_adb_keys()
        if keys:
            sep = ";" if os.name == "nt" else ":"
            env["ADB_VENDOR_KEYS"] = sep.join(keys)
        self._adb_env_cache = (now, env)
        return env

    def _adb_ports(self) -> List[int]:
        """Porte dei server adb da interrogare: 5037 + extra da settings."""
        ports = [5037]
        for p in (self._settings.adb_extra_ports or "").split(","):
            p = p.strip()
            if p.isdigit() and int(p) not in ports:
                ports.append(int(p))
        return ports

    async def adb_command(
        self, *args: str, serial: Optional[str] = None,
        timeout: float = 30.0, port: Optional[int] = None,
        lock_timeout: Optional[float] = None,
        priority: str = "shell",
    ) -> Tuple[int, str, str]:
        """Esegue un comando ADB e ritorna (returncode, stdout, stderr).

        lock_timeout: se specificato, attende al massimo quel tempo per
        acquisire il lock ADB globale. Per gli input e i tap serve un
        valore breve, altrimenti un click resta bloccato dietro un bulk
        shell di 25 device per decine di secondi.
        """
        lock = get_adb_cmd_lock(priority)
        if lock_timeout is None:
            await lock.acquire()
        else:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=lock_timeout)
            except asyncio.TimeoutError:
                return -1, "", "adb lock busy"
        try:
            cmd = [self._adb]
            # Instradamento multi-server: il device va comandato sul server
            # che lo enumera (5037 standard, 5038 QuickForward/Panda).
            eff_port = port
            if eff_port is None and serial:
                eff_port = _SERIAL_PORT.get(serial, 5037)
            if eff_port and eff_port != 5037:
                # Porta di un server di terzi: il NOSTRO client di versione
                # diversa ucciderebbe il loro server a ogni comando e
                # auto-avvierebbe un clone che ruba i device USB. Solo il
                # binario proprietario puo' parlare con quel server; se non
                # lo troviamo il comando fallisce invece di fare danni.
                if not _adb_port_listening(eff_port):
                    return -1, "", f"server adb :{eff_port} non in ascolto"
                foreign = _foreign_adb_path(self._adb)
                if not foreign:
                    return -1, "", f"binario adb del server :{eff_port} non trovato"
                cmd = [foreign, "-P", str(eff_port)]
            if serial:
                cmd += ["-s", serial]
            cmd += list(args)
            try:
                rc, stdout, stderr = await run_proc(
                    cmd, timeout, env=self._adb_env()
                )
                out_str = stdout.decode("utf-8", errors="replace")
                err_str = stderr.decode("utf-8", errors="replace")
                # Se un dispositivo specifico sparisce da ADB, sincronizziamo subito lo stato.
                # Solo errori adb di trasporto: l'output della shell (es. "sh: cmd: not found")
                # non deve marcare il device offline.
                if serial and serial in self._devices:
                    combined = (out_str + err_str).lower()
                    if (
                        f"device '{serial.lower()}' not found" in combined
                        or f"device {serial.lower()} not found" in combined
                        or "device offline" in combined
                        or "no devices/emulators found" in combined
                    ):
                        dev = self._devices[serial]
                        # Non retrocedere un device gia' OFFLINE: il reconnect
                        # periodico su seriali assenti fallisce "not found" e
                        # riarmerebbe il log "scomparso" a ogni poll.
                        if dev.status not in (
                            DeviceStatus.DISCONNECTED,
                            DeviceStatus.OFFLINE,
                        ):
                            dev.status = DeviceStatus.DISCONNECTED
                            dev.streaming = False
                            logs.warn("Dispositivo segnato offline da ADB", serial=serial)
                return (rc, out_str, err_str)
            except asyncio.TimeoutError:
                logs.warn(f"Timeout comando ADB: {' '.join(cmd)}")
                return -1, "", "timeout"
            except Exception as exc:
                logs.error(f"Errore ADB: {exc}", throttle_s=30)
                return -1, "", str(exc)
        finally:
            lock.release()

    async def shell(
        self, serial: str, command: str,
        timeout: float = 30.0, lock_timeout: Optional[float] = None,
        priority: str = "shell",
    ) -> str:
        """Esegue un comando shell su un dispositivo specifico."""
        rc, out, err = await self.adb_command(
            "shell", command, serial=serial, timeout=timeout,
            lock_timeout=lock_timeout, priority=priority,
        )
        return out.strip()

    # ------------------------------------------------------------------
    # Discovery loop
    # ------------------------------------------------------------------

    async def _track_loop(self) -> None:
        """DEPRECATED: il discovery e' gestito con `_poll_loop` per maggiore robustezza."""
        logs.warn("_track_loop non utilizzato, si usa _poll_loop")
        while self._running:
            await asyncio.sleep(3600)

    async def _poll_loop(self) -> None:
        # Contatore di poll consecutivi falliti (nessuna porta adb ha
        # risposto): alimenta il backoff esponenziale per non martellare
        # adb quando e' del tutto irraggiungibile (es. WinError 5
        # persistente sul binario altrui). Si resetta al primo poll ok.
        consecutive_failures = 0
        while self._running:
            try:
                await self._refresh_devices()
            except Exception as exc:
                logs.error(f"Errore nel polling ADB: {exc}", throttle_s=30)
                self._last_poll_ok = False
            if self._last_poll_ok:
                consecutive_failures = 0
                delay = self._settings.poll_interval_s
            else:
                consecutive_failures += 1
                # Backoff esponenziale: 5s, 10s, 20s, 40s, cap 60s.
                # Senza questo, un adb bloccato genera decine di errori
                # al minuto a tempo indeterminato.
                delay = min(
                    self._settings.poll_interval_s * (2 ** consecutive_failures),
                    60.0,
                )
                if consecutive_failures == 1:
                    logs.warn(
                        "ADB non risponde: backoff del polling "
                        f"(prossimo tentativo tra {delay:.0f}s)",
                        throttle_s=60,
                    )
            await asyncio.sleep(delay)

    def _upsert_device(
        self,
        serial: str,
        state_str: str,
        *,
        model: str = "",
        product: str = "",
        usb: str = "",
        tid: str = "",
        port: int = 5037,
    ) -> None:
        """Aggiorna o crea lo stato di un dispositivo da un rigo adb devices."""
        status = {
            "device": DeviceStatus.ONLINE,
            "offline": DeviceStatus.OFFLINE,
            "unauthorized": DeviceStatus.UNAUTHORIZED,
        }.get(state_str, DeviceStatus.OFFLINE)

        now = time.time()
        _SERIAL_PORT[serial] = port
        if serial in self._known:
            self._known[serial]["last_seen"] = now
        if serial in self._devices:
            dev = self._devices[serial]
            dev.adb_port = port
            old_status = dev.status
            # aggiorna le info senza perdere quelle gia' presenti
            dev.info.model = (model or dev.info.model).replace("_", " ")
            dev.info.product = product or dev.info.product
            dev.info.transport_id = tid or dev.info.transport_id
            dev.info.usb_port = usb or dev.info.usb_port
            dev.status = status
            dev.played = serial in self._played_serials
            dev.skipped = serial in self._skipped_serials
            dev.last_seen = now
            dev.error = ""
            if old_status != status:
                logs.info(
                    f"Stato cambiato: {old_status.value} -> {status.value}",
                    serial=serial,
                )
                if status == DeviceStatus.ONLINE:
                    dev.stream_failures = 0
                    dev.next_stream_attempt = 0.0
                    # Auto-lettura saldo in background: CDP-only, non
                    # blocca il device. Se Chrome e' in foreground il
                    # saldo si aggiorna entro ~5s dal login.
                    self._schedule_balance_read(serial)
        else:
            info = DeviceInfo(
                serial=serial,
                model=(model or "").replace("_", " "),
                product=product or "",
                transport_id=tid or "",
                usb_port=usb or "",
            )
            label = self._labels.get(serial, "")
            label_color = self._label_colors.get(serial, "")
            order = self._order.get(serial, 0)
            tags = self._tags.get(serial, [])
            dev = DeviceState(
                info=info,
                label=label,
                label_color=label_color,
                order=order,
                tags=tags,
                status=status,
                played=serial in self._played_serials,
                skipped=serial in self._skipped_serials,
                adb_port=port,
            )
            self._devices[serial] = dev
            # Registra il dispositivo nel file persistente
            self._known[serial] = {
                "model": dev.info.model,
                "product": dev.info.product,
                "transport_id": dev.info.transport_id,
                "usb_port": dev.info.usb_port,
                "label": dev.label,
                "tags": dev.tags,
                "last_seen": dev.last_seen,
            }
            try:
                save_known(self._known)
            except Exception as exc:
                logs.warn(f"Salvataggio known fallito: {exc}", throttle_s=60)
            logs.success(f"Nuovo dispositivo rilevato: {dev.display_name}", serial=serial)
            if status == DeviceStatus.ONLINE:
                self._schedule_balance_read(serial)

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
        # Rilevazione: alterna `adb devices` e `adb devices -l` perche' con
        # molti dispositivi uno puo' riuscire dove l'altro tronca.
        seen_serials: set = set()
        # Porte il cui poll e' fallito del tutto (timeout per adb saturo,
        # es. durante la lettura saldi): un poll fallito NON e' prova che
        # i device siano spariti — saltiamo il conteggio missing per loro.
        ports_failed: set = set()
        # True se almeno una porta ha risposto rc==0: alimenta il backoff
        # del _poll_loop quando adb e' del tutto irraggiungibile.
        any_port_ok = False
        # Multi-server: oltre alla 5037 interroghiamo le porte extra
        # (es. 5038 = QuickForward/Panda che ri-esporta i device come adb).
        for port in self._adb_ports():
            # MAI interrogare una porta extra senza server in ascolto:
            # `adb -P <porta> devices` auto-avvia un daemon se la porta e'
            # libera, e quel clone contenderebbe i device USB al 5037.
            if port != 5037 and not _adb_port_listening(port):
                continue
            if port != 5037:
                # Porta esterna (Panda/QuickForward): SOLO socket grezzo col
                # protocollo smart-host. Il binario adb farebbe il check di
                # versione, ucciderebbe il loro server e auto-avvierebbe un
                # clone nostro sulla porta — la guerra di riavvii che faceva
                # rimbalzare i device. Nessun subprocess, nessun lock.
                out = None
                for req in ("host:devices-l", "host:devices"):
                    try:
                        out = await asyncio.to_thread(
                            _adb_host_query, port, req
                        )
                    except Exception:
                        out = None
                    if out is not None:
                        break
                if out is None:
                    ports_failed.add(port)
                    continue
                any_port_ok = True
                for match in _DEVICE_RE.finditer(out):
                    serial = match.group("serial")
                    if serial == "List" or not _is_valid_serial(serial):
                        continue
                    if serial not in seen_serials:
                        seen_serials.add(serial)
                        self._upsert_device(
                            serial,
                            match.group("state"),
                            model=match.group("model") or "",
                            product=match.group("product") or "",
                            usb=match.group("usb") or "",
                            tid=match.group("tid") or "",
                            port=port,
                        )
                continue
            poll_ok = False
            # Ogni tentativo e' un comando adb sotto lock globale: farne 5
            # fissi a ogni ciclo satura adb e fa singhiozzare stream e touch.
            # Ci fermiamo appena una coppia plain + '-l' non porta seriali
            # nuovi, mantenendo la ridondanza solo quando serve davvero.
            stable_rounds = 0
            for attempt in range(5):
                use_long = attempt % 2 == 1  # 1, 3 con -l
                before = len(seen_serials)
                rc, out, _ = await self.adb_command(
                    "devices",
                    *("-l",) if use_long else (),
                    timeout=15.0,
                    port=port,
                )
                if rc == 0 and out:
                    poll_ok = True
                    any_port_ok = True
                    regex = _DEVICE_RE if use_long else _DEVICE_RE_PLAIN
                    for match in regex.finditer(out):
                        serial = match.group("serial")
                        if serial == "List" or not _is_valid_serial(serial):
                            continue
                        if serial not in seen_serials:
                            seen_serials.add(serial)
                            model = match.group("model") if use_long else ""
                            product = match.group("product") if use_long else ""
                            usb = match.group("usb") if use_long else ""
                            tid = match.group("tid") if use_long else ""
                            self._upsert_device(
                                serial,
                                match.group("state"),
                                model=model,
                                product=product,
                                usb=usb,
                                tid=tid,
                                port=port,
                            )
                if poll_ok and len(seen_serials) == before and before > 0:
                    stable_rounds += 1
                    # Una lettura plain e una '-l' concordi: elenco completo.
                    if stable_rounds >= 2:
                        break
                else:
                    stable_rounds = 0
                if attempt < 4:
                    await asyncio.sleep(0.2)
            if not poll_ok:
                ports_failed.add(port)

        # Aggiorna il flag per il backoff del _poll_loop: se nessuna porta
        # ha risposto (adb irraggiungibile / WinError 5 su tutti i binari),
        # il poll successivo verra' ritardato progressivamente.
        self._last_poll_ok = any_port_ok

        # Device visti prima ma assenti ora: senza questo restavano
        # "online" all'infinito (card fantasma — il log mostrava Focus su
        # seriali che adb non elencava piu'). Dopo 2 poll senza vederli
        # li marchiamo offline, come Panda che li mostra disconnessi.
        reconnect_due = []
        for serial, dev in self._devices.items():
            if serial in seen_serials:
                self._missing.pop(serial, None)
                continue
            # Poll della sua porta fallito (adb saturo): non e' una
            # scomparsa, salta il conteggio — altrimenti durante la lettura
            # saldi marchiamo offline mezzo farm e spariamo 'adb reconnect'
            # in raffica, uccidendo gli stream scrcpy.
            if getattr(dev, "adb_port", _SERIAL_PORT.get(serial, 5037)) in ports_failed:
                continue
            misses = self._missing.get(serial, 0) + 1
            self._missing[serial] = misses
            if misses >= 2 and dev.status != DeviceStatus.OFFLINE:
                dev.status = DeviceStatus.OFFLINE
                dev.streaming = False
                dev.error = "non rilevato"
                logs.warn("Device scomparso da adb devices", serial=serial)
            elif misses == 10:
                # Sparito da ~5 minuti: se torna solo col riavvio del PC e'
                # la sospensione selettiva USB di Windows, non adb.
                logs.warn(
                    "Device assente da 10 poll: se torna solo riavviando il PC, "
                    "disattiva la sospensione selettiva USB di Windows",
                    serial=serial,
                )
            # Recovery attivo, ma SOLO nei primi poll dopo la scomparsa.
            # Un device staccato fisicamente non torna con 'adb reconnect':
            # insistere all'infinito faceva ri-negoziare i transport del
            # server adb, e a ogni giro cadevano i forward di TUTTI gli
            # altri device (stream chiusi a grappolo nello stesso secondo).
            # Oltre questa soglia lo lasciamo offline, come fa Panda.
            if 2 <= misses <= _RECONNECT_MAX_MISSES and misses % 3 == 0:
                reconnect_due.append(serial)

        if reconnect_due:
            # Il lock adb globale serializza comunque questi comandi: con
            # molti device assenti si accumulavano decine di secondi di adb
            # bloccato, e nel frattempo i forward degli stream vivi cadevano.
            # Ne facciamo pochi per ciclo, con timeout corto.
            await asyncio.gather(
                *(
                    self.adb_command("reconnect", serial=s, timeout=5.0)
                    for s in reconnect_due[:2]
                ),
                return_exceptions=True,
            )

        # Calo improvviso: tipico di un altro adb.exe (Panda, scrcpy,
        # altro GridDroid) che uccide il server per versione diversa.
        prev = getattr(self, "_last_seen_count", 0)
        # Se qualche porta non ha risposto il conteggio e' incompleto:
        # non e' un calo reale, salta il check (e lo switch di binario).
        if not ports_failed and prev - len(seen_serials) >= 3:
            logs.warn(
                f"Calo improvviso device ({prev} -> {len(seen_serials)}): "
                "possibile conflitto con un altro adb.exe che riavvia il server",
                throttle_s=60,
            )
            # Se un altro adb.exe e' attivo (es. Panda partito dopo di noi),
            # passiamo al suo binario: stesso server 5037, niente piu' kill
            # incrociati che fanno sparire i device a intermittenza.
            # Solo se e' effettivamente lanciabile da noi: se l'altra app ne
            # mantiene un lock esclusivo, adottarlo produrrebbe un flood di
            # [WinError 5] Accesso negato a ogni comando.
            other = _find_running_adb(exclude=self._adb)
            if other and _adb_executable_works(other):
                logs.info(
                    f"Rilevato adb di terzi attivo: passo a {other} "
                    "(condivide lo stesso server, fine del flapping)"
                )
                self._adb = other
        self._last_seen_count = len(seen_serials)

        # Breakdown per stato: aiuta a capire perche' mancano device
        # (es. 30 collegati ma ADB ne elenca 13, di cui 2 unauthorized).
        stati = {}
        for serial in seen_serials:
            dev = self._devices.get(serial)
            stato = dev.status.value if dev else "?"
            stati[stato] = stati.get(stato, 0) + 1
        dettaglio = ", ".join(f"{k}: {v}" for k, v in sorted(stati.items()))
        logs.info(
            f"Dispositivi ADB rilevati: {len(seen_serials)} ({dettaglio})",
            throttle_s=30,
        )

        # Aggiungi dispositivi gia' visti in passato, ora assenti
        for serial, k in self._known.items():
            if serial not in seen_serials:
                if serial not in self._devices:
                    info = DeviceInfo(
                        serial=serial,
                        model=(k.get("model") or "").replace("_", " "),
                        product=k.get("product") or "",
                        transport_id=k.get("transport_id") or "",
                        usb_port=k.get("usb_port") or "",
                    )
                    label = self._labels.get(serial, "")
                    tags = self._tags.get(serial, [])
                    dev = DeviceState(
                        info=info,
                        label=label,
                        tags=tags,
                        status=DeviceStatus.DISCONNECTED,
                        played=serial in self._played_serials,
                        last_seen=k.get("last_seen") or 0,
                    )
                    dev.error = "non collegato"
                    self._devices[serial] = dev
                else:
                    dev = self._devices[serial]
                    if dev.status == DeviceStatus.ONLINE:
                        dev.streaming = False
                    # Non retrocedere OFFLINE -> DISCONNECTED: il missing-block
                    # sopra ha gia' loggato "scomparso", e il flip-flop lo
                    # riarmerebbe a ogni poll.
                    if dev.status not in (
                        DeviceStatus.DISCONNECTED,
                        DeviceStatus.OFFLINE,
                    ):
                        dev.status = DeviceStatus.DISCONNECTED
                        dev.error = "non collegato"

        # Segna come disconnessi i dispositivi online scomparsi
        for serial, dev in self._devices.items():
            if serial not in seen_serials and dev.status == DeviceStatus.ONLINE:
                dev.status = DeviceStatus.DISCONNECTED
                dev.streaming = False
                dev.error = "non collegato"
                logs.warn(f"Dispositivo disconnesso", serial=serial, throttle_s=30)

        # Recovery automatico: i device rimasti offline/unauthorized dopo un
        # riavvio del daemon restano bloccati finche' non si cambia modalita'
        # USB sul telefono (carica -> PTP). `adb reconnect` forza la
        # rinegoziazione del transport senza toccare il telefono.
        now = time.time()
        for serial in seen_serials:
            dev = self._devices.get(serial)
            # Solo OFFLINE: su 'unauthorized' il reconnect non serve (non e'
            # un problema di transport) e ogni tentativo forza una
            # rinegoziazione USB che amplifica il flap.
            if not dev or dev.status != DeviceStatus.OFFLINE:
                continue
            if now - self._last_reconnect.get(serial, 0.0) < 20.0:
                continue
            self._last_reconnect[serial] = now
            asyncio.ensure_future(self._try_reconnect(serial))

        # Device 'unauthorized': il server adb sta girando con una chiave
        # che i telefoni non hanno autorizzato (es. dopo il passaggio al
        # binario di Panda). kill-server -> il prossimo comando riparte con
        # ADB_VENDOR_KEYS e carica anche le chiavi di terzi. Si riprova se
        # nel frattempo sono state trovate chiavi nuove (max 3 volte).
        unauth = [
            s for s in seen_serials
            if self._devices.get(s)
            and self._devices[s].status == DeviceStatus.UNAUTHORIZED
        ]
        if unauth and self._key_reload_attempts < 3:
            keys_now = self._collect_adb_keys()
            if set(keys_now) != self._keys_loaded:
                # kill-server ammazza TUTTI i tunnel forward: se ci sono
                # stream attivi e' una strage (1 device unauthorized ->
                # 20 stream morti, WinError 64 a raffica). Il reload si fa
                # solo a farm fermo; con stream attivi il device resta
                # unauthorized finche' l'utente non accetta il prompt RSA
                # sul telefono o riavvia adb a mano.
                streaming_now = any(
                    d.streaming for d in self._devices.values()
                )
                if streaming_now:
                    logs.warn(
                        f"{len(unauth)} device unauthorized ma ci sono stream "
                        f"attivi: niente riavvio server adb. Autorizza sul "
                        f"telefono o usa 'Riavvia ADB'.",
                        throttle_s=60,
                    )
                else:
                    self._key_reload_attempts += 1
                    self._keys_loaded = set(keys_now)
                    logs.warn(
                        f"{len(unauth)} device unauthorized: riavvio il server adb "
                        f"con {len(keys_now)} chiavi ({keys_now})"
                    )
                    asyncio.ensure_future(self._reload_server_keys())

        # Lettura saldi periodica: non solo alla transizione ONLINE — se
        # l'utente naviga verso una pagina saldi mentre il device resta
        # online, il valore deve aggiornarsi da solo. Il throttle interno
        # (30s per device) mantiene il carico basso; solo CDP, mai
        # uiautomator (congela la UI del telefono).
        for serial in seen_serials:
            dev = self._devices.get(serial)
            if dev and dev.status == DeviceStatus.ONLINE:
                self._schedule_balance_read(serial)

    async def _reload_server_keys(self) -> None:
        """kill-server + start-server con ADB_VENDOR_KEYS: il nuovo server
        carica tutte le chiavi trovate, i device 'unauthorized' che avevano
        autorizzato una di quelle chiavi tornano 'device'."""
        await self.adb_command("kill-server", timeout=10.0)
        await asyncio.sleep(1.0)
        rc, out, err = await self.adb_command("start-server", timeout=15.0)
        logs.info(
            f"Server adb riavviato con chiavi vendor (rc={rc})",
        )

    async def _try_reconnect(self, serial: str) -> None:
        """Tenta `adb reconnect` su un device bloccato offline/unauthorized."""
        rc, out, err = await self.adb_command(
            "reconnect", serial=serial, timeout=10.0
        )
        msg = (out or err).strip()
        logs.info(f"adb reconnect: {msg or 'nessuna risposta'}", serial=serial)


    # ------------------------------------------------------------------
    # Comandi utili
    # ------------------------------------------------------------------

    def set_streams(self, streams) -> None:
        """Inietta il StreamManager (chiamato da app.py dopo il wiring)."""
        self._streams = streams

    async def _display_power(self, serial: str, on: bool) -> bool:
        """Accende/spegne il pannello via canale scrcpy: schermo buio ma
        device sbloccato e stream video sempre attivo. False se non c'e'
        uno stream con canale di controllo attivo su cui inviarlo."""
        streams = self._streams
        if streams is None:
            return False
        stream = streams.get_stream(serial)
        if stream is None:
            return False
        return await stream.set_display_power(on)

    async def screen_on(self, serial: str) -> None:
        SCREEN_OFF_REQUESTED.discard(serial)
        # Riaccende il pannello se era in display-off; il WAKEUP copre il
        # caso di device messo in standby dal fallback senza stream.
        await self._display_power(serial, True)
        await self.shell(serial, "input keyevent KEYCODE_WAKEUP")
        if serial in self._devices:
            self._devices[serial].screen_on = True
        logs.info("Schermo acceso", serial=serial)

    async def _is_screen_on(self, serial: str) -> Optional[bool]:
        """Stato reale del display, None se non determinabile.

        Controlla sia il wakefulness che lo stato del pannello: con il
        display spento via set_display_power il device resta 'Awake' ma il
        pannello e' OFF — guardare solo il wakefulness sbaglierebbe.
        """
        try:
            out = await self.shell(
                serial,
                "dumpsys power | grep -E 'mWakefulness=|Display Power: state='",
                timeout=10.0,
            )
        except Exception:
            return None
        wake = re.search(r"mWakefulness=(\w+)", out)
        disp = re.search(r"Display Power:\s*state=(\w+)", out)
        if not wake and not disp:
            return None
        if wake and wake.group(1) != "Awake":
            return False
        if disp and disp.group(1) == "OFF":
            return False
        return True

    async def screen_off(self, serial: str) -> bool:
        """Spegne lo schermo. True = eseguito (display-off via scrcpy o
        sleep verificato); False = non riuscito. Il display-off via
        SurfaceControl NON e' visibile in dumpsys (mScreenState resta ON):
        chi verifica deve fidarsi di questo valore di ritorno."""
        # Registriamo l'intenzione PRIMA di spegnere: se nel frattempo lo
        # stream si riavvia, il suo KEYCODE_WAKEUP viene saltato invece di
        # riaccendere il device appena bloccato.
        SCREEN_OFF_REQUESTED.add(serial)
        # Via scrcpy (MOD+O): spegne solo la retroilluminazione — il device
        # NON si blocca e da PC si continua a vedere lo schermo.
        if await self._display_power(serial, False):
            if serial in self._devices:
                self._devices[serial].screen_on = False
            logs.info("Schermo spento (display off, device sbloccato)", serial=serial)
            return True
        # Senza stream attivo non c'e' canale scrcpy: l'unico comando adb
        # e' il tasto sleep, che pero' porta anche al lockscreen.
        await self.shell(serial, "input keyevent KEYCODE_SLEEP")
        # Il keyevent puo' andare perso se il device e' occupato: verifichiamo
        # l'esito e ritentiamo una volta invece di dichiarare successo al buio.
        await asyncio.sleep(0.4)
        if await self._is_screen_on(serial):
            await self.shell(serial, "input keyevent KEYCODE_SLEEP")
            await asyncio.sleep(0.4)
            if await self._is_screen_on(serial):
                logs.warn("Blocco schermo non riuscito", serial=serial)
                return False
        if serial in self._devices:
            self._devices[serial].screen_on = False
        logs.info("Schermo spento (con blocco — nessuno stream attivo)", serial=serial)
        return True

    async def reboot(self, serial: str) -> None:
        logs.info("Riavvio in corso...", serial=serial)
        await self.adb_command("reboot", serial=serial)

    async def restart_adb_server(self) -> bool:
        """Riavvia il daemon ADB: kill + start per forzare re-enumerazione USB.

        Con molti device su hub USB la re-enumerazione puo' richiedere decine
        di secondi: dopo lo start attendiamo attivamente che `adb devices`
        torni a vedere i device, con un secondo tentativo se la prima
        enumerazione resta a zero (es. porta 5037 ancora occupata).
        """
        logs.warn("Riavvio daemon ADB richiesto dall'utente")
        try:
            for attempt in range(2):
                await self.adb_command("kill-server", timeout=10.0)
                await asyncio.sleep(1.5)
                rc, _, err = await self.adb_command("start-server", timeout=15.0)
                if rc != 0:
                    logs.error(f"Start ADB fallito: {err}")
                    if attempt == 0:
                        continue
                    return False

                logs.success("Daemon ADB riavviato, attendo i dispositivi...")
                # Attesa attiva: `adb devices` finche' non torna almeno un device
                for i in range(40):
                    await asyncio.sleep(1.0)
                    rc, out, _ = await self.adb_command("devices", timeout=10.0)
                    count = len(_DEVICE_RE_PLAIN.findall(out)) if rc == 0 else 0
                    if count > 0:
                        break
                    if i % 10 == 9:
                        logs.info(f"Ancora nessun dispositivo ({i + 1}s)...")

                if count == 0:
                    logs.warn("Nessun dispositivo dopo il riavvio, riprovo...")
                    continue

                break

            await self._refresh_devices()
            online = sum(
                1 for d in self._devices.values()
                if d.status == DeviceStatus.ONLINE
            )
            if online:
                logs.success(f"Re-enumerazione completata: {online} dispositivi online")
            else:
                logs.error("Nessun dispositivo rilevato dopo il riavvio ADB")
            return online > 0
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
        cmd = [self._adb, *adb_server_args(serial),
               "-s", serial, "exec-out", "screencap", "-p"]
        try:
            rc, stdout, _ = await run_proc(cmd, 15.0)
            if rc == 0 and stdout:
                return stdout
        except Exception:
            pass
        return None
