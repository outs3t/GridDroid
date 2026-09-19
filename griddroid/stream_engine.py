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

from .adb_manager import (
    adb_binary_for_serial,
    adb_server_args,
    get_adb_cmd_lock,
    run_proc,
)
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

# Righe stdout di scrcpy-server puramente di routine: vengono ripetute a
# ogni avvio stream e a ogni reset_video (re-init cattura + encoder) e
# non portano informazione diagnostica. Filtrarle dal log riduce il
# fan-out websocket verso la UI, che a sua volta allevia l'event loop.
_SERVER_STDOUT_NOISE: Tuple[str, ...] = (
    "Display: using DisplayManager API",
    "Using video encoder",
    "Using codec option",
    "Display size set to",
    "Size alignment",
)


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
    """Posizione del prossimo start code Annex-B (00 00 01 o 00 00 00 01).

    find() gira in C: il vecchio loop Python scandiva ~1M byte/s per
    stream sull'event loop (~30-50ms di CPU/s per device a 8Mbps) —
    con 25 device era il maggior consumo CPU del processo.
    Il codice a 4 byte 00 00 00 01 contiene 00 00 01 all'offset 1, quindi
    il primo match di find e' sempre il codice piu' precoce; se il byte
    prima del match e' 0 il vero inizio e' p-1 (forma a 4 byte).
    """
    p = b.find(b"\x00\x00\x01", start)
    if p < 0:
        return -1
    if p > start and b[p - 1] == 0:
        return p - 1
    return p


def _find_scrcpy_server() -> Optional[str]:
    bundled = _tools_dir() / "scrcpy-server"
    if bundled.exists():
        return str(bundled)
    return None


def find_ffmpeg(adb_path: str = "") -> Optional[str]:
    """Cerca ffmpeg: accanto ad adb.exe, nella cartella tools, poi nel PATH."""
    if adb_path:
        beside = Path(adb_path).with_name("ffmpeg.exe")
        if beside.exists():
            return str(beside)
    bundled = _tools_dir() / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if bundled.exists():
        return str(bundled)
    found = shutil.which("ffmpeg")
    if found:
        return found
    return None


