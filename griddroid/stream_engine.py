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
from pathlib import Path
from typing import Dict, List, Optional, Set

from PIL import Image

from .adb_manager import adb_cmd_lock
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
    ) -> None:
        self.serial = serial
        self._settings = settings
        self._start_sem = start_sem
        self._server_proc: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._current_frame: Optional[bytes] = None
        self._sps: bytes = b""
        self._pps: bytes = b""
        self._h264_config: bytes = b""
        self._last_keyframe: Optional[bytes] = None
        self._subscribers: Set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self._native_width: int = 0
        self._native_height: int = 0
        self._tcp_port: int = 0
        self._writer: Optional[asyncio.StreamWriter] = None
        self._control: Optional[ControlChannel] = None
        self._server_ready = asyncio.Event()
        self._server_error: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self._running

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
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._control:
            try:
                self._control.close()
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
                    adb, "-s", self.serial, "get-state",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    **_SUBPROCESS_KW,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                return stdout.decode("utf-8", errors="replace").strip() == "device"
            except Exception:
                return False

    async def _run_scrcpy_server(self, server_jar: str) -> None:
        adb = self._settings.adb_path or "adb"
        s = self._settings.stream

        await self._detect_native_resolution()

        consecutive_failures = 0

        while self._running:
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
                    # 1. Push server jar
                    remote_jar = "/data/local/tmp/scrcpy-server.jar"
                    await self._adb_exec(adb, "push", server_jar, remote_jar)

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
                    #    control=true abilita il control socket (input nativi).
                    #    I meta sono disattivati: il socket video porta H264 puro.
                    server_cmd = (
                        f"CLASSPATH={remote_jar} "
                        f"app_process / com.genymobile.scrcpy.Server {_SCRCPY_VERSION} "
                        f"tunnel_forward=true "
                        f"audio=false control=true cleanup=true "
                        f"send_device_meta=false send_frame_meta=false "
                        f"send_dummy_byte=false "
                        f"max_size={s.max_size} max_fps={s.max_fps} "
                        f"video_bit_rate={s.bit_rate} "
                        f"scid={scid_hex}"
                    )
                    self._server_proc = await asyncio.create_subprocess_exec(
                        adb, "-s", self.serial, "shell", server_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        **_SUBPROCESS_KW,
                    )
                    self._server_ready.clear()
                    self._server_error = None
                    asyncio.create_task(self._log_proc_output(self._server_proc, "server"))
                    logs.info(f"scrcpy-server avviato (porta {self._tcp_port})", serial=self.serial)

                    # Attende che il server logghi "INFO: Device:"
                    try:
                        await asyncio.wait_for(self._server_ready.wait(), timeout=6.0)
                    except asyncio.TimeoutError:
                        raise RuntimeError("scrcpy-server non pronto entro 6s")
                    if self._server_error:
                        raise RuntimeError(f"scrcpy-server: {self._server_error}")

                    # 4. Connessione video con retry (evita WinError 1225 per avvio troppo rapido)
                    last_exc: Optional[Exception] = None
                    for attempt in range(3):
                        try:
                            reader, self._writer = await asyncio.wait_for(
                                asyncio.open_connection("127.0.0.1", self._tcp_port),
                                timeout=3.0,
                            )
                            break
                        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
                            last_exc = exc
                            if attempt < 2:
                                await asyncio.sleep(0.5)
                    if reader is None or self._writer is None:
                        raise last_exc or RuntimeError("Connessione video TCP rifiutata")
                    logs.success("Connesso al video socket TCP", serial=self.serial)

                    # 5. Control socket: input nativi a latenza ~1ms.
                    for attempt in range(3):
                        try:
                            _, ctrl_writer = await asyncio.wait_for(
                                asyncio.open_connection("127.0.0.1", self._tcp_port),
                                timeout=3.0,
                            )
                            self._control = ControlChannel(self.serial, ctrl_writer)
                            logs.success("Canale di controllo attivo (input nativi)", serial=self.serial)
                            break
                        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
                            if attempt == 2:
                                logs.warn("Canale di controllo non disponibile", serial=self.serial)
                                self._control = None
                                break
                            await asyncio.sleep(0.3)

                # 6. Passthrough H264 → browser (decodifica hardware WebCodecs)
                au_count = await self._stream_h264(reader)
                if au_count > 0:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1

            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                logs.warn(f"Errore stream: {exc}", serial=self.serial)
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
                logs.info(f"Riconnessione stream tra {delay:.1f}s...", serial=self.serial)
                await asyncio.sleep(delay)

    async def _stream_h264(self, tcp_reader: asyncio.StreamReader) -> None:
        """Legge H264 Annex-B dal socket TCP, lo divide in access unit e le
        distribuisce ai subscriber. Il browser decodifica in hardware via WebCodecs.

        Formato messaggio: 1 byte flag (1 = keyframe, 0 = delta) + dati Annex-B.
        """
        buf = bytearray()
        pending = bytearray()   # NAL non-VCL (SPS/PPS/SEI) in attesa del frame
        synced = False
        au_count = 0

        while self._running:
            data = await tcp_reader.read(65536)
            if not data:
                logs.info(f"TCP stream chiuso ({au_count} frame)", serial=self.serial)
                break
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

    async def _adb_exec(self, adb: str, *args: str, timeout: float = 15.0) -> str:
        async with adb_cmd_lock():
            proc = await asyncio.create_subprocess_exec(
                adb, "-s", self.serial, *args,
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
        if self._control:
            try:
                self._control.close()
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
                        logs.info(f"{tag}: {text}", serial=self.serial)
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
                    adb, "-s", self.serial, "exec-out", "screencap", "-p",
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
        dead: List[asyncio.Queue] = []
        for q in self._subscribers:
            try:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(frame)
            except Exception:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)


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

    async def start_stream(self, serial: str) -> DeviceStream:
        if serial in self._streams:
            stream = self._streams[serial]
            if stream.alive:
                return stream
            await stream.stop()
        stream = DeviceStream(serial, self._settings, self._start_sem)
        self._streams[serial] = stream
        await stream.start()
        return stream

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
