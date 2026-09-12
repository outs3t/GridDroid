"""Motore di streaming: scrcpy-server standalone → TCP raw H264 → WebSocket."""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .adb_manager import adb_cmd_lock, adb_server_args
from .config import AppSettings, load_device_overrides, save_device_overrides
from .control_channel import ControlChannel
from .device import SCREEN_OFF_REQUESTED
from .log_manager import logs

# Nessuna finestra di terminale per i processi figli su Windows
if os.name == "nt":
    _SUBPROCESS_KW = {"creationflags": 0x08000000}
else:
    _SUBPROCESS_KW = {}

_SCRCPY_VERSION = "4.1"
_BASE_PORT = 27183

# Seriali il cui encoder crasha con video_codec_options=i-frame-interval
# (0 frame prodotti). A livello di modulo perche' l'auto-stream ricrea
# DeviceStream a ogni tentativo: lo stato deve sopravvivere.
_IFRAME_UNSUPPORTED: Set[str] = set()

# Throttling adattivo: livello di degradazione per seriale (0 = pieno).
# A ogni fallimento encoder consecutivo si alza (fps e bitrate ridotti),
# dopo uno stream stabile si azzera. Sopravvive alla ricreazione di
# DeviceStream perche' l'auto-stream ne crea uno nuovo a ogni tentativo.
_DEGRADED: Dict[str, int] = {}
# livelli: 0=100%, 1=50% fps, 2=25% fps+meta' bitrate, 3=minimo
_DEGRADE_MAX = 3

# Risoluzione fisica per seriale: costante nel tempo, la rileviamo una volta
# sola invece che a ogni riavvio di stream.
_NATIVE_SIZE_CACHE: Dict[str, tuple] = {}

# Seriali su cui scrcpy-server.jar e' gia' stato copiato in questa sessione.
_JAR_PUSHED: Set[str] = set()

# Seriali per cui l'encoder hardware ha fallito: usiamo software.
_HW_ENCODER_FAILED: Set[str] = set()


def _degrade_factor(level: int) -> Tuple[float, float]:
    """Ritorna (fattore_fps, fattore_bitrate) per il livello di degradazione."""
    return {
        0: (1.0, 1.0),
        1: (0.5, 0.75),
        2: (0.25, 0.5),
        3: (0.15, 0.35),
    }.get(level, (0.15, 0.35))


def _tools_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "tools"
    return Path(__file__).parent.parent / "tools"


def _find_start_code(b: bytearray, start: int) -> int:
    """Trova la posizione del prossimo start code Annex-B (00 00 01 o 00 00 00 01)."""
    n = len(b)
    i = start
    while i + 2 < n:
        if b[i] == 0 and b[i + 1] == 0:
            if b[i + 2] == 1:
                return i
            if b[i + 2] == 0 and i + 3 < n and b[i + 3] == 1:
                return i
        i += 1
    return -1


def _find_scrcpy_server() -> Optional[str]:
    bundled = _tools_dir() / "scrcpy-server"
    if bundled.exists():
        return str(bundled)
    return None


def _is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) != 0


# Protegge l'allocazione della porta tra più stream paralleli
_PORT_LOCK: Optional[asyncio.Lock] = None


def _port_lock() -> asyncio.Lock:
    global _PORT_LOCK
    if _PORT_LOCK is None:
        _PORT_LOCK = asyncio.Lock()
    return _PORT_LOCK