def _split_jpegs(buf: bytearray) -> List[bytes]:
    """Estrae i JPEG completi dal buffer (SOI FFD8 .. EOI FFD9).

    ffmpeg in modalita' image2pipe/mjpeg emette un JPEG completo per frame;
    le letture da pipe arrivano a blocchi arbitrari, quindi i confini vanno
    cercati nel byte-stream accumulato. Modifica buf in place.
    """
    frames: List[bytes] = []
    while True:
        soi = buf.find(b"\xff\xd8")
        if soi < 0:
            # Nessun SOI: conserva solo un eventuale 0xff in coda,
            # potrebbe essere l'inizio di un SOI spezzato tra due read.
            if buf[-1:] == b"\xff":
                del buf[:-1]
            else:
                del buf[:]
            break
        eoi = buf.find(b"\xff\xd9", soi + 2)
        if eoi < 0:
            del buf[:soi]
            break
        frames.append(bytes(buf[soi:eoi + 2]))
        del buf[:eoi + 2]
    return frames


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
        # Frame delta distribuiti dopo l'ultimo keyframe: se > 0 il keyframe
        # in cache e' vecchio e non basta a far partire un nuovo client.
        self._frames_since_key: int = 0
        self._last_key_request: float = 0.0
        # Reset keyframe ravvicinati: oltre i primi tentativi si passa a un
        # ritmo piu' lento (un reset al secondo affossa l'encoder senza
        # salvare un client cronicamente lento).
        self._key_request_burst: int = 0
        self._subscribers: Set[asyncio.Queue] = set()
        # Code che hanno perso frame: ricevono solo keyframe finche' non si riallineano
        self._desynced: Set[asyncio.Queue] = set()
        # Transcoder JPEG server-side (modalita' compatibile): ffmpeg
        # decodifica l'H264 e manda JPEG pronti ai subscriber.
        self._jpeg_subs: Set[asyncio.Queue] = set()
        self._jpeg_proc: Optional[asyncio.subprocess.Process] = None
        self._jpeg_task: Optional[asyncio.Task] = None
        self._jpeg_stop_task: Optional[asyncio.Task] = None
        # Coda AU -> stdin di ffmpeg + task feeder: il loop _stream_h264
        # non deve MAI attendere il drain di ffmpeg (un encoder lento
        # frenava la distribuzione video a tutti i subscriber).
        self._jpeg_inq: Optional[asyncio.Queue] = None
        self._jpeg_feeder: Optional[asyncio.Task] = None
        # True dal primo keyframe inviato a ffmpeg: prima di un IDR il
        # decoder h264 non puo' produrre nulla.
        self._jpeg_feed_ok: bool = False
        # Device il cui encoder crasha con i-frame-interval (0 frame):
        # set a livello di modulo perche' l'auto-stream ricrea DeviceStream
        # a ogni tentativo e una variabile locale si resetterebbe.
        self._iframe_unsupported = _IFRAME_UNSUPPORTED
        self._degrade_level = _DEGRADED.get(serial, 0)
        self._task: Optional[asyncio.Task] = None
        self._log_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._control_monitor_task: Optional[asyncio.Task] = None
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

    async def set_display_power(self, on: bool) -> bool:
        """Spegne/accende il pannello senza bloccare il device.

        False se lo stream non e' attivo o il canale di controllo e' giu':
        il chiamante ricade su input keyevent (che pero' blocca il device).
        """
        ctrl = self.control
        if ctrl is None:
            return False
        return await ctrl.set_display_power(on)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_heartbeat = time.monotonic()
        self._task = asyncio.create_task(self._run())
        self._control_monitor_task = asyncio.create_task(self._monitor_control())

    async def stop(self) -> None:
        self._running = False
        # Sblocca subito i subscriber: il WS video deve chiudersi anche se
        # la task e' gia' morta o impiega tempo a terminare.
        self._signal_stream_end()
        if self._jpeg_stop_task:
            self._jpeg_stop_task.cancel()
            self._jpeg_stop_task = None
        if self._jpeg_task:
            self._jpeg_task.cancel()
            try:
                await asyncio.wait_for(self._jpeg_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._jpeg_task = None
        await self._stop_jpeg_transcoder()
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
        if self._control_monitor_task:
            self._control_monitor_task.cancel()
            try:
                await asyncio.wait_for(self._control_monitor_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            self._control_monitor_task = None

    def subscribe(self) -> asyncio.Queue:
        # Buffer di 30 frame (~2s a 15fps, ~60KB a 300kbps): uno stallo
        # breve del loop o del browser non deve mai costare un desync —
        # ogni desync forza un keyframe, e ogni keyframe costa un
        # reset_video ovvero una re-init completa di cattura+encoder sul
        # telefono. Con 25+ device gli stalli periodici (letture saldi,
        # raffiche adb) producevano reset di massa a cadenza fissa.
        q: asyncio.Queue = asyncio.Queue(maxsize=30)
        self._subscribers.add(q)
        # Partenza immediata solo se il keyframe in cache e' ancora l'ultimo
        # frame prodotto. Altrimenti keyframe vecchio + delta recenti = video
        # corrotto o decoder in errore: si aspetta un keyframe fresco.
        if self._last_keyframe and self._frames_since_key == 0:
            q.put_nowait(self._last_keyframe)
        else:
            self._desynced.add(q)
            # Nuovo subscriber: bypassa il slow-mode, non puo' aspettare
            # 10s per il primo frame (resta il throttle di 1s).
            self.request_keyframe(force=True)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        self._desynced.discard(q)

    def request_keyframe(self, force: bool = False) -> None:
        """Chiede all'encoder un nuovo keyframe (throttle adattivo).

        Senza i-frame-interval MediaCodec emette IDR solo all'avvio: un client
        che perde anche un solo delta resterebbe congelato per sempre.
        reset_video non e' gratis: scrcpy reinizializza tutta la cattura
        (DisplayManager + encoder). Se un client si desincronizza di
        continuo, un reset al secondo non lo salva e penalizza tutti gli
        altri subscriber: dopo 3 richieste ravvicinate passiamo a un reset
        ogni ~10s, finche' 30s di calma non riportano al ritmo veloce.
        force=True (nuovo subscriber) salta il slow-mode ma non il throttle
        base di 1s.
        """
        ctrl = self.control
        if ctrl is None:
            return
        now = time.monotonic()
        if now - self._last_key_request < 1.0:
            return
        if not force:
            if now - self._last_key_request > 30.0:
                self._key_request_burst = 0
            if self._key_request_burst >= 3 and now - self._last_key_request < 10.0:
                return
            self._key_request_burst += 1
        self._last_key_request = now
        asyncio.create_task(ctrl.reset_video())

    # ------------------------------------------------------------------
    # Transcoder JPEG server-side (modalita' compatibile)
    # ------------------------------------------------------------------

    def subscribe_jpeg(self) -> asyncio.Queue:
        """Coda da 1 JPEG: last-wins come i frame H264. Al primo subscriber
        parte ffmpeg; all'ultimo unsubscribe si ferma dopo 5s di grazia."""
        if self._jpeg_stop_task:
            self._jpeg_stop_task.cancel()
            self._jpeg_stop_task = None
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._jpeg_subs.add(q)
        if self._jpeg_proc is None or self._jpeg_proc.returncode is not None:
            if self._jpeg_task is None or self._jpeg_task.done():
                self._jpeg_task = asyncio.create_task(self._run_jpeg_transcoder())
        return q

    def unsubscribe_jpeg(self, q: asyncio.Queue) -> None:
        self._jpeg_subs.discard(q)
        if not self._jpeg_subs and self._jpeg_proc is not None:
            async def _delayed_stop():
                try:
                    await asyncio.sleep(5.0)
                    if not self._jpeg_subs:
                        await self._stop_jpeg_transcoder()
                except asyncio.CancelledError:
                    pass
            self._jpeg_stop_task = asyncio.create_task(_delayed_stop())

    async def _run_jpeg_transcoder(self) -> None:
        """Avvia ffmpeg (H264 da stdin -> JPEG su stdout) e legge l'output."""
        ffmpeg = find_ffmpeg(self._settings.adb_path)
        if not ffmpeg:
            logs.warn("ffmpeg non trovato: modalita' JPEG non disponibile",
                      serial=self.serial)
            return
        s = self._settings.stream
        size = s.jpeg_max_size
        # Scala il lato lungo a jpeg_max_size, mantenendo il rapporto e
        # dimensioni pari (richieste da mjpeg). Le virgole dentro if()
        # vanno escapate per il parser dei filtri ffmpeg.
        vf = (
            f"fps={s.jpeg_fps},"
            f"scale=if(gt(iw\\,ih)\\,min(iw\\,{size})\\,-2)"
            f":if(gt(iw\\,ih)\\,-2\\,min(ih\\,{size}))"
        )
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            # Decode in hardware quando possibile (D3D11VA/DXVA2/NVDEC):
            # stessa scelta di Panda (decode nativo fuori dal browser).
            # Se la GPU non supporta il formato, ffmpeg torna a software
            # da solo — hwaccel auto non fallisce mai.
            "-hwaccel", "auto",
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "h264", "-i", "pipe:0",
            "-vf", vf,
            "-q:v", str(s.jpeg_quality),
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ]
        try:
            self._jpeg_proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                **_SUBPROCESS_KW,
            )
        except Exception as exc:
            logs.warn(f"ffmpeg non avviabile: {exc}", serial=self.serial)
            self._jpeg_proc = None
            return
        self._jpeg_feed_ok = False
        logs.info(f"Transcoder JPEG avviato ({s.jpeg_fps}fps/{size}px)",
                  serial=self.serial)
        # Coda + feeder separato: la scrittura su stdin (drain lento se
        # ffmpeg e' in backlog) avviene fuori dal loop di distribuzione.
        # ~6s di AU a 15fps: oltre, il transcoder viene considerato
        # incapace di reggere e fermato.
        self._jpeg_inq = asyncio.Queue(maxsize=90)
        self._jpeg_feeder = asyncio.create_task(self._jpeg_stdin_feeder())
        # Se il keyframe in cache e' ancora l'ultimo frame prodotto, lo
        # diamo subito a ffmpeg: il primo JPEG arriva senza attendere il
        # prossimo IDR dell'encoder.
        if self._last_keyframe and self._frames_since_key == 0:
            self._jpeg_inq.put_nowait((True, self._last_keyframe[1:]))
        # Riferimento locale: _stop_jpeg_transcoder puo' azzerare
        # self._jpeg_proc mentre il loop legge — senza questo scattava
        # AttributeError su .stdout di None.
        proc = self._jpeg_proc
        try:
            buf = bytearray()
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                for jpeg in _split_jpegs(buf):
                    for q in list(self._jpeg_subs):
                        try:
                            if q.full():
                                q.get_nowait()
                            q.put_nowait(jpeg)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logs.warn(f"Transcoder JPEG: {exc}", serial=self.serial)
        finally:
            if self._jpeg_feeder is not None:
                self._jpeg_feeder.cancel()
                self._jpeg_feeder = None
            self._jpeg_inq = None
            await self._stop_jpeg_transcoder()

    async def _feed_jpeg(self, is_key: bool, au: bytes) -> None:
        """Accoda un'access unit al feeder di ffmpeg — MAI bloccante.

        Se la coda e' piena ffmpeg non smaltisce il flusso: meglio
        fermare il transcoder che far attendere il loop video.
        """
        proc = self._jpeg_proc
        q = self._jpeg_inq
        if proc is None or q is None:
            return
        try:
            q.put_nowait((is_key, au))
        except asyncio.QueueFull:
            logs.warn("Transcoder JPEG in backlog: fermato", serial=self.serial)
            asyncio.create_task(self._stop_jpeg_transcoder())

    async def _jpeg_stdin_feeder(self) -> None:
        """Consuma la coda e scrive le AU nello stdin di ffmpeg."""
        q = self._jpeg_inq
        try:
            while q is not None:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Nessuna AU per 30s: schermo statico, tutto normale.
                    continue
                is_key, au = item
                proc = self._jpeg_proc
                if proc is None or proc.returncode is not None or proc.stdin is None:
                    return
                if not self._jpeg_feed_ok:
                    if not is_key:
                        # Senza un IDR iniziale il decoder h264 non decodifica.
                        continue
                    # SPS/PPS davanti al primo keyframe se non gia' inclusi.
                    if self._h264_config and self._h264_config not in au:
                        proc.stdin.write(self._h264_config)
                    self._jpeg_feed_ok = True
                proc.stdin.write(au)
                # Timeout sul drain: un ffmpeg inceppato viene fermato —
                # qui possiamo attendere, siamo fuori dal loop video.
                try:
                    await asyncio.wait_for(proc.stdin.drain(), timeout=5.0)
                except asyncio.TimeoutError:
                    logs.warn("Transcoder JPEG lento: fermato", serial=self.serial)
                    await self._stop_jpeg_transcoder()
                    return
        except asyncio.CancelledError:
            pass
        except (BrokenPipeError, ConnectionError, OSError):
            await self._stop_jpeg_transcoder()
        except Exception:
            pass

    async def _stop_jpeg_transcoder(self) -> None:
        proc = self._jpeg_proc
        self._jpeg_proc = None
        self._jpeg_feed_ok = False
        # Ferma il feeder: senza processo non ha piu' senso consumare.
        # Guardia: se lo chiama il feeder stesso (drain timeout), non
        # auto-cancellarsi — altrimenti lo stop viene interrotto a meta'.
        cur = asyncio.current_task()
        if self._jpeg_feeder is not None and self._jpeg_feeder is not cur:
            self._jpeg_feeder.cancel()
        self._jpeg_feeder = None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            except (ProcessLookupError, Exception):
                pass
        # Sveglia i subscriber JPEG: stream/transcoder terminato.
        for q in list(self._jpeg_subs):
            try:
                if q.full():
                    q.get_nowait()
                q.put_nowait(None)
            except Exception:
                pass

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
        # Anche i subscriber JPEG devono sbloccarsi a fine stream.
        for q in list(self._jpeg_subs):
            try:
                if q.full():
                    q.get_nowait()
                q.put_nowait(None)
            except Exception:
                pass

    async def _is_device_online(self) -> bool:
        adb = adb_binary_for_serial(
            self.serial, self._settings.adb_path or "adb"
        )
        if not adb:
            return False
        async with get_adb_cmd_lock():
            try:
                # Timeout corto: un device appeso non deve tenere il lock
                # adb globale per 30s bloccando il polling di tutti.
                _, stdout, _ = await run_proc(
                    [adb, *adb_server_args(self.serial),
                     "-s", self.serial, "get-state"],
                    8.0,
                )
                return stdout.decode("utf-8", errors="replace").strip() == "device"
            except Exception:
                return False

    async def _run_scrcpy_server(self, server_jar: str) -> None:
        adb = adb_binary_for_serial(
            self.serial, self._settings.adb_path or "adb"
        )
        if not adb:
            logs.warn(
                "Device su server adb esterno: binario proprietario non "
                "trovato, stream impossibile",
                serial=self.serial,
            )
            return
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
        """Parametri video con override per device e bitrate assoluto."""
        max_fps = self.max_fps_override or s.max_fps
        max_size = self.max_size_override or s.max_size
        # bit_rate e' il bitrate reale dell'encoder, scelto dall'utente.
        # Encoder: default hardware per latenza minima. Se l'hw di un
        # dispositivo e' gia' crashato, torniamo a software per quel
        # seriale. Tetto piu' basso in sw per non sovraccaricare.
        use_software = (
            getattr(s, "software_encoder", False)
            or self.serial in _HW_ENCODER_FAILED
        )
        max_bit_rate = 1_000_000 if use_software else 20_000_000
        bit_rate = max(
            min(self.bit_rate_override or s.bit_rate, max_bit_rate),
            50_000,
        )
        params = f"max_fps={max_fps} video_bit_rate={bit_rate} "
        # Nessun B-frame: il decoder non deve riordinare, ogni frame e'
        # mostrabile appena arriva (stessa opzione usata da Okto).
        # profile:int=1 = AVC Baseline (MediaFormat.KEY_PROFILE): profilo
        # universale per MSE/WebCodecs — alcuni client rifiutano High
        # (avc1.64xxxx). Se l'encoder ignora l'opzione resta High, che
        # funziona comunque; il client ricava il codec dall'SPS.
        params += "video_codec_options=max-bframes:int=0,profile:int=1 "
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

                    # bytes + bytearray = una sola copia (non due come
                    # flag + bytes(au)): ~12MB/s di copie risparmiati a
                    # 25 device x 20fps x ~50KB/frame.
                    payload = (b"\x01" if is_key else b"\x00") + au
                    if is_key:
                        self._last_keyframe = payload
                    self._distribute_frame(payload)
                    if self._jpeg_proc is not None:
                        await self._feed_jpeg(is_key, au)
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
        if not adb:
            raise RuntimeError("binario adb esterno non trovato")
        async with get_adb_cmd_lock():
            try:
                rc, stdout, stderr = await run_proc(
                    [adb, *adb_server_args(self.serial), "-s", self.serial, *args],
                    timeout,
                )
            except asyncio.TimeoutError:
                raise RuntimeError("timeout ADB")
            if rc != 0:
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

    async def _monitor_control(self) -> None:
        """Controlla ogni 5s che il canale di controllo sia attivo e lo
        riconnette in background se e' caduto. Riduce la latenza dei click
        perche' il canale e' gia' pronto quando serve."""
        while self._running:
            try:
                await asyncio.sleep(5.0)
                if not self._control or not self._control.alive:
                    if self._running and self._tcp_port and self._writer:
                        logs.info(
                            "Canale di controllo spento: riconnessione...",
                            serial=self.serial,
                            throttle_s=30,
                        )
                        try:
                            await self.ensure_control()
                        except Exception as exc:
                            logs.warn(f"Riconnessione controllo fallita: {exc}", serial=self.serial)
            except Exception as exc:
                logs.warn(f"Monitor controllo: {exc}", serial=self.serial)

    async def _remove_forward(self) -> None:
        if not self._tcp_port:
            return
        adb = adb_binary_for_serial(
            self.serial, self._settings.adb_path or "adb"
        )
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
        self._frames_since_key = 0
        # Il transcoder JPEG (se attivo) deve ripartire dal prossimo IDR
        # del nuovo stream: i delta di una sessione diversa non decodificano.
        self._jpeg_feed_ok = False
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
                        elif not any(k in text for k in _SERVER_STDOUT_NOISE):
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
        adb = adb_binary_for_serial(
            self.serial, self._settings.adb_path or "adb"
        )
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
        self._frames_since_key = 0 if is_key else self._frames_since_key + 1
        dead: List[asyncio.Queue] = []
        need_key = False
        for q in self._subscribers:
            try:
                if q in self._desynced:
                    # Ha perso frame: i delta produrrebbero video corrotto,
                    # si riallinea solo sul prossimo keyframe.
                    if not is_key:
                        need_key = True
                        continue
                    self._desynced.discard(q)
                try:
                    q.put_nowait(frame)
                except asyncio.QueueFull:
                    # Backlog oltre il buffer (3 frame): last-frame-wins,
                    # si svuota e si tiene solo il piu' recente. I frame
                    # scartati rompono la catena dei delta: il client non
                    # puo' piu' decodificare, serve un keyframe.
                    while True:
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    q.put_nowait(frame)
                    if not is_key:
                        self._desynced.add(q)
                        need_key = True
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)
            self._desynced.discard(q)
        if need_key:
            self.request_keyframe()


