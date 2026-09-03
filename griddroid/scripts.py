"""Libreria di script ADB per il laboratorio di test.

Ogni script dichiara i parametri che richiede e viene eseguito in parallelo
su tutti i dispositivi selezionati. Gli script semplici sono una sequenza di
comandi shell; quelli che richiedono logica (es. sblocco con PIN, che deve
leggere lo stato del keyguard) hanno un handler Python dedicato.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from .adb_manager import AdbManager
from .log_manager import logs

# --------------------------------------------------------------------------
# Keycode Android usati dagli script
# --------------------------------------------------------------------------

KEY_BACK = 4
KEY_HOME = 3
KEY_MENU = 82
KEY_POWER = 26
KEY_ENTER = 66
KEY_WAKEUP = 224
KEY_SLEEP = 223
KEY_APP_SWITCH = 187


@dataclass
class ScriptParam:
    """Parametro richiesto da uno script, mostrato come campo nella UI."""
    name: str
    label: str
    tipo: str = "text"           # text | password | number | select
    default: str = ""
    placeholder: str = ""
    opzioni: List[str] = field(default_factory=list)
    obbligatorio: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "tipo": self.tipo,
            "default": self.default,
            "placeholder": self.placeholder,
            "opzioni": self.opzioni,
            "obbligatorio": self.obbligatorio,
        }


@dataclass
class ScriptResult:
    """Esito dell'esecuzione su un singolo dispositivo."""
    serial: str
    ok: bool
    messaggio: str
    output: str = ""

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "ok": self.ok,
            "messaggio": self.messaggio,
            "output": self.output,
        }


@dataclass
class Script:
    """Definizione di uno script eseguibile."""
    id: str
    nome: str
    descrizione: str
    categoria: str
    icona: str = "⚙"
    parametri: List[ScriptParam] = field(default_factory=list)
    comandi: List[str] = field(default_factory=list)
    handler: Optional[Callable] = None
    pericoloso: bool = False
    timeout: float = 30.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nome": self.nome,
            "descrizione": self.descrizione,
            "categoria": self.categoria,
            "icona": self.icona,
            "parametri": [p.to_dict() for p in self.parametri],
            "pericoloso": self.pericoloso,
        }