class DeviceStream:
    """Streaming di un singolo dispositivo via scrcpy-server TCP → ffmpeg → JPEG."""

    def __init__(
        self,
        serial: str,
        settings: AppSettings,
        start_sem: Optional[asyncio.Semaphore] = None,
        max_size_override: Optional[int] = None,
        max_fps_override: Optional[int] = None,
        bit_rate_override: Optional[int] = None,
    ) -> None:
        self.serial = serial
        self._settings = settings
        # Risoluzione dedicata a questo device (fullscreen): se impostata
        # sovrascrive il max_size globale solo per questo stream.
        self.max_size_override = max_size_override
        self.max_fps_override = max_fps_override
        self.bit_rate_override = bit_rate_override
        self._pre_zoom: Optional[Tuple[Optional[int], Optional[int], Optional[int]]] = None
        # Limite avvii concorrenti per non sovraccaricare ADB (default 4)
        self._start_sem: Optional[asyncio.Semaphore] = start_sem
        self._last_heartbeat = time.monotonic()
        self._server_proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._sps: bytes = b""
        self._pps: bytes = b""
        self._h264_config: bytes = b""
        self._last_keyframe: Optional[bytes] = None
        self._subscribers: Set[asyncio.Queue] = set()
        # Code che hanno perso frame: ricevono solo keyframe finche' non si riallineano
        self._desynced: Set[asyncio.Queue] = set()
        # Device il cui encoder crasha con i-frame-interval (0 frame):
        # set a livello di modulo perche' l'auto-stream ricrea DeviceStream
        # a ogni tentativo e una variabile locale si resetterebbe.
        self._iframe_unsupported = _IFRAME_UNSUPPORTED
        self._degrade_level = _DEGRADED.get(serial, 0)
        self._task: Optional[asyncio.Task] = None
        self._log_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._native_width: int = 0
        self._native_height: int = 0
        self._tcp_port: int = 0
        self._writer: Optional[asyncio.StreamWriter] = None
        self._control: Optional[ControlChannel] = None
        self._server_ready = asyncio.Event()
        self._server_error: Optional[str] = None

    @property
    def alive(self) -> bool:
        if not self._running or self._task is None:
            return False
        # Vitalita' = la pipeline sta girando. NON si guarda l'ultimo frame:
        # con schermo statico MediaCodec non emette nulla e uno stream sano
        # resta muto per minuti (prima veniva ucciso e riavviato ogni 180s).
        # Se l'encoder muore il socket si chiude e la task termina, quindi
        # _task.done() copre comunque il caso reale.
        return not self._task.done()

    @property
    def native_size(self) -> tuple:
        return (self._native_width, self._native_height)

    @property
    def last_keyframe(self) -> Optional[bytes]:
        """Ultimo keyframe H264, per permettere ai nuovi client di iniziare subito."""
        return self._last_keyframe

    @property
    def control(self) -> Optional[ControlChannel]:
        """Canale di controllo scrcpy, se attivo: input nativi a latenza minima."""
        if self._control and self._control.alive:
            return self._control
        return None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_heartbeat = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        # Sblocca subito i subscriber: il WS video deve chiudersi anche se
        # la task e' gia' morta o impiega tempo a terminare.
        self._signal_stream_end()
        if self._control:
            try:
                await self._control.close()
            except Exception:
                pass
            self._control = None
        if self._writer:
            try:
                if not self._writer.is_closing():
                    self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._server_proc:
            try:
                if self._server_proc.returncode is None:
                    self._server_proc.terminate()
                    try:
                        await asyncio.wait_for(self._server_proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        try:
                            self._server_proc.kill()
                        except Exception:
                            pass
            except (ProcessLookupError, Exception):
                pass
            self._server_proc = None
        if self._tcp_port:
            await self._remove_forward()
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._task = None

    def subscribe(self) -> asyncio.Queue:
        # Coda da 1 frame: "last frame wins". Il client riceve SEMPRE il
        # frame piu' recente prodotto dallo scrcpy-server. Se e' lento e
        # non riesce a consumare, i vecchi frame vengono sovrascritti dai
        # nuovi in _distribute_frame. Latenza minima possibile per
        # fullscreen e interazione reattiva.
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        self._desynced.discard(q)

    # ------------------------------------------------------------------
    # Core: scrcpy-server standalone via TCP
    # ------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _start_sem_cm(self):
        """Acquisisce (se presente) il semaforo per limitare avvii concorrenti."""
        if self._start_sem is not None:
            async with self._start_sem:
                yield
        else:
            yield

    def _on_server_output(self, text: str, tag: str) -> None:
        """Intercetta l'output di scrcpy-server per capire quando e' pronto."""
        if "INFO: Device:" in text and not self._server_ready.is_set():
            self._server_ready.set()
        if (
            "ERROR:" in text
            or "FATAL:" in text
            or "Exception" in text
            or ("device" in text and "not found" in text)
        ):
            if not self._server_error:
                self._server_error = text
            if not self._server_ready.is_set():
                self._server_ready.set()
            # Encoder morto a meta' stream (es. 'Released state'): il socket
            # resta aperto ma non arrivano piu' frame. Chiudiamo il writer per
            # sbloccare read() e far ripartire lo stream subito.
            if "Capture/encoding error" in text and self._writer is not None:
                # Se stavamo usando l'encoder hw, alla prossima occasione
                # proviamo con software per questo seriale.
                if self.serial not in _HW_ENCODER_FAILED and not getattr(self._settings.stream, "software_encoder", False):
                    _HW_ENCODER_FAILED.add(self.serial)
                    logs.warn(
                        f"Encoder hw fallito per {self.serial}, "
                        "prossimo tentativo con software",
                        serial=self.serial,
                    )
                try:
                    self._writer.close()
                except Exception:
                    pass

    async def _run(self) -> None:
        try:
            server_jar = _find_scrcpy_server()
            if server_jar:
                await self._run_scrcpy_server(server_jar)
            else:
                logs.error("scrcpy-server non trovato, stream impossibile", serial=self.serial)
                self._running = False
        finally:
            self._signal_stream_end()

    def _signal_stream_end(self) -> None:
        """Sveglia i subscriber bloccati su q.get() con un sentinel None.

        Senza questo il WS /ws/stream resta aperto all'infinito quando lo
        stream muore: il browser non riceve onclose, non schedula il retry
        e il feed resta fermo (bug del 'Riavvia stream' che non riparte).
        """
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(None)
                except Exception:
                    pass
            except Exception:
                pass

    async def _is_device_online(self) -> bool:
        adb = self._settings.adb_path or "adb"
        async with adb_cmd_lock():
            try:
                proc = await asyncio.create_subprocess_exec(
                    adb, *adb_server_args(self.serial),
                    "-s", self.serial, "get-state",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_SUBPROCESS_KW,
                )
                # Timeout corto: un device appeso non deve tenere il lock
                # adb globale per 30s bloccando il polling di tutti.
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=8.0)
                return stdout.decode("utf-8", errors="replace").strip() == "device"
            except Exception:
                return False

    async def _run_scrcpy_server(self, server_jar: str) -> None:
        adb = self._settings.adb_path or "adb"
        s = self._settings.stream

        await self._detect_native_resolution()

        consecutive_failures = 0

        while self._running:
            self._last_heartbeat = time.monotonic()
            if not await self._is_device_online():
                logs.warn(
                    "Dispositivo non più raggiungibile, interrompo stream",
                    serial=self.serial,
                )
                self._running = False
                break

            reader: Optional[asyncio.StreamReader] = None
            try:
                async with self._start_sem_cm():
                    self._last_heartbeat = time.monotonic()

                    # 0. Sveglia il dispositivo: scrcpy richiede display attivo.
                    #    Timeout corto: un device appeso non deve tenere il
                    #    lock adb globale bloccando i comandi degli altri.
                    #    Saltato se l'utente ha bloccato lo schermo di questo
                    #    device: altrimenti il primo riavvio di stream lo
                    #    riaccendeva e il blocco sembrava fallire a caso.
                    if self.serial not in SCREEN_OFF_REQUESTED:
                        try:
                            await self._adb_exec(
                                adb, "shell", "input", "keyevent", "KEYCODE_WAKEUP",
                                timeout=8.0,
                            )
                        except Exception:
                            pass

                    # 1. Push server jar — una volta sola per sessione.
                    #    Ripusharlo a ogni riavvio di stream significa, con
                    #    una farm che ricicla, un trasferimento continuo
                    #    sotto il lock adb globale. Se l'avvio poi fallisce
                    #    la cache viene invalidata e si ripusha.
                    remote_jar = "/data/local/tmp/scrcpy-server.jar"
                    if self.serial not in _JAR_PUSHED:
                        await self._adb_exec(adb, "push", server_jar, remote_jar, timeout=60.0)
                        _JAR_PUSHED.add(self.serial)

                    # 2. Setup forward con porta libera (evita collisioni)
                    async with _port_lock():
                        for _ in range(20):
                            port = _BASE_PORT + random.randint(0, 999)
                            if _is_port_free("127.0.0.1", port):
                                break
                        else:
                            raise RuntimeError("Nessuna porta TCP libera per il forward")
                        self._tcp_port = port
                        scid_int = random.randint(0, 0x7FFFFFFF)
                        scid_hex = f"{scid_int:08x}"
                        socket_name = f"scrcpy_{scid_hex}"
                        await self._adb_exec(
                            adb, "forward", f"tcp:{self._tcp_port}",
                            f"localabstract:{socket_name}",
                        )

                    # 3. Avvia server sul dispositivo.
                    #    raw_stream=true produce un flusso H.264 puro (senza meta).
                    server_cmd = (
                        f"CLASSPATH={remote_jar} "
                        f"app_process / com.genymobile.scrcpy.Server {_SCRCPY_VERSION} "
                        f"tunnel_forward=true "
                        f"audio=false control=true cleanup=false "
                        f"clipboard_autosync=false "
                        f"show_touches=true stay_awake=true power_off_on_close=false "
                        f"raw_stream=true "
                        f"max_size={self.max_size_override or s.max_size} "
                        + self._adaptive_params(s)
                        # i-frame-interval disabilitato: anche con encoder hw
                        # ha causato 'encoder senza frame' e riavvii in loop.
                        # Panda non lo usa e i log confermano che e' piu' stabile
                        # senza forzare keyframe ogni 2s.
                        + ""
                        + f"scid={scid_hex}"
                    )
                    iframe_requested = "video_codec_options=i-frame-interval" in server_cmd
                    self._server_proc = await asyncio.create_subprocess_exec(
                        adb, *adb_server_args(self.serial),
                        "-s", self.serial, "shell", server_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **_SUBPROCESS_KW,
                    )
                    self._server_ready.clear()
                    self._server_error = None
                    self._log_task = asyncio.create_task(self._log_proc_output(self._server_proc, "server"))
                    logs.info(f"scrcpy-server avviato (porta {self._tcp_port})", serial=self.serial)

                    # Attende che il server logghi "INFO: Device:"
                    try:
                        await asyncio.wait_for(self._server_ready.wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        raise RuntimeError("scrcpy-server non pronto entro 6s")
                    if self._server_error:
                        raise RuntimeError(f"scrcpy-server: {self._server_error}")

                    # 4. Connessione video con retry (evita WinError 1225 e l'EOF
                    #    che si riceve se il socket remoto di scrcpy non e' ancora attivo).
                    last_exc: Optional[Exception] = None
                    await asyncio.sleep(0.5)  # aspetta che il ServerSocket si bindi
                    for attempt in range(10):
                        try:
                            reader, self._writer = await asyncio.wait_for(
                                asyncio.open_connection("127.0.0.1", self._tcp_port),
                                timeout=5.0,
                            )
                            # Se il socket non ha ancora dati, scrcpy ha appena bindato,
                            # ma la connessione remota e' ancora in corso. Richiudiamo e riproviamo.
                            if not self._running:
                                break
                            break
                        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
                            last_exc = exc
                            if attempt < 9:
                                await asyncio.sleep(0.3)
                    if reader is None or self._writer is None:
                        raise last_exc or RuntimeError("Connessione video TCP rifiutata")
                    logs.success("Connesso al video socket TCP", serial=self.serial)

                    # 5. Connessione al canale di controllo (input nativi)
                    await asyncio.sleep(0.3)
                    await self._connect_control()

                # 6. Watchdog del processo server: se muore in silenzio col
                #    socket ancora aperto, sblocca la read e riavvia lo stream.
                self._watchdog_task = asyncio.create_task(self._watch_server_proc())

                # 7. Passthrough H264 → browser (decodifica hardware WebCodecs)
                self._last_video_data = time.monotonic()
                au_count = await self._stream_h264(reader)
                if au_count > 0:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if (
                        iframe_requested
                        and self.serial not in self._iframe_unsupported
                    ):
                        _IFRAME_UNSUPPORTED.add(self.serial)
                        logs.warn(
                            "Encoder non produce frame con i-frame-interval; "
                            f"disabilitato per {self.serial}",
                            serial=self.serial,
                            throttle_s=30,
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                # Il jar remoto potrebbe essere sparito (pulizia /data/local/tmp
                # o device riavviato): al prossimo giro lo ricopiamo.
                _JAR_PUSHED.discard(self.serial)
                logs.warn(f"Errore stream: {exc}", serial=self.serial, throttle_s=30)
            finally:
                await self._cleanup_server()

            if self._running:
                if consecutive_failures >= 5:
                    logs.warn(
                        "Troppi tentativi falliti, interrompo stream",
                        serial=self.serial,
                    )
                    self._running = False
                    break
                delay = min(2.0 * (1.5 ** consecutive_failures), 60.0) * random.uniform(0.8, 1.2)
                logs.info(f"Riconnessione stream tra {delay:.1f}s...", serial=self.serial, throttle_s=30)
                await asyncio.sleep(delay)

    def _adaptive_params(self, s) -> str:
        """Parametri video con override per device e bitrate scalato."""
        max_fps = self.max_fps_override or s.max_fps
        max_size = self.max_size_override or s.max_size
        base_bit_rate = self.bit_rate_override or s.bit_rate
        # Scala bitrate con risoluzione e fps per evitare artefatti
        # su fullscreen / alta qualita': 480@2fps come riferimento.
        factor = (max_size / 480.0) ** 2 * (max(max_fps, 1) / 2.0)
        bit_rate = int(base_bit_rate * factor)
        # Encoder: default hardware per latenza minima. Se l'hw di un
        # dispositivo e' gia' crashato, torniamo a software per quel
        # seriale. Stesso tetto bitrate del sw per non sovraccaricare.
        use_software = (
            getattr(s, "software_encoder", False)
            or self.serial in _HW_ENCODER_FAILED
        )
        max_bit_rate = 1_000_000 if use_software else 8_000_000
        bit_rate = max(min(bit_rate, max_bit_rate), min(base_bit_rate, max_bit_rate))
        params = f"max_fps={max_fps} video_bit_rate={bit_rate} "
        # Encoder SOFTWARE (OMX.google) solo se esplicito o fallback.
        if use_software:
            params += "video_encoder=OMX.google.h264.encoder "
        return params

    async def _watch_server_proc(self) -> None:
        """Watchdog granulare: sorveglia il processo scrcpy-server.

        Se il processo muore in silenzio mentre il socket video resta aperto
        (server crashato senza riga ERROR), la read() resterebbe appesa per
        sempre. Chiudiamo il writer per sbloccarla e far ripartire SOLO
        questo stream — gli altri non vengono toccati.
        """
        proc = self._server_proc
        if proc is None:
            return
        stalls = 0
        try:
            while self._running:
                # Stallo video: tunnel adb morto a meta' (socket locale
                # ancora aperto, peer andato — es. saturazione USB durante
                # la lettura saldi). La read() resterebbe appesa per
                # sempre: chiudo il writer e il loop riavvia lo stream.
                # ATTENZIONE ai falsi positivi: uno schermo statico non
                # emette frame per minuti — riavviare uno stream sano costa
                # un flash nero + pressione adb che fa stallare gli altri.
                # Quindi: al primo silenzio verifico se il device e' vivo;
                # riavvio subito solo se morto, altrimenti tollero ~90s.
                last_vd = getattr(self, "_last_video_data", 0)
                if last_vd and time.monotonic() - last_vd > 30.0:
                    proc_dead = proc.returncode is not None
                    online = (
                        not proc_dead and await self._is_device_online()
                    )
                    if proc_dead or not online or stalls >= 3:
                        logs.warn(
                            "Stream video in stallo"
                            + (" (server morto)" if proc_dead else "")
                            + (" (device non raggiungibile)" if not online and not proc_dead else "")
                            + ": riavvio automatico",
                            serial=self.serial,
                        )
                        if self._writer is not None and not self._writer.is_closing():
                            try:
                                self._writer.close()
                            except Exception:
                                pass
                        return
                    # Device vivo e server vivo: schermo statico, non stallo.
                    stalls += 1
                    self._last_video_data = time.monotonic()
                    logs.info(
                        "Stream muto da 30s ma device attivo: "
                        "probabile schermo statico, attendo",
                        serial=self.serial,
                        throttle_s=120,
                    )
                    continue
                stalls = 0
                if proc.returncode is not None:
                    # Processo morto: se il socket e' ancora aperto lo chiudiamo
                    if self._writer is not None and not self._writer.is_closing():
                        logs.warn(
                            "scrcpy-server terminato in silenzio: riavvio stream",
                            serial=self.serial,
                        )
                        try:
                            self._writer.close()
                        except Exception:
                            pass
                    return
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _stream_h264(self, tcp_reader: asyncio.StreamReader) -> int:
        """Legge H264 Annex-B dal socket TCP, lo divide in access unit e le
        distribuisce ai subscriber. Il browser decodifica in hardware via WebCodecs.

        Formato messaggio: 1 byte flag (1 = keyframe, 0 = delta) + dati Annex-B.
        Ritorna il numero di access unit inviate.
        """
        buf = bytearray()
        pending = bytearray()   # NAL non-VCL (SPS/PPS/SEI) in attesa del frame
        synced = False
        au_count = 0

        while self._running:
            # Nessun timeout sulla read: se lo schermo del telefono e' statico
            # MediaCodec non emette alcun frame, quindi uno stream sano puo'
            # restare muto per minuti. Un watchdog a tempo lo ucciderebbe in
            # loop. Il caso zombie reale (encoder crashato con il socket
            # ancora aperto) e' gestito da _on_server_output, che chiude il
            # writer appena vede l'errore e sblocca questa read.
            data = await tcp_reader.read(65536)
            if not data:
                logs.info(f"TCP stream chiuso ({au_count} frame)", serial=self.serial)
                break
            self._last_heartbeat = time.monotonic()
            self._last_video_data = self._last_heartbeat
            buf.extend(data)

            # Sincronizza il buffer sul primo start code
            if not synced:
                pos = _find_start_code(buf, 0)
                if pos < 0:
                    continue
                del buf[:pos]
                synced = True

            # Estrae tutti i NAL completi presenti nel buffer
            while True:
                sc_len = 4 if buf[:4] == b"\x00\x00\x00\x01" else 3
                nxt = _find_start_code(buf, sc_len)
                if nxt < 0:
                    break
                nal = bytes(buf[:nxt])
                nal_type = buf[sc_len] & 0x1F
                del buf[:nxt]

                if nal_type in (7, 8):          # SPS / PPS
                    pending.extend(nal)
                    if nal_type == 7:
                        self._sps = nal
                    else:
                        self._pps = nal
                    if self._sps and self._pps:
                        self._h264_config = self._sps + self._pps
                elif nal_type == 6:             # SEI
                    pending.extend(nal)
                elif nal_type in (1, 5):        # slice non-IDR / IDR
                    is_key = nal_type == 5
                    au = bytearray()
                    if is_key and self._h264_config and not pending:
                        au.extend(self._h264_config)
                    au.extend(pending)
                    au.extend(nal)
                    pending.clear()

                    payload = (b"\x01" if is_key else b"\x00") + bytes(au)
                    if is_key:
                        self._last_keyframe = payload
                    self._distribute_frame(payload)
                    au_count += 1
                    if au_count == 1:
                        logs.success(f"Stream H264 attivo (primo frame {len(au)} bytes)", serial=self.serial)
                else:
                    pending.extend(nal)

        logs.info(f"Pipeline terminata ({au_count} frame)", serial=self.serial)
        return au_count

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _adb_exec(self, adb: str, *args: str, timeout: float = 30.0) -> str:
        async with adb_cmd_lock():
            proc = await asyncio.create_subprocess_exec(
                adb, *adb_server_args(self.serial), "-s", self.serial, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_SUBPROCESS_KW,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError("timeout ADB")
            if proc.returncode != 0:
                text = stdout.decode("utf-8", errors="replace").strip()
                if not text:
                    text = stderr.decode("utf-8", errors="replace").strip()
                if not text:
                    text = "errore ADB"
                raise RuntimeError(text)
            return stdout.decode("utf-8", errors="replace").strip()

    async def _connect_control(self) -> bool:
        """Apre il canale di controllo scrcpy sul forward gia' attivo.

        Il socket e' una seconda connessione TCP sulla stessa porta del
        tunnel adb. Se il tunnel ha un singhiozzo il socket locale resta
        'aperto' ma il peer e' morto: i write() non danno errore e i tap
        spariscono nel vuoto. Il keepalive TCP fa emergere il peer morto.
        """
        try:
            _, ctrl_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._tcp_port),
                timeout=5.0,
            )
            sock = ctrl_writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    if hasattr(socket, "TCP_KEEPIDLE"):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                    if hasattr(socket, "TCP_KEEPINTVL"):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                    if hasattr(socket, "TCP_KEEPCNT"):
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                except OSError:
                    pass
            self._control = ControlChannel(self.serial, ctrl_writer)
            logs.success("Canale di controllo attivo", serial=self.serial)
            return True
        except Exception as ctrl_exc:
            logs.warn(f"Canale di controllo non attivo: {ctrl_exc}", serial=self.serial)
            self._control = None
            return False

    async def ensure_control(self) -> Optional[ControlChannel]:
        """Ritorna il canale di controllo, riconnettendolo se e' caduto.

        Chiamato dal relay input quando un tap non trova canale: se il
        forward e il server sono ancora su, riapre il socket senza
        riavviare lo stream video.
        """
        if self._control and self._control.alive:
            return self._control
        if not self._running or not self._tcp_port or not self._writer:
            return None
        if self._writer.is_closing():
            return None
        if self._control:
            try:
                await self._control.close()
            except Exception:
                pass
            self._control = None
        logs.info("Canale di controllo caduto: riconnessione...", serial=self.serial)
        await self._connect_control()
        return self._control

    async def _remove_forward(self) -> None:
        if not self._tcp_port:
            return
        adb = self._settings.adb_path or "adb"
        try:
            await self._adb_exec(adb, "forward", "--remove", f"tcp:{self._tcp_port}")
        except Exception:
            pass
        self._tcp_port = 0

    async def _cleanup_server(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await asyncio.wait_for(self._watchdog_task, timeout=0.5)
            except Exception:
                pass
            self._watchdog_task = None
        if self._log_task:
            try:
                self._log_task.cancel()
                try:
                    await asyncio.wait_for(self._log_task, timeout=0.5)
                except Exception:
                    pass
            except Exception:
                pass
            self._log_task = None
        if self._control:
            try:
                await self._control.close()
            except Exception:
                pass
            self._control = None
        if self._writer:
            try:
                if not self._writer.is_closing():
                    self._writer.close()
                    try:
                        await asyncio.wait_for(
                            self._writer.wait_closed(), timeout=0.5
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            self._writer = None
        if self._server_proc:
            try:
                if self._server_proc.returncode is None:
                    self._server_proc.terminate()
                    try:
                        await asyncio.wait_for(self._server_proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        try:
                            self._server_proc.kill()
                        except Exception:
                            pass
            except ProcessLookupError:
                pass
            except Exception:
                pass
            self._server_proc = None
        self._sps = b""
        self._pps = b""
        self._h264_config = b""
        self._last_keyframe = None
        await self._remove_forward()

    async def _log_proc_output(self, proc: asyncio.subprocess.Process,
                                label: str, stderr_only: bool = False) -> None:
        async def read_stream(stream, tag):
            try:
                while stream and not stream.at_eof():
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        # Logga solo le righe significative per evitare saturazione
                        if tag == "server":
                            if any(k in text for k in ("ERROR", "FATAL", "Exception")):
                                logs.warn(f"{tag}: {text}", serial=self.serial, throttle_s=30)
                        else:
                            logs.info(f"{tag}: {text}", serial=self.serial, throttle_s=30)
                        self._on_server_output(text, tag)
            except Exception:
                pass

        tasks = []
        if proc.stderr:
            tasks.append(read_stream(proc.stderr, label))
        if not stderr_only and proc.stdout:
            tasks.append(read_stream(proc.stdout, f"{label}-out"))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _detect_native_resolution(self) -> None:
        # La risoluzione fisica non cambia tra un riavvio di stream e
        # l'altro: senza cache ogni ciclo spendeva un 'adb shell wm size'
        # sotto il lock globale, moltiplicato per tutti i device.
        cached = _NATIVE_SIZE_CACHE.get(self.serial)
        if cached:
            self._native_width, self._native_height = cached
            return
        adb = self._settings.adb_path or "adb"
        try:
            text = await self._adb_exec(adb, "shell", "wm", "size")
            for line in text.strip().splitlines():
                if "x" in line:
                    parts = line.split(":")[-1].strip().split("x")
                    if len(parts) == 2:
                        self._native_width = int(parts[0])
                        self._native_height = int(parts[1])
                        _NATIVE_SIZE_CACHE[self.serial] = (
                            self._native_width, self._native_height,
                        )
                        logs.info(f"Risoluzione nativa: {self._native_width}x{self._native_height}", serial=self.serial)
                        return
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Distribuzione frame
    # ------------------------------------------------------------------

    def _distribute_frame(self, frame: bytes) -> None:
        self._last_heartbeat = time.monotonic()
        # Keyframe H264 (flag 0x01): riallinea un client che ha perso frame.
        is_key = frame[:1] == b"\x01"
        dead: List[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                if q in self._desynced:
                    # Ha perso frame: i delta produrrebbero video corrotto,
                    # si riallinea solo sul prossimo keyframe.
                    if not is_key:
                        continue
                    self._desynced.discard(q)
                # Last-frame-wins: se la coda e' piena, svuotiamo e
                # inseriamo il frame piu' recente. Con maxsize=1 il client
                # riceve SEMPRE l'ultimo frame prodotto da scrcpy.
                while True:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                q.put_nowait(frame)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)
            self._desynced.discard(q)


class StreamManager:
    """Gestisce tutti gli stream attivi."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._streams: Dict[str, DeviceStream] = {}
        starts = max(1, settings.stream.max_concurrent_stream_starts)
        self._start_sem = asyncio.Semaphore(starts)
        self._device_overrides = load_device_overrides()

    @property
    def streams(self) -> Dict[str, DeviceStream]:
        return self._streams

    async def start_stream(
        self,
        serial: str,
        max_size_override: Optional[int] = None,
        max_fps_override: Optional[int] = None,
        bit_rate_override: Optional[int] = None,
    ) -> DeviceStream:
        ov = self._device_overrides.get(serial, {})
        if max_size_override is None and "max_size" in ov:
            max_size_override = ov["max_size"]
        if max_fps_override is None and "max_fps" in ov:
            max_fps_override = ov["max_fps"]
        if bit_rate_override is None and "bit_rate" in ov:
            bit_rate_override = ov["bit_rate"]

        if serial in self._streams:
            stream = self._streams[serial]
            if stream.alive:
                return stream
            # Conserva gli override per device (es. fullscreen attivo)
            if max_size_override is None:
                max_size_override = stream.max_size_override
            if max_fps_override is None:
                max_fps_override = stream.max_fps_override
            if bit_rate_override is None:
                bit_rate_override = stream.bit_rate_override
            await stream.stop()
        stream = DeviceStream(
            serial,
            self._settings,
            self._start_sem,
            max_size_override,
            max_fps_override,
            bit_rate_override,
        )
        self._streams[serial] = stream
        await stream.start()
        return stream

    async def set_device_stream_params(
        self,
        serial: str,
        max_size: Optional[int] = None,
        max_fps: Optional[int] = None,
        bit_rate: Optional[int] = None,
    ) -> Optional[DeviceStream]:
        """Cambia risoluzione/fps/bitrate di un singolo device riavviando il suo stream."""
        # Aggiorna e persiste gli override per device.
        ov = self._device_overrides.setdefault(serial, {})
        if max_size is not None:
            ov["max_size"] = max_size
        elif "max_size" in ov:
            del ov["max_size"]
        if max_fps is not None:
            ov["max_fps"] = max_fps
        elif "max_fps" in ov:
            del ov["max_fps"]
        if bit_rate is not None:
            ov["bit_rate"] = bit_rate
        elif "bit_rate" in ov:
            del ov["bit_rate"]
        if not ov:
            self._device_overrides.pop(serial, None)
        save_device_overrides(self._device_overrides)

        stream = self._streams.get(serial)
        if stream is not None:
            # Se nessun parametro cambia, non riavviare.
            current_size = stream.max_size_override or self._settings.stream.max_size
            current_fps = stream.max_fps_override or self._settings.stream.max_fps
            current_br = stream.bit_rate_override or self._settings.stream.bit_rate
            wanted_size = max_size if max_size is not None else stream.max_size_override
            wanted_fps = max_fps if max_fps is not None else stream.max_fps_override
            wanted_br = bit_rate if bit_rate is not None else stream.bit_rate_override
            if (
                (max_size is None or current_size == wanted_size)
                and (max_fps is None or current_fps == wanted_fps)
                and (bit_rate is None or current_br == wanted_br)
            ):
                stream.max_size_override = max_size
                stream.max_fps_override = max_fps
                stream.bit_rate_override = bit_rate
                return stream
            await stream.stop()
            self._streams.pop(serial, None)
        return await self.start_stream(
            serial,
            max_size_override=max_size,
            max_fps_override=max_fps,
            bit_rate_override=bit_rate,
        )

    async def set_device_max_size(
        self, serial: str, max_size: Optional[int]
    ) -> Optional[DeviceStream]:
        """Alias compatibilita' per chi chiama solo con max_size."""
        return await self.set_device_stream_params(serial, max_size=max_size)

    async def set_device_zoom(self, serial: str) -> Optional[DeviceStream]:
        """Entra in zoom fullscreen: 720p/15fps/500k per latenza minima.

        1080p@20fps satura troppo il decoder del client quando altri
        device sono aperti. 720p@15fps@500k e' il sweet spot: testo
        leggibile, latenza <200ms anche su GPU entry-level.
        """
        stream = self._streams.get(serial)
        if stream is None:
            return None
        stream._pre_zoom = (
            stream.max_size_override,
            stream.max_fps_override,
            stream.bit_rate_override,
        )
        return await self.set_device_stream_params(serial, max_size=720, max_fps=15, bit_rate=500_000)

    async def unset_device_zoom(self, serial: str) -> Optional[DeviceStream]:
        """Esce dallo zoom e ripristina i parametri precedenti."""
        stream = self._streams.get(serial)
        if stream is None:
            return None
        pre = stream._pre_zoom
        if pre is None:
            return stream
        stream._pre_zoom = None
        return await self.set_device_stream_params(serial, max_size=pre[0], max_fps=pre[1], bit_rate=pre[2])

    async def stop_stream(self, serial: str) -> None:
        if serial in self._streams:
            await self._streams[serial].stop()
            del self._streams[serial]

    async def restart_stream(
        self,
        serial: str,
        max_size_override: Optional[int] = None,
        max_fps_override: Optional[int] = None,
        bit_rate_override: Optional[int] = None,
    ) -> Optional[DeviceStream]:
        """Stop + start immediato: evita il race dei 600ms del frontend."""
        stream = self._streams.get(serial)
        if stream is not None:
            # Conserva gli override fullscreen/qualita' dello stream precedente
            if max_size_override is None:
                max_size_override = stream.max_size_override
            if max_fps_override is None:
                max_fps_override = stream.max_fps_override
            if bit_rate_override is None:
                bit_rate_override = stream.bit_rate_override
            await stream.stop()
            self._streams.pop(serial, None)
        return await self.start_stream(
            serial,
            max_size_override=max_size_override,
            max_fps_override=max_fps_override,
            bit_rate_override=bit_rate_override,
        )

    async def stop_all(self) -> None:
        for stream in list(self._streams.values()):
            await stream.stop()
        self._streams.clear()

    def get_stream(self, serial: str) -> Optional[DeviceStream]:
        return self._streams.get(serial)