class StreamManager:
    """Gestisce tutti gli stream attivi."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._streams: Dict[str, DeviceStream] = {}
        starts = max(1, settings.stream.max_concurrent_stream_starts)
        self._start_sem = asyncio.Semaphore(starts)
        self._device_overrides = load_device_overrides()
        # Modello Panda: un solo device "in focus" (fullscreen) gira al
        # tier focus_*; gli altri restano sul profilo griglia. Non
        # persistito: e' uno stato di sessione, non un override manuale.
        self._focused_serial: Optional[str] = None
        self._focus_gen = 0
        # Seriali da riavviare al prossimo apply: accumulati tra chiamate
        # ravvicinate cosi' un toggle A→B→exit non lascia A ferma ai
        # parametri focus (stale) quando il debounce annulla i restart.
        self._focus_dirty: Set[str] = set()

    def reload_overrides(self) -> None:
        """Ricarica gli override video per device da disco (dopo un import)."""
        self._device_overrides = load_device_overrides()

    def remove_device_override(self, serial: str) -> None:
        """Dimentica gli override stream di un device eliminato."""
        if self._device_overrides.pop(serial, None) is not None:
            save_device_overrides(self._device_overrides)

    @property
    def streams(self) -> Dict[str, DeviceStream]:
        return self._streams

    def ffmpeg_available(self) -> bool:
        """True se un ffmpeg utilizzabile per la modalita' JPEG esiste."""
        return find_ffmpeg(self._settings.adb_path) is not None

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

        # Tier focus: se questo e' il device in fullscreen i campi
        # focus_* (>0) vincono su override manuali e profilo griglia.
        if serial == self._focused_serial:
            fs = self._settings.stream
            if fs.focus_max_size:
                max_size_override = fs.focus_max_size
            if fs.focus_max_fps:
                max_fps_override = fs.focus_max_fps
            if fs.focus_bit_rate:
                bit_rate_override = fs.focus_bit_rate

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

    async def set_stream_focus(self, serial: str, focused: bool) -> None:
        """Sposta il tier focus su `serial` (fullscreen) o lo toglie.

        Modello Panda: un solo stream a qualita' piena, gli altri su
        profilo griglia. Il restart e' debounced: toggle ravvicinati
        (enter/exit fullscreen in rapida sequenza) annullano il ciclo
        precedente invece di riavviare a cascata.
        """
        fs = self._settings.stream
        if not (fs.focus_max_size or fs.focus_max_fps or fs.focus_bit_rate):
            # Tier focus non configurato: funzione disattivata, il
            # fullscreen non provoca nessun restart dello stream.
            self._focused_serial = None
            return
        if focused:
            if self._focused_serial == serial:
                return
            old = self._focused_serial
            self._focused_serial = serial
            targets = [s for s in (old, serial) if s]
        else:
            if self._focused_serial != serial:
                return
            self._focused_serial = None
            targets = [serial]
        self._focus_dirty.update(targets)
        self._focus_gen += 1
        gen = self._focus_gen

        async def _apply() -> None:
            await asyncio.sleep(0.6)
            if gen != self._focus_gen:
                return
            targets = list(self._focus_dirty)
            self._focus_dirty.clear()
            for s in targets:
                st = self._streams.pop(s, None)
                if st is None:
                    # Device non in streaming: non avviarlo da qui.
                    continue
                try:
                    await st.stop()
                except Exception:
                    pass
                try:
                    await self.start_stream(s)
                    logs.info(
                        f"Stream {'focus' if focused and s == serial else 'griglia'}: "
                        f"riavviato ({s})",
                        serial=s,
                    )
                except Exception as exc:
                    logs.warn(f"Restart stream focus fallito: {exc}", serial=s)

        asyncio.create_task(_apply())

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
