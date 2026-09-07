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
)
from .device import DeviceInfo, DeviceState, DeviceStatus
from .log_manager import logs


# Lock globale per serializzare i comandi ADB (piu' stabile su hub USB).
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
        self._skipped_serials: set = set(load_skipped())
        self._known: Dict[str, dict] = load_known()
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._change_callbacks: List = []
        # Auto-clicker per device: serial -> task asyncio
        self._autoclick_tasks: Dict[str, asyncio.Task] = {}
        # Throttle per `adb reconnect` automatico su device bloccati
        self._last_reconnect: Dict[str, float] = {}
        # Contatore poll consecutivi in cui un device non appare in adb devices
        self._missing: Dict[str, int] = {}

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
    ) -> bool:
        """Avvia l'auto-clicker su un device in (x, y) con timing umano."""
        if serial not in self._devices:
            return False
        self.stop_autoclick(serial)
        dev = self._devices[serial]
        dev.autoclick = True
        self._autoclick_tasks[serial] = asyncio.create_task(
            self._autoclick_loop(serial, x, y, max(150, interval_ms), jitter_px)
        )
        logs.info(
            f"Auto-click avviato ({x},{y} ogni ~{interval_ms}ms)", serial=serial
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
        self, serial: str, x: int, y: int, interval_ms: int, jitter_px: int
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
            while True:
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
        "betsson": "Betsson", "netbet": "NetBet", "fantasyteam": "FantasyTeam",
        "betclic": "Betclic", "novibet": "Novibet", "stake": "Stake",
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
        return package

    async def read_account_info(self, serial: str) -> dict:
        """Legge saldo, bookmaker e username visibili a schermo.

        Saldo: prima i nodi con parole chiave (saldo/balance/totale), poi gli
        importi con simbolo di valuta — sempre normalizzati a '1234.56'.
        Bookmaker: dal package dell'app in foreground.
        Username: nodi vicino a 'ciao'/'benvenuto'/'account'/'profilo'.
        """
        info = {"saldo": None, "bookmaker": "", "username": ""}
        # Una sola chiamata adb: dump diretto su stdout (niente file
        # intermedio + cat). Il messaggio "UI hierchary dumped to:" va
        # tolto: l'XML vero sta tra <hierarchy> e </hierarchy>.
        xml = ""
        try:
            out = await self.shell(
                serial, "uiautomator dump /dev/stdout", timeout=15.0
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
                    serial, "uiautomator dump /sdcard/griddroid_ui.xml", timeout=15.0
                )
                xml = await self.shell(
                    serial, "cat /sdcard/griddroid_ui.xml", timeout=15.0
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
        if pkgs:
            # Il package piu' frequente e' quello dell'app a schermo
            pkg = max(set(pkgs), key=pkgs.count)
            info["bookmaker"] = self._bookmaker_from_package(pkg)

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

        return info

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
        # Card persistenti (come il registro di Panda): ogni device mai
        # visto resta in griglia marcato 'non rilevato' finche' non torna.
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

    async def _track_loop(self) -> None:
        """DEPRECATED: il discovery e' gestito con `_poll_loop` per maggiore robustezza."""
        logs.warn("_track_loop non utilizzato, si usa _poll_loop")
        while self._running:
            await asyncio.sleep(3600)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_devices()
            except Exception as exc:
                logs.error(f"Errore nel polling ADB: {exc}", throttle_s=30)
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

        now = time.time()
        if serial in self._known:
            self._known[serial]["last_seen"] = now
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
                skipped=serial in self._skipped_serials,
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
        for attempt in range(5):
            use_long = attempt % 2 == 1  # 1, 3 con -l
            rc, out, _ = await self.adb_command(
                "devices",
                *("-l",) if use_long else (),
                timeout=15.0,
            )
            if rc == 0 and out:
                regex = _DEVICE_RE if use_long else _DEVICE_RE_PLAIN
                for match in regex.finditer(out):
                    serial = match.group("serial")
                    if serial == "List":
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
                        )
            if attempt < 4:
                await asyncio.sleep(0.2)

        # Device visti prima ma assenti ora: senza questo restavano
        # "online" all'infinito (card fantasma — il log mostrava Focus su
        # seriali che adb non elencava piu'). Dopo 2 poll senza vederli
        # li marchiamo offline, come Panda che li mostra disconnessi.
        reconnect_due = []
        for serial, dev in self._devices.items():
            if serial in seen_serials:
                self._missing.pop(serial, None)
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
            # Recovery attivo (come Panda): ogni ~3 poll senza vederlo
            # forziamo 'adb reconnect' per far ri-enumerare il device al
            # server, invece di aspettare che torni da solo.
            if misses >= 2 and misses % 3 == 0:
                reconnect_due.append(serial)

        if reconnect_due:
            await asyncio.gather(
                *(
                    self.adb_command("reconnect", serial=s, timeout=10.0)
                    for s in reconnect_due
                ),
                return_exceptions=True,
            )

        # Calo improvviso: tipico di un altro adb.exe (Panda, scrcpy,
        # altro GridDroid) che uccide il server per versione diversa.
        prev = getattr(self, "_last_seen_count", 0)
        if prev - len(seen_serials) >= 3:
            logs.warn(
                f"Calo improvviso device ({prev} -> {len(seen_serials)}): "
                "possibile conflitto con un altro adb.exe che riavvia il server",
                throttle_s=60,
            )
            # Se un altro adb.exe e' attivo (es. Panda partito dopo di noi),
            # passiamo al suo binario: stesso server 5037, niente piu' kill
            # incrociati che fanno sparire i device a intermittenza.
            other = _find_running_adb(exclude=self._adb)
            if other:
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
                    if dev.status != DeviceStatus.DISCONNECTED:
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
            if not dev or dev.status not in (
                DeviceStatus.OFFLINE,
                DeviceStatus.UNAUTHORIZED,
            ):
                continue
            if now - self._last_reconnect.get(serial, 0.0) < 20.0:
                continue
            self._last_reconnect[serial] = now
            asyncio.ensure_future(self._try_reconnect(serial))

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
