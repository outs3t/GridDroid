"""Motore di streaming: scrcpy-server standalone → TCP raw H264 → ffmpeg → JPEG."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import random
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PIL import Image

from .adb_manager import adb_cmd_lock, adb_server_args
from .config import AppSettings
from .control_channel import ControlChannel
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
_DEGRADE_MAX = 3  # livelli: 0=100%, 1=50% fps, 2=25% fps+meta' bitrate, 3=minimo


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
    ) -> None:
        self.serial = serial
        self._settings = settings
        # Risoluzione dedicata a questo device (fullscreen): se impostata
        # sovrascrive il max_size globale solo per questo stream.
        self.max_size_override = max_size_override
        # Limite avvii concorrenti per non sovraccaricare ADB (default 4)
        self._start_sem: Optional[asyncio.Semaphore] = start_sem
        self._last_heartbeat = time.monotonic()
        self._server_proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._current_frame: Optional[bytes] = None
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
    def last_frame(self) -> Optional[bytes]:
        return self._current_frame

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
        q: asyncio.Queue = asyncio.Queue(maxsize=60)
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
                try:
                    self._writer.close()
                except Exception:
                    pass

    async def _run(self) -> None:
        server_jar = _find_scrcpy_server()
        if server_jar:
            await self._run_scrcpy_server(server_jar)
        else:
            logs.warn("scrcpy-server non trovato", serial=self.serial)
            logs.info("Fallback a screenshot periodici", serial=self.serial)
            await self._screenshot_fallback()

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
                    try:
                        await self._adb_exec(
                            adb, "shell", "input", "keyevent", "KEYCODE_WAKEUP",
                            timeout=8.0,
                        )
                    except Exception:
                        pass

                    # 1. Push server jar
                    remote_jar = "/data/local/tmp/scrcpy-server.jar"
                    await self._adb_exec(adb, "push", server_jar, remote_jar, timeout=60.0)

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
                        f"show_touches=true stay_awake=true power_off_on_close=true "
                        f"raw_stream=true "
                        f"max_size={self.max_size_override or s.max_size} "
                        + self._adaptive_params(s)
                        # Keyframe ogni 2s: chi perde frame (rete lenta/VPN)
                        # si riallinea in fretta invece di restare corrotto.
                        + (
                            "video_codec_options=i-frame-interval=2 "
                            if self.serial not in self._iframe_unsupported else ""
                        )
                        + f"scid={scid_hex}"
                    )
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
                    try:
                        await asyncio.sleep(0.3)
                        _, ctrl_writer = await asyncio.wait_for(
                            asyncio.open_connection("127.0.0.1", self._tcp_port),
                            timeout=5.0,
                        )
                        self._control = ControlChannel(self.serial, ctrl_writer)
                        logs.success("Canale di controllo attivo", serial=self.serial)
                    except Exception as ctrl_exc:
                        logs.warn(f"Canale di controllo non attivo: {ctrl_exc}", serial=self.serial)
                        self._control = None

                # 6. Watchdog del processo server: se muore in silenzio col
                #    socket ancora aperto, sblocca la read e riavvia lo stream.
                self._watchdog_task = asyncio.create_task(self._watch_server_proc())

                # 7. Passthrough H264 → browser (decodifica hardware WebCodecs)
                au_count = await self._stream_h264(reader)
                if au_count > 0:
                    consecutive_failures = 0
                    # Stream stabile: riporta la qualita' al livello pieno.
                    if au_count > 300 and _DEGRADED.get(self.serial, 0) > 0:
                        _DEGRADED[self.serial] = 0
                        logs.info("Stream stabile: qualita' ripristinata", serial=self.serial)
                else:
                    consecutive_failures += 1
                    # Throttling adattivo: encoder che fallisce di continuo
                    # riceve fps/bitrate ridotti al prossimo tentativo.
                    level = _DEGRADED.get(self.serial, 0)
                    if level < _DEGRADE_MAX:
                        _DEGRADED[self.serial] = level + 1
                        logs.warn(
                            f"Encoder instabile: qualita' ridotta al livello {level + 1}",
                            serial=self.serial,
                        )
                    if self.serial not in self._iframe_unsupported:
                        # Encoder morto subito dopo il configure:
                        # i-frame-interval non digerito, si riprova senza.
                        self._iframe_unsupported.add(self.serial)
                        logs.warn(
                            "Encoder senza frame: disattivo i-frame-interval e riprovo",
                            serial=self.serial,
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
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
        """Parametri video scalati per il livello di degradazione corrente.

        Encoder instabili (timeout, 0 frame) ricevono fps e bitrate ridotti:
        meno lavoro per l'encoder hardware del telefono, piu' probabilita'
        che lo stream regga in setup densi.
        """
        level = _DEGRADED.get(self.serial, 0)
        fps_f, br_f = _degrade_factor(level)
        fps = max(5, int(s.max_fps * fps_f))
        br = max(500_000, int(s.bit_rate * br_f))
        return f"max_fps={fps} video_bit_rate={br} "

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
        try:
            while self._running:
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
        adb = self._settings.adb_path or "adb"
        try:
            text = await self._adb_exec(adb, "shell", "wm", "size")
            for line in text.strip().splitlines():
                if "x" in line:
                    parts = line.split(":")[-1].strip().split("x")
                    if len(parts) == 2:
                        self._native_width = int(parts[0])
                        self._native_height = int(parts[1])
                        logs.info(f"Risoluzione nativa: {self._native_width}x{self._native_height}", serial=self.serial)
                        return
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Fallback screenshot
    # ------------------------------------------------------------------

    async def _screenshot_fallback(self) -> None:
        adb = self._settings.adb_path or "adb"
        if self._native_width == 0:
            await self._detect_native_resolution()

        frame_count = 0
        while self._running:
            try:
                proc = await asyncio.create_subprocess_exec(
                    adb, *adb_server_args(self.serial),
                    "-s", self.serial, "exec-out", "screencap", "-p",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_SUBPROCESS_KW,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                if proc.returncode == 0 and stdout and len(stdout) > 100:
                    try:
                        img = Image.open(io.BytesIO(stdout))
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        max_size = self._settings.stream.max_size
                        if img.width > max_size or img.height > max_size:
                            ratio = max_size / max(img.width, img.height)
                            new_size = (int(img.width * ratio), int(img.height * ratio))
                            img = img.resize(new_size, Image.NEAREST)
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=85)
                        jpeg_bytes = buf.getvalue()
                    except Exception as img_exc:
                        logs.warn(f"Errore elaborazione immagine: {img_exc}", serial=self.serial)
                        jpeg_bytes = stdout
                    self._current_frame = jpeg_bytes
                    self._distribute_frame(jpeg_bytes)
                    frame_count += 1
                    if frame_count == 1:
                        logs.success(f"Primo frame screenshot ({len(jpeg_bytes)} bytes)", serial=self.serial)
                else:
                    err_msg = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
                    if frame_count == 0:
                        logs.warn(f"screencap fallito: {err_msg}", serial=self.serial)
            except asyncio.TimeoutError:
                logs.warn("Screenshot timeout", serial=self.serial)
            except Exception as exc:
                logs.warn(f"Errore screenshot: {exc}", serial=self.serial)
            await asyncio.sleep(0.05)

    def _distribute_frame(self, frame: bytes) -> None:
        self._last_heartbeat = time.monotonic()
        # Keyframe H264 (flag 0x01) o JPEG completo del fallback:
        # entrambi riallineano un client che ha perso frame.
        is_key = frame[:1] == b"\x01" or frame[:2] == b"\xff\xd8"
        dead: List[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                if q in self._desynced:
                    # Ha perso frame: i delta produrrebbero video corrotto,
                    # si riallinea solo sul prossimo keyframe.
                    if not is_key:
                        continue
                    self._desynced.discard(q)
                elif q.full():
                    # Scartare un solo delta corrompe il decoder del client
                    # fino al prossimo keyframe: meglio svuotare la coda
                    # e riallineare direttamente sul keyframe.
                    while True:
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    if not is_key:
                        self._desynced.add(q)
                        continue
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

    @property
    def streams(self) -> Dict[str, DeviceStream]:
        return self._streams

    async def start_stream(
        self, serial: str, max_size_override: Optional[int] = None
    ) -> DeviceStream:
        if serial in self._streams:
            stream = self._streams[serial]
            if stream.alive:
                return stream
            # Conserva la risoluzione dedicata (es. fullscreen attivo) quando
            # lo stream viene ricreato dopo una caduta
            if max_size_override is None:
                max_size_override = stream.max_size_override
            await stream.stop()
        stream = DeviceStream(
            serial, self._settings, self._start_sem, max_size_override
        )
        self._streams[serial] = stream
        await stream.start()
        return stream

    async def set_device_max_size(
        self, serial: str, max_size: Optional[int]
    ) -> Optional[DeviceStream]:
        """Cambia la risoluzione di un singolo device riavviando il suo stream.

        Serve al fullscreen: alla risoluzione della griglia l'immagine ingrandita
        risulterebbe sfocata perche' il browser fa upscaling del bitmap decodificato.
        """
        stream = self._streams.get(serial)
        if stream is not None:
            # Confronta la risoluzione EFFETTIVA (override o globale):
            # la qualita' adattiva puo' chiedere un valore uguale al
            # globale con override ancora None — senza questo check
            # riavviava tutti gli stream al primo giro.
            current = stream.max_size_override or self._settings.stream.max_size
            wanted = max_size or self._settings.stream.max_size
            if current == wanted:
                stream.max_size_override = max_size
                return stream
            await stream.stop()
            self._streams.pop(serial, None)
        return await self.start_stream(serial, max_size_override=max_size)

    async def stop_stream(self, serial: str) -> None:
        if serial in self._streams:
            await self._streams[serial].stop()
            del self._streams[serial]

    async def stop_all(self) -> None:
        for stream in list(self._streams.values()):
            await stream.stop()
        self._streams.clear()

    def get_stream(self, serial: str) -> Optional[DeviceStream]:
        return self._streams.get(serial)