class ScriptEngine:
    """Registro ed esecutore degli script ADB."""

    def __init__(self, adb: AdbManager) -> None:
        self._adb = adb
        self._scripts: Dict[str, Script] = {}
        self._register_all()

    # ------------------------------------------------------------------
    # API pubblica
    # ------------------------------------------------------------------

    @property
    def scripts(self) -> List[Script]:
        return list(self._scripts.values())

    def get(self, script_id: str) -> Optional[Script]:
        return self._scripts.get(script_id)

    def catalogo(self) -> List[dict]:
        """Lista degli script raggruppati per categoria, per la UI."""
        per_categoria: Dict[str, List[dict]] = {}
        for s in self._scripts.values():
            per_categoria.setdefault(s.categoria, []).append(s.to_dict())
        return [
            {"categoria": cat, "script": items}
            for cat, items in per_categoria.items()
        ]

    async def esegui(
        self, script_id: str, serials: List[str], params: Optional[dict] = None,
    ) -> List[ScriptResult]:
        """Esegue lo script in parallelo su tutti i dispositivi indicati."""
        script = self._scripts.get(script_id)
        if not script:
            return [ScriptResult("", False, f"Script '{script_id}' inesistente")]
        if not serials:
            return [ScriptResult("", False, "Nessun dispositivo selezionato")]

        params = params or {}
        mancanti = [
            p.label for p in script.parametri
            if p.obbligatorio and not str(params.get(p.name, "")).strip()
        ]
        if mancanti:
            return [ScriptResult(
                "", False, f"Parametri mancanti: {', '.join(mancanti)}",
            )]

        logs.info(f"Script '{script.nome}' su {len(serials)} dispositivi")

        risultati = await asyncio.gather(*[
            self._esegui_su(script, serial, params) for serial in serials
        ], return_exceptions=True)

        finali: List[ScriptResult] = []
        for serial, res in zip(serials, risultati):
            if isinstance(res, Exception):
                finali.append(ScriptResult(serial, False, f"Errore: {res}"))
            else:
                finali.append(res)

        ok = sum(1 for r in finali if r.ok)
        if ok == len(finali):
            logs.success(f"Script '{script.nome}' completato su {ok} dispositivi")
        else:
            logs.warn(f"Script '{script.nome}': {ok}/{len(finali)} riusciti")
        return finali

    async def _esegui_su(
        self, script: Script, serial: str, params: dict,
    ) -> ScriptResult:
        try:
            if script.handler:
                return await script.handler(self, serial, params)

            output: List[str] = []
            for cmd in script.comandi:
                # Sostituisce i placeholder {param} con i valori forniti, quotati
                # per evitare shell injection lato dispositivo.
                reale = cmd
                for p in script.parametri:
                    val = shlex.quote(str(params.get(p.name, p.default)))
                    reale = reale.replace("{" + p.name + "}", val)
                out = await self._adb.shell(serial, reale, timeout=script.timeout)
                if out:
                    output.append(out)
            return ScriptResult(
                serial, True, "Completato", "\n".join(output).strip(),
            )
        except asyncio.TimeoutError:
            return ScriptResult(serial, False, "Timeout")
        except Exception as exc:
            return ScriptResult(serial, False, f"Errore: {exc}")

    # ------------------------------------------------------------------
    # Helper interni
    # ------------------------------------------------------------------

    async def _shell(self, serial: str, cmd: str, timeout: float = 15.0) -> str:
        return await self._adb.shell(serial, cmd, timeout=timeout)

    async def _key(self, serial: str, keycode: int) -> None:
        await self._shell(serial, f"input keyevent {keycode}")

    async def _schermo_acceso(self, serial: str) -> bool:
        """Verifica se il display è accesso."""
        out = await self._shell(serial, "dumpsys display")
        if re.search(r"mScreenState\s*=\s*ON\b", out, re.I):
            return True
        # Fallback per ROM che non espongono mScreenState
        out = await self._shell(serial, "dumpsys power")
        return bool(re.search(r"Display\s+Power:\s*state=ON\b", out, re.I))

    async def _bloccato(self, serial: str) -> bool:
        """Verifica se il keyguard (lockscreen) è attivo."""
        out = await self._shell(serial, "dumpsys window")
        return bool(re.search(r"m(Dreaming|Showing)Lockscreen\s*=\s*true", out, re.I))

    # ------------------------------------------------------------------
    # Handler: sblocco con PIN
    # ------------------------------------------------------------------

    async def _sblocca_pin(self, serial: str, params: dict) -> ScriptResult:
        """Sblocca il dispositivo inserendo il PIN.

        Sequenza robusta che funziona sulla maggioranza delle ROM:
        1. accende lo schermo
        2. se il keyguard è attivo, fa swipe verso l'alto e tappa al centro
        3. digita il PIN tramite keyevent (cifra per cifra) e conferma
        4. verifica che il keyguard sia effettivamente caduto
        """
        pin = str(params.get("pin", "")).strip()
        if not pin.isdigit():
            return ScriptResult(serial, False, "Il PIN deve contenere solo cifre")

        # 1. Accende lo schermo (power, poi wakeup se serve)
        if not await self._schermo_acceso(serial):
            await self._key(serial, KEY_POWER)
            await asyncio.sleep(0.6)
        if not await self._schermo_acceso(serial):
            await self._key(serial, KEY_WAKEUP)
            await asyncio.sleep(0.7)

        # Se non è bloccato, non serve fare altro
        if not await self._bloccato(serial):
            return ScriptResult(serial, True, "Già sbloccato")

        # 2. Calcola il centro dello schermo e mostra il tastierino
        size = await self._shell(serial, "wm size")
        larghezza, altezza = 1080, 1920
        match = re.search(r"(\d+)x(\d+)", size)
        if match:
            larghezza, altezza = int(match.group(1)), int(match.group(2))

        cx, cy = larghezza // 2, altezza // 2
        await self._shell(
            serial,
            f"input swipe {cx} {int(altezza * 0.85)} {cx} {int(altezza * 0.15)} 200",
        )
        await asyncio.sleep(0.5)
        await self._shell(serial, f"input tap {cx} {cy}")
        await asyncio.sleep(0.3)

        # 3. Inserisce il PIN cifra per cifra con keyevent
        keycodes = " ".join(str(7 + int(d)) for d in pin)  # KEYCODE_0..9 = 7..16
        await self._shell(serial, f"input keyevent {keycodes}")
        await asyncio.sleep(0.3)
        await self._key(serial, KEY_ENTER)
        await asyncio.sleep(1.2)

        # 4. Verifica
        if not await self._bloccato(serial):
            return ScriptResult(serial, True, "Dispositivo sbloccato")

        # Fallback: alcune ROM accettano solo input text con conferma alternativa
        await self._shell(serial, f"input text {pin}")
        await asyncio.sleep(0.3)
        await self._key(serial, KEY_ENTER)
        await asyncio.sleep(1.0)

        if not await self._bloccato(serial):
            return ScriptResult(serial, True, "Dispositivo sbloccato")

        # Ultimo tentativo: conferma con DPAD_CENTER
        await self._shell(serial, f"input keyevent {keycodes}")
        await asyncio.sleep(0.3)
        await self._key(serial, KEY_MENU)
        await asyncio.sleep(1.0)

        if await self._bloccato(serial):
            return ScriptResult(
                serial, False, "Sblocco fallito: PIN errato o ROM non compatibile",
            )
        return ScriptResult(serial, True, "Dispositivo sbloccato")

    async def _blocca(self, serial: str, params: dict) -> ScriptResult:
        """Blocca il dispositivo e spegne lo schermo."""
        if await self._schermo_acceso(serial):
            await self._key(serial, KEY_SLEEP)
        return ScriptResult(serial, True, "Dispositivo bloccato")

    # ------------------------------------------------------------------
    # Handler: informazioni dispositivo
    # ------------------------------------------------------------------

    async def _info_dispositivo(self, serial: str, params: dict) -> ScriptResult:
        """Raccoglie una scheda completa del dispositivo."""
        async def prop(nome: str) -> str:
            return (await self._shell(serial, f"getprop {nome}")).strip()

        modello, marca, android, sdk, build = await asyncio.gather(
            prop("ro.product.model"),
            prop("ro.product.manufacturer"),
            prop("ro.build.version.release"),
            prop("ro.build.version.sdk"),
            prop("ro.build.display.id"),
        )

        batteria = await self._shell(serial, "dumpsys battery")
        livello = re.search(r"level: (\d+)", batteria)
        temp = re.search(r"temperature: (\d+)", batteria)
        salute = re.search(r"health: (\d+)", batteria)

        risoluzione = await self._shell(serial, "wm size")
        densita = await self._shell(serial, "wm density")
        storage = await self._shell(serial, "df /data | tail -1")
        ram = await self._shell(serial, "cat /proc/meminfo | head -2")
        cpu = await prop("ro.product.cpu.abi")

        salute_txt = {
            "1": "sconosciuta", "2": "buona", "3": "surriscaldata",
            "4": "esausta", "5": "sovratensione", "6": "guasta", "7": "fredda",
        }.get(salute.group(1) if salute else "", "n/d")

        righe = [
            f"Dispositivo:   {marca} {modello}",
            f"Android:       {android} (API {sdk})",
            f"Build:         {build}",
            f"CPU:           {cpu}",
            f"{risoluzione}",
            f"{densita}",
            f"Batteria:      {livello.group(1) if livello else '?'}%"
            f"  |  {int(temp.group(1)) / 10 if temp else '?'}°C"
            f"  |  salute: {salute_txt}",
            f"Storage /data: {storage}",
            f"Memoria:       {ram}",
        ]
        return ScriptResult(serial, True, "Info raccolte", "\n".join(righe))

    async def _stato_batteria(self, serial: str, params: dict) -> ScriptResult:
        out = await self._shell(serial, "dumpsys battery")
        livello = re.search(r"level: (\d+)", out)
        temp = re.search(r"temperature: (\d+)", out)
        stato = "in carica" if "AC powered: true" in out or "USB powered: true" in out else "a batteria"
        msg = (
            f"{livello.group(1) if livello else '?'}% "
            f"({int(temp.group(1)) / 10 if temp else '?'}°C, {stato})"
        )
        return ScriptResult(serial, True, msg, out)

    async def _lista_app(self, serial: str, params: dict) -> ScriptResult:
        """Elenca i pacchetti installati dall'utente (esclusi quelli di sistema)."""
        out = await self._shell(serial, "pm list packages -3", timeout=30.0)
        pacchetti = sorted(
            line.replace("package:", "").strip()
            for line in out.splitlines() if line.startswith("package:")
        )
        return ScriptResult(
            serial, True, f"{len(pacchetti)} app installate", "\n".join(pacchetti),
        )

    async def _monkey_test(self, serial: str, params: dict) -> ScriptResult:
        """Stress test casuale su un'app: utile per test massivi di stabilità."""
        pacchetto = str(params.get("pacchetto", "")).strip()
        eventi = str(params.get("eventi", "500")).strip() or "500"
        if not re.match(r"^[A-Za-z][A-Za-z0-9_\.]*$", pacchetto):
            return ScriptResult(serial, False, "Nome pacchetto non valido")
        if not re.match(r"^[1-9]\d*$", eventi):
            return ScriptResult(serial, False, "Numero di eventi non valido")
        cmd = (
            f"monkey -p {pacchetto} --throttle 100 "
            f"--pct-syskeys 0 --ignore-crashes --ignore-timeouts "
            f"-v {eventi}"
        )
        out = await self._shell(serial, cmd, timeout=300.0)
        crash = "// CRASH" in out or "Monkey aborted" in out
        return ScriptResult(
            serial, not crash,
            "Crash rilevato durante il test" if crash else f"{eventi} eventi eseguiti",
            out[-2000:],
        )

    async def _shell_libera(self, serial: str, params: dict) -> ScriptResult:
        """Esegue un comando shell arbitrario."""
        cmd = str(params.get("comando", "")).strip()
        out = await self._shell(serial, cmd, timeout=60.0)
        return ScriptResult(serial, True, "Comando eseguito", out)

    # ------------------------------------------------------------------
    # Registro degli script
    # ------------------------------------------------------------------

    def _aggiungi(self, script: Script) -> None:
        self._scripts[script.id] = script

    def _register_all(self) -> None:
        # ---------------- Sblocco e schermo ----------------
        self._aggiungi(Script(
            id="sblocca_pin",
            nome="Sblocca con PIN",
            descrizione="Sveglia il dispositivo, mostra il tastierino e inserisce il PIN.",
            categoria="Sblocco e schermo",
            icona="🔓",
            parametri=[ScriptParam(
                "pin", "PIN del dispositivo", tipo="password",
                placeholder="es. 1234",
            )],
            handler=ScriptEngine._sblocca_pin,
        ))
        self._aggiungi(Script(
            id="blocca",
            nome="Blocca dispositivo",
            descrizione="Spegne lo schermo e attiva il blocco.",
            categoria="Sblocco e schermo",
            icona="🔒",
            handler=ScriptEngine._blocca,
        ))
        self._aggiungi(Script(
            id="accendi_schermo",
            nome="Accendi schermo",
            descrizione="Sveglia il display senza sbloccare.",
            categoria="Sblocco e schermo",
            icona="💡",
            comandi=[f"input keyevent {KEY_WAKEUP}"],
        ))
        self._aggiungi(Script(
            id="spegni_schermo",
            nome="Spegni schermo",
            descrizione="Manda il display in standby.",
            categoria="Sblocco e schermo",
            icona="🌙",
            comandi=[f"input keyevent {KEY_SLEEP}"],
        ))
        self._aggiungi(Script(
            id="schermo_sempre_acceso",
            nome="Schermo sempre acceso",
            descrizione="Impedisce lo spegnimento del display mentre è sotto USB. Ideale per test lunghi.",
            categoria="Sblocco e schermo",
            icona="☀",
            comandi=["svc power stayon usb"],
        ))
        self._aggiungi(Script(
            id="schermo_standby_normale",
            nome="Ripristina standby normale",
            descrizione="Riattiva lo spegnimento automatico del display.",
            categoria="Sblocco e schermo",
            icona="🔄",
            comandi=["svc power stayon false"],
        ))
        self._aggiungi(Script(
            id="luminosita",
            nome="Imposta luminosità",
            descrizione="Regola la luminosità del display (0-255).",
            categoria="Sblocco e schermo",
            icona="🔆",
            parametri=[ScriptParam(
                "valore", "Luminosità (0-255)", tipo="number", default="128",
            )],
            comandi=[
                "settings put system screen_brightness_mode 0",
                "settings put system screen_brightness {valore}",
            ],
        ))

        # ---------------- Sistema ----------------
        self._aggiungi(Script(
            id="info_dispositivo",
            nome="Scheda dispositivo",
            descrizione="Modello, Android, CPU, risoluzione, batteria, storage e memoria.",
            categoria="Sistema",
            icona="📋",
            handler=ScriptEngine._info_dispositivo,
        ))
        self._aggiungi(Script(
            id="stato_batteria",
            nome="Stato batteria",
            descrizione="Livello, temperatura e stato di carica.",
            categoria="Sistema",
            icona="🔋",
            handler=ScriptEngine._stato_batteria,
        ))
        self._aggiungi(Script(
            id="disattiva_animazioni",
            nome="Disattiva animazioni",
            descrizione="Azzera le animazioni di sistema: i test diventano molto più rapidi e stabili.",
            categoria="Sistema",
            icona="⚡",
            comandi=[
                "settings put global window_animation_scale 0",
                "settings put global transition_animation_scale 0",
                "settings put global animator_duration_scale 0",
            ],
        ))
        self._aggiungi(Script(
            id="attiva_animazioni",
            nome="Riattiva animazioni",
            descrizione="Ripristina le animazioni di sistema al valore predefinito.",
            categoria="Sistema",
            icona="🎬",
            comandi=[
                "settings put global window_animation_scale 1",
                "settings put global transition_animation_scale 1",
                "settings put global animator_duration_scale 1",
            ],
        ))
        self._aggiungi(Script(
            id="riavvia",
            nome="Riavvia dispositivo",
            descrizione="Riavvio normale del sistema.",
            categoria="Sistema",
            icona="♻",
            comandi=["reboot"],
            pericoloso=True,
        ))
        self._aggiungi(Script(
            id="data_ora_automatica",
            nome="Data e ora automatiche",
            descrizione="Attiva la sincronizzazione automatica di data, ora e fuso.",
            categoria="Sistema",
            icona="🕐",
            comandi=[
                "settings put global auto_time 1",
                "settings put global auto_time_zone 1",
            ],
        ))
        self._aggiungi(Script(
            id="lingua_italiana",
            nome="Verifica lingua di sistema",
            descrizione="Mostra la lingua e il fuso orario configurati sul dispositivo.",
            categoria="Sistema",
            icona="🇮🇹",
            comandi=[
                "getprop persist.sys.locale",
                "getprop persist.sys.timezone",
            ],
        ))

        # ---------------- App ----------------
        self._aggiungi(Script(
            id="lista_app",
            nome="Elenca app installate",
            descrizione="Lista dei pacchetti installati dall'utente.",
            categoria="App",
            icona="📦",
            handler=ScriptEngine._lista_app,
        ))
        self._aggiungi(Script(
            id="apri_app",
            nome="Apri app",
            descrizione="Avvia un'app dal nome del pacchetto.",
            categoria="App",
            icona="▶",
            parametri=[ScriptParam(
                "pacchetto", "Nome pacchetto", placeholder="es. com.android.chrome",
            )],
            comandi=["monkey -p {pacchetto} -c android.intent.category.LAUNCHER 1"],
        ))
        self._aggiungi(Script(
            id="chiudi_app",
            nome="Chiudi app",
            descrizione="Termina forzatamente un'app.",
            categoria="App",
            icona="⏹",
            parametri=[ScriptParam("pacchetto", "Nome pacchetto")],
            comandi=["am force-stop {pacchetto}"],
        ))
        self._aggiungi(Script(
            id="svuota_dati_app",
            nome="Svuota dati app",
            descrizione="Cancella dati e cache di un'app, riportandola allo stato iniziale.",
            categoria="App",
            icona="🧹",
            parametri=[ScriptParam("pacchetto", "Nome pacchetto")],
            comandi=["pm clear {pacchetto}"],
            pericoloso=True,
        ))
        self._aggiungi(Script(
            id="disinstalla_app",
            nome="Disinstalla app",
            descrizione="Rimuove un'app dal dispositivo.",
            categoria="App",
            icona="🗑",
            parametri=[ScriptParam("pacchetto", "Nome pacchetto")],
            comandi=["pm uninstall {pacchetto}"],
            pericoloso=True,
        ))
        self._aggiungi(Script(
            id="concedi_permessi",
            nome="Concedi tutti i permessi",
            descrizione="Concede automaticamente i permessi runtime più comuni a un'app.",
            categoria="App",
            icona="✅",
            parametri=[ScriptParam("pacchetto", "Nome pacchetto")],
            comandi=[
                "pm grant {pacchetto} android.permission.CAMERA",
                "pm grant {pacchetto} android.permission.RECORD_AUDIO",
                "pm grant {pacchetto} android.permission.ACCESS_FINE_LOCATION",
                "pm grant {pacchetto} android.permission.READ_EXTERNAL_STORAGE",
                "pm grant {pacchetto} android.permission.WRITE_EXTERNAL_STORAGE",
                "pm grant {pacchetto} android.permission.READ_CONTACTS",
                "pm grant {pacchetto} android.permission.POST_NOTIFICATIONS",
            ],
        ))

        # ---------------- Rete ----------------
        self._aggiungi(Script(
            id="wifi_on",
            nome="Attiva Wi-Fi",
            descrizione="Accende la radio Wi-Fi.",
            categoria="Rete",
            icona="📶",
            comandi=["svc wifi enable"],
        ))
        self._aggiungi(Script(
            id="wifi_off",
            nome="Disattiva Wi-Fi",
            descrizione="Spegne la radio Wi-Fi.",
            categoria="Rete",
            icona="📴",
            comandi=["svc wifi disable"],
        ))
        self._aggiungi(Script(
            id="dati_on",
            nome="Attiva dati mobili",
            descrizione="Accende la connessione dati cellulare.",
            categoria="Rete",
            icona="🌐",
            comandi=["svc data enable"],
        ))
        self._aggiungi(Script(
            id="dati_off",
            nome="Disattiva dati mobili",
            descrizione="Spegne la connessione dati cellulare.",
            categoria="Rete",
            icona="✈",
            comandi=["svc data disable"],
        ))
        self._aggiungi(Script(
            id="info_rete",
            nome="Informazioni di rete",
            descrizione="Indirizzo IP, Wi-Fi collegato e stato della connessione.",
            categoria="Rete",
            icona="🔍",
            comandi=[
                "ip route | grep wlan",
                "dumpsys wifi | grep 'mWifiInfo SSID'",
            ],
        ))

        # ---------------- Test e diagnostica ----------------
        self._aggiungi(Script(
            id="monkey_test",
            nome="Stress test (monkey)",
            descrizione="Genera eventi casuali su un'app per verificarne la stabilità.",
            categoria="Test e diagnostica",
            icona="🐒",
            parametri=[
                ScriptParam("pacchetto", "Nome pacchetto"),
                ScriptParam(
                    "eventi", "Numero di eventi", tipo="number", default="500",
                ),
            ],
            handler=ScriptEngine._monkey_test,
        ))
        self._aggiungi(Script(
            id="svuota_logcat",
            nome="Svuota logcat",
            descrizione="Azzera il buffer dei log: utile prima di iniziare un test.",
            categoria="Test e diagnostica",
            icona="🧽",
            comandi=["logcat -c"],
        ))
        self._aggiungi(Script(
            id="errori_logcat",
            nome="Leggi errori recenti",
            descrizione="Estrae gli ultimi errori e crash dal logcat.",
            categoria="Test e diagnostica",
            icona="🐛",
            comandi=["logcat -d -t 200 *:E"],
            timeout=30.0,
        ))
        self._aggiungi(Script(
            id="app_in_primo_piano",
            nome="App in primo piano",
            descrizione="Mostra quale attività è attualmente visibile.",
            categoria="Test e diagnostica",
            icona="👁",
            comandi=["dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"],
        ))
        self._aggiungi(Script(
            id="prestazioni_grafiche",
            nome="Prestazioni grafiche",
            descrizione="Statistiche di rendering di un'app: frame lenti e jank.",
            categoria="Test e diagnostica",
            icona="📊",
            parametri=[ScriptParam("pacchetto", "Nome pacchetto")],
            comandi=["dumpsys gfxinfo {pacchetto} | head -30"],
        ))
        self._aggiungi(Script(
            id="memoria_app",
            nome="Memoria usata da un'app",
            descrizione="Consumo di RAM dettagliato di un pacchetto.",
            categoria="Test e diagnostica",
            icona="💾",
            parametri=[ScriptParam("pacchetto", "Nome pacchetto")],
            comandi=["dumpsys meminfo {pacchetto} | head -25"],
        ))
        self._aggiungi(Script(
            id="shell_libera",
            nome="Comando shell libero",
            descrizione="Esegue un comando ADB shell arbitrario sui dispositivi selezionati.",
            categoria="Test e diagnostica",
            icona="⌨",
            parametri=[ScriptParam(
                "comando", "Comando shell", placeholder="es. getprop ro.product.model",
            )],
            handler=ScriptEngine._shell_libera,
            pericoloso=True,
        ))

        # ---------------- Navigazione ----------------
        self._aggiungi(Script(
            id="vai_home",
            nome="Vai alla home",
            descrizione="Preme il tasto Home.",
            categoria="Navigazione",
            icona="🏠",
            comandi=[f"input keyevent {KEY_HOME}"],
        ))
        self._aggiungi(Script(
            id="indietro",
            nome="Indietro",
            descrizione="Preme il tasto Indietro.",
            categoria="Navigazione",
            icona="⬅",
            comandi=[f"input keyevent {KEY_BACK}"],
        ))
        self._aggiungi(Script(
            id="app_recenti",
            nome="App recenti",
            descrizione="Apre la schermata delle app recenti.",
            categoria="Navigazione",
            icona="🗂",
            comandi=[f"input keyevent {KEY_APP_SWITCH}"],
        ))
        self._aggiungi(Script(
            id="chiudi_tutte_recenti",
            nome="Chiudi tutte le app recenti",
            descrizione="Termina i processi in background per liberare memoria.",
            categoria="Navigazione",
            icona="🧯",
            comandi=["am kill-all"],
        ))
        self._aggiungi(Script(
            id="apri_impostazioni",
            nome="Apri Impostazioni",
            descrizione="Avvia l'app Impostazioni di sistema.",
            categoria="Navigazione",
            icona="⚙",
            comandi=["am start -a android.settings.SETTINGS"],
        ))
