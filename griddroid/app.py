"""FastAPI application – REST + WebSocket per GridDroid."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .adb_manager import AdbManager
from .bulk_actions import BulkActionRunner
from .config import AppSettings, load_settings, save_settings, load_labels, load_tags, load_played, load_known
from .device import DeviceStatus
from .input_relay import InputRelay
from .log_manager import logs
from . import __version__, startup, updater
from .scripts import ScriptEngine
from .stream_engine import StreamManager


def _get_local_ips() -> List[str]:
    """Restituisce gli indirizzi IPv4 locali (escluso loopback)."""
    try:
        host = socket.gethostname()
        infos = socket.getaddrinfo(host, None, socket.AF_INET)
        ips = {info[4][0] for info in infos if not info[4][0].startswith("127.")}
        return sorted(ips)
    except Exception:
        return []


def _add_firewall_rule(port: int) -> tuple[bool, str]:
    """Crea una regola firewall in ingresso per la porta TCP data.

    Se l'app non e' in esecuzione come amministratore, tenta di
    elevare tramite UAC con ShellExecute + powershell.
    """
    if sys.platform != "win32":
        return True, "Solo Windows richiede regole firewall"

    rule_name = "GridDroid"
    add_cmd = (
        f'netsh advfirewall firewall add rule name={rule_name} '
        f'dir=in action=allow protocol=tcp localport={port}'
    )
    delete_cmd = f'netsh advfirewall firewall delete rule name={rule_name}'

    # Prova diretta (funziona se gia' admin)
    combined = f"{delete_cmd}; {add_cmd}"
    try:
        proc = subprocess.run(
            ["powershell", "-WindowStyle", "Hidden", "-Command", combined],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0:
            return True, "Regola firewall aggiunta"
    except Exception as exc:
        return False, f"Errore: {exc}"

    # Se non abbiamo i permessi, prova a chiedere l'elevazione UAC
    try:
        command = f"{delete_cmd}; {add_cmd}"
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            "powershell.exe",
            f'-WindowStyle Hidden -Command "{command}"',
            None,
            0,
        )
        if result <= 32:
            return False, "Impossibile richiedere privilegi amministratore"
        return True, "Richiesta UAC inviata. Clicca 'Sì' su Windows, poi aggiorna."
    except Exception as exc:
        return False, f"Errore UAC: {exc}"


def create_app(settings: Optional[AppSettings] = None) -> FastAPI:
    if settings is None:
        settings = load_settings()

    app = FastAPI(title="GridDroid", version="0.1.0")

    # Protezione CSRF / Origin: accettiamo richieste dalla stessa origine.
    # L'origine valida e' dedotta dall'header Host della richiesta, cosi'
    # funziona sia in locale (127.0.0.1/localhost) sia da un altro PC
    # che accede tramite l'IP del server.
    _allowed_hosts = {"127.0.0.1", "localhost"}
    if settings.host and settings.host not in ("0.0.0.0", "::"):
        _allowed_hosts.add(settings.host)
    _allowed_origins = {f"http://{h}:{settings.port}" for h in _allowed_hosts}

    def _origin_allowed(origin: Optional[str], host: Optional[str]) -> bool:
        if not origin:
            return True
        if origin in _allowed_origins:
            return True
        if host:
            if origin == f"http://{host}" or origin == f"https://{host}":
                return True
        return False

    @app.middleware("http")
    async def _origin_middleware(request: Request, call_next):
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if origin and not _origin_allowed(origin, host):
            return JSONResponse({"error": "origine non consentita"}, status_code=403)
        return await call_next(request)

    # Servizi
    adb = AdbManager(settings)
    streams = StreamManager(settings)
    input_relay = InputRelay(adb, streams)
    bulk = BulkActionRunner(adb, max_concurrent=settings.max_concurrent_installs)
    script_engine = ScriptEngine(adb)

    # Salva riferimenti nell'app state
    app.state.adb = adb
    app.state.streams = streams
    app.state.input_relay = input_relay
    app.state.bulk = bulk
    app.state.scripts = script_engine
    app.state.settings = settings
    app.state.update_state = {
        "status": "idle",
        "percent": 0,
        "error": None,
        "version": None,
        "download_url": None,
        "silent_args": [],
        "installer": None,
    }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @app.on_event("startup")
    async def startup() -> None:
        logs.info("GridDroid avviato")
        await adb.start()
        app.state.auto_stream_task = asyncio.create_task(_auto_stream_loop())

        # Proactor Windows: evita crash da OSError non gestiti su socket chiusi
        def _proactor_exc_handler(loop, context):
            exc = context.get("exception")
            if isinstance(exc, OSError):
                # connessione rifiutata/resettata: gia' gestita dai task stream
                return
            loop.default_exception_handler(context)

        asyncio.get_running_loop().set_exception_handler(_proactor_exc_handler)

    async def _auto_stream_loop() -> None:
        """Avvia automaticamente lo stream per i dispositivi online e lo riavvia se cade."""
        await asyncio.sleep(3)
        logs.info("Auto-stream loop attivo")
        while True:
            try:
                devices_snapshot = list(adb.devices.items())
                now = time.time()
                for serial, dev in devices_snapshot:
                    # Se il frontend pensa che lo stream sia attivo ma il motore
                    # e morto, resettiamo lo stato cosi il loop lo riavvia.
                    if dev.streaming:
                        stream = streams.get_stream(serial)
                        if stream is None or not stream.alive:
                            dev.streaming = False
                            dev.stream_failures += 1
                            dev.next_stream_attempt = now + min(
                                2 ** dev.stream_failures, 120
                            )
                            if stream is not None:
                                try:
                                    await streams.stop_stream(serial)
                                except Exception:
                                    pass

                    # Se il dispositivo e offline/unauthorized/disconnesso,
                    # fermo subito lo stream per evitare tentativi infiniti.
                    if dev.status != DeviceStatus.ONLINE and dev.streaming:
                        logs.info("Dispositivo non online, interrompo stream", serial=serial)
                        dev.streaming = False
                        try:
                            await streams.stop_stream(serial)
                        except Exception:
                            pass
                        continue

                    # Dispositivi segnati come giocati vengono nascosti e non riavviati
                    if dev.played:
                        if dev.streaming:
                            logs.info("Dispositivo giocato, interrompo stream", serial=serial)
                            dev.streaming = False
                            try:
                                await streams.stop_stream(serial)
                            except Exception:
                                pass
                        continue

                    if dev.status == DeviceStatus.ONLINE and not dev.streaming:
                        if now < dev.next_stream_attempt:
                            continue
                        try:
                            logs.info("Avvio stream...", serial=serial)
                            await streams.start_stream(serial)
                            dev.streaming = True
                            dev.stream_failures = 0
                            dev.next_stream_attempt = 0.0
                            logs.success("Stream attivo", serial=serial)
                        except Exception as exc:
                            dev.streaming = False
                            dev.stream_failures += 1
                            dev.next_stream_attempt = now + min(
                                2 ** dev.stream_failures, 120
                            )
                            logs.error(f"Errore avvio stream: {exc}", serial=serial, throttle_s=30)
            except Exception as exc:
                logs.warn(f"Errore auto-stream loop: {exc}", throttle_s=30)
            await asyncio.sleep(3)

    @app.on_event("shutdown")
    async def shutdown() -> None:
        if hasattr(app.state, "auto_stream_task"):
            app.state.auto_stream_task.cancel()
            try:
                await asyncio.wait_for(app.state.auto_stream_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        await streams.stop_all()
        await adb.stop()
        logs.info("GridDroid fermato")

    # ------------------------------------------------------------------
    # Static files
    # ------------------------------------------------------------------

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        index_file = static_dir / "index.html"
        return index_file.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # REST API – Dispositivi
    # ------------------------------------------------------------------

    @app.get("/api/version")
    async def get_version():
        return {"version": __version__}

    @app.get("/api/devices")
    async def get_devices():
        return [d.to_dict() for d in adb.devices.values()]

    @app.post("/api/devices/{serial}/label")
    async def set_label(serial: str, label: str = Query(...)):
        adb.set_label(serial, label)
        return {"ok": True}

    @app.post("/api/devices/{serial}/select")
    async def select_device(serial: str, selected: bool = Query(True)):
        dev = adb.get_device(serial)
        if dev:
            dev.selected = selected
        return {"ok": True}

    @app.post("/api/devices/{serial}/screen-on")
    async def screen_on(serial: str):
        await adb.screen_on(serial)
        return {"ok": True}

    @app.post("/api/devices/{serial}/screen-off")
    async def screen_off(serial: str):
        await adb.screen_off(serial)
        return {"ok": True}

    @app.post("/api/devices/{serial}/reboot")
    async def reboot_device(serial: str):
        await adb.reboot(serial)
        return {"ok": True}

    @app.post("/api/adb/restart")
    async def restart_adb():
        """Riavvia il daemon ADB per forzare un nuovo handshake RSA.

        Richiede che sul telefono siano state revocate le autorizzazioni debug USB;
        l'app non puo aggirare la sicurezza Android.
        """
        success = await adb.restart_adb_server()
        if success:
            return {"ok": True, "message": "Daemon ADB riavviato"}
        return JSONResponse(
            {"ok": False, "error": "riavvio ADB fallito"}, status_code=500
        )

    @app.get("/api/devices/{serial}/screenshot")
    async def screenshot(serial: str):
        data = await adb.take_screenshot_raw(serial)
        if data:
            return StreamingResponse(
                iter([data]), media_type="image/png"
            )
        return JSONResponse({"error": "screenshot fallito"}, status_code=500)

    # ------------------------------------------------------------------
    # REST API – Streaming
    # ------------------------------------------------------------------

    @app.post("/api/stream/{serial}/start")
    async def start_stream(serial: str):
        dev = adb.get_device(serial)
        if not dev or dev.status != DeviceStatus.ONLINE:
            return JSONResponse({"error": "dispositivo non online"}, status_code=400)
        try:
            stream = await streams.start_stream(serial)
            dev.streaming = stream.alive
        except Exception as exc:
            dev.streaming = False
            logs.error(f"Errore avvio stream: {exc}", serial=serial)
            return JSONResponse({"error": str(exc)}, status_code=500)
        return {"ok": True}

    @app.post("/api/stream/{serial}/stop")
    async def stop_stream(serial: str):
        await streams.stop_stream(serial)
        dev = adb.get_device(serial)
        if dev:
            dev.streaming = False
        return {"ok": True}

    @app.post("/api/stream/start-all")
    async def start_all_streams():
        for serial, dev in adb.devices.items():
            if dev.status == DeviceStatus.ONLINE:
                try:
                    await streams.start_stream(serial)
                    dev.streaming = True
                except Exception as exc:
                    dev.streaming = False
                    logs.error(f"Errore avvio stream: {exc}", serial=serial)
        return {"ok": True}

    @app.post("/api/stream/stop-all")
    async def stop_all_streams():
        await streams.stop_all()
        for dev in adb.devices.values():
            dev.streaming = False
        return {"ok": True}

    @app.get("/api/stream/{serial}/mjpeg")
    async def mjpeg_feed(serial: str):
        """Endpoint MJPEG per lo streaming continuo di frame JPEG."""
        stream = streams.get_stream(serial)
        if not stream:
            return JSONResponse({"error": "stream non attivo"}, status_code=404)

        async def generate():
            q = stream.subscribe()
            try:
                while True:
                    try:
                        frame = await asyncio.wait_for(q.get(), timeout=10.0)
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n"
                            + frame
                            + b"\r\n"
                        )
                    except asyncio.TimeoutError:
                        # Invia un frame vuoto per keepalive
                        yield b"--frame\r\n\r\n"
            finally:
                stream.unsubscribe(q)

        return StreamingResponse(
            generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/api/stream/{serial}/frame")
    async def last_frame(serial: str):
        """Ritorna l'ultimo frame JPEG catturato."""
        stream = streams.get_stream(serial)
        if stream and stream.last_frame:
            return StreamingResponse(
                iter([stream.last_frame]), media_type="image/jpeg"
            )
        return JSONResponse({"error": "nessun frame"}, status_code=404)

    # ------------------------------------------------------------------
    # REST API – Input
    # ------------------------------------------------------------------

    @app.post("/api/input/focus/{serial}")
    async def focus_device(serial: str):
        input_relay.focused_serial = serial
        return {"ok": True}

    @app.post("/api/input/broadcast")
    async def toggle_broadcast(enabled: bool = Query(True)):
        input_relay.broadcast_mode = enabled
        return {"ok": True, "broadcast": enabled}

    @app.post("/api/input/tap")
    async def input_tap(x: int = Query(...), y: int = Query(...),
                        w: int = Query(0), h: int = Query(0)):
        await input_relay.tap(x, y, w, h)
        return {"ok": True}

    @app.post("/api/input/swipe")
    async def input_swipe(x1: int = Query(...), y1: int = Query(...),
                          x2: int = Query(...), y2: int = Query(...),
                          duration: int = Query(300)):
        await input_relay.swipe(x1, y1, x2, y2, duration)
        return {"ok": True}

    @app.post("/api/input/keyevent")
    async def input_keyevent(keycode: int = Query(...)):
        await input_relay.keyevent(keycode)
        return {"ok": True}

    @app.post("/api/input/text")
    async def input_text(text: str = Query(...)):
        await input_relay.text(text)
        return {"ok": True}

    # ------------------------------------------------------------------
    # REST API – Bulk Actions
    # ------------------------------------------------------------------

    @app.post("/api/bulk/install-apk")
    async def bulk_install_apk(file: UploadFile = File(...)):
        # Salva il file temporaneamente
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".apk")
        try:
            content = await file.read()
            tmp.write(content)
            tmp.close()
            progress = await bulk.install_apk(tmp.name)
            return progress.to_dict()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    @app.post("/api/bulk/shell")
    async def bulk_shell(command: str = Query(...)):
        results = await bulk.run_shell(command)
        return results

    @app.post("/api/bulk/push-file")
    async def bulk_push_file(
        file: UploadFile = File(...),
        remote_path: str = Query("/sdcard/"),
    ):
        remote_path = remote_path.strip()
        if ".." in remote_path or not re.match(r"^/(sdcard|data/local/tmp)/", remote_path):
            return JSONResponse({"error": "percorso remoto non consentito"}, status_code=400)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="_" + (file.filename or "file"))
        try:
            content = await file.read()
            tmp.write(content)
            tmp.close()
            progress = await bulk.push_file(tmp.name, remote_path)
            return progress.to_dict()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    @app.post("/api/bulk/reboot-all")
    async def bulk_reboot():
        await bulk.reboot_all()
        return {"ok": True}

    @app.post("/api/bulk/wake-all")
    async def bulk_wake():
        await bulk.wake_all()
        return {"ok": True}

    @app.post("/api/bulk/sleep-all")
    async def bulk_sleep():
        await bulk.sleep_all()
        return {"ok": True}

    # ------------------------------------------------------------------
    # REST API – Settings
    # ------------------------------------------------------------------

    @app.get("/api/settings")
    async def get_settings():
        return settings.model_dump()

    @app.post("/api/settings")
    async def update_settings(data: dict):
        nonlocal settings
        ALLOWED = {"poll_interval_s", "grid_columns", "max_concurrent_installs",
                   "start_with_windows", "start_minimized", "minimize_to_tray"}
        STREAM_KEYS = {"max_fps", "max_size", "bit_rate", "video_codec"}
        for key, value in data.items():
            if key in ALLOWED:
                if key in ("poll_interval_s", "max_concurrent_installs"):
                    value = max(1, int(value))
                elif key == "grid_columns":
                    value = max(1, min(20, int(value)))
                elif key in ("start_with_windows", "start_minimized", "minimize_to_tray"):
                    value = bool(value)
                    if key == "start_with_windows":
                        try:
                            startup.set_run_at_boot(value)
                        except Exception as exc:
                            logs.warn(f"Errore impostazione avvio Windows: {exc}", throttle_s=30)
                setattr(settings, key, value)
            elif key == "stream" and isinstance(value, dict):
                for sk, sv in value.items():
                    if sk not in STREAM_KEYS:
                        continue
                    if sk == "max_size":
                        sv = max(240, min(1920, int(sv)))
                    elif sk == "max_fps":
                        sv = max(1, min(60, int(sv)))
                    elif sk == "bit_rate":
                        sv = max(500_000, min(20_000_000, int(sv)))
                    elif sk == "video_codec":
                        sv = "h264" if sv not in ("h264", "h265") else sv
                    setattr(settings.stream, sk, sv)
        save_settings(settings)
        app.state.settings = settings
        return {"ok": True}

    @app.post("/api/settings/apply-stream")
    async def apply_stream_settings():
        await streams.stop_all()
        for serial in adb.devices:
            dev = adb.get_device(serial)
            if dev and dev.status == DeviceStatus.ONLINE:
                try:
                    await streams.start_stream(serial)
                    dev.streaming = True
                except Exception as exc:
                    dev.streaming = False
                    logs.error(f"Errore riavvio stream: {exc}", serial=serial)
        return {"ok": True}

    @app.get("/api/settings/export")
    async def export_settings():
        payload = {
            "version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "settings": settings.model_dump(),
            "labels": load_labels(),
            "tags": load_tags(),
            "played": load_played(),
            "known": load_known(),
        }
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        filename = f"griddroid-config-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            content=data,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/devices")
    async def get_devices():
        """Restituisce la lista corrente dei dispositivi (fallback per polling)."""
        return {
            "devices": [d.to_dict() for d in list(adb.devices.values())],
            "broadcast": input_relay.broadcast_mode,
            "focused": input_relay.focused_serial,
        }

    @app.get("/api/server-info")
    async def server_info(request: Request):
        """Restituisce host, porta e URL raggiungibili dalla LAN."""
        port = settings.port
        bind_host = settings.host
        local_ips = _get_local_ips()
        if bind_host == "0.0.0.0":
            urls = [f"http://{ip}:{port}" for ip in local_ips]
        else:
            urls = [f"http://{bind_host}:{port}"]
        host_header = request.headers.get("host")
        if host_header:
            current_url = f"http://{host_header}"
        else:
            current_url = f"http://{bind_host}:{port}"
        return {
            "host": bind_host,
            "port": port,
            "local_urls": urls,
            "current_url": current_url,
        }

    @app.post("/api/open-firewall")
    async def open_firewall():
        """Crea/apre una regola firewall in ingresso per la porta dell'app."""
        ok, message = await asyncio.get_event_loop().run_in_executor(
            None, _add_firewall_rule, settings.port
        )
        return {"ok": ok, "message": message}

    # ------------------------------------------------------------------
    # REST API – Logs
    # ------------------------------------------------------------------

    @app.get("/api/logs")
    async def get_logs(limit: int = Query(200)):
        return logs.history(limit)

    # ------------------------------------------------------------------
    # Script ADB
    # ------------------------------------------------------------------

    @app.get("/api/scripts")
    async def elenca_script():
        """Catalogo degli script disponibili, raggruppati per categoria."""
        return {"categorie": script_engine.catalogo()}

    @app.post("/api/scripts/{script_id}/esegui")
    async def esegui_script(script_id: str, body: dict):
        """Esegue uno script sui dispositivi indicati.

        Body: {"target": "selezionati" | "tutti" | "singolo",
               "serial": "...", "parametri": {...}}
        """
        target = body.get("target", "selezionati")
        parametri = body.get("parametri", {})

        if target == "singolo":
            serial = body.get("serial", "")
            serials = [serial] if serial else []
        elif target == "tutti":
            serials = [
                s for s, d in adb.devices.items()
                if d.status == DeviceStatus.ONLINE
            ]
        else:
            serials = [
                s for s, d in adb.devices.items()
                if d.status == DeviceStatus.ONLINE and d.selected
            ]

        if not serials:
            return JSONResponse(
                {"errore": "Nessun dispositivo disponibile per l'esecuzione"},
                status_code=400,
            )

        risultati = await script_engine.esegui(script_id, serials, parametri)
        return {
            "risultati": [r.to_dict() for r in risultati],
            "riusciti": sum(1 for r in risultati if r.ok),
            "totale": len(risultati),
        }

    # ------------------------------------------------------------------
    # Aggiornamento automatico
    # ------------------------------------------------------------------

    @app.get("/api/check-update")
    async def check_update():
        remote = await updater.fetch_remote_info(settings.update_url)
        if not remote:
            return {
                "available": False,
                "version": __version__,
                "message": "Impossibile contattare il server degli aggiornamenti (offline).",
            }
        remote_version = remote.get("version", "0.0.0")
        if not updater.is_newer(remote_version, __version__):
            return {"available": False, "version": __version__}
        platform_key = "windows" if sys.platform == "win32" else "linux"
        platform_info = remote.get(platform_key)
        if not platform_info:
            return {"available": False, "version": __version__}
        return {
            "available": True,
            "current_version": __version__,
            "new_version": remote_version,
            "download_url": platform_info.get("download_url"),
            "silent_args": platform_info.get("silent_args", []),
        }

    @app.post("/api/update/start")
    async def update_start(data: dict):
        url = data.get("download_url")
        version = data.get("version")
        silent_args = data.get("silent_args", [])
        if not url or not version:
            return JSONResponse({"error": "dati mancanti"}, status_code=400)
        if app.state.update_state["status"] in ("downloading", "installing"):
            return JSONResponse({"error": "aggiornamento già in corso"}, status_code=409)
        ext = ".exe" if sys.platform == "win32" else ".sh"
        dest = Path(tempfile.gettempdir()) / f"GridDroid_{version}_setup{ext}"
        app.state.update_state = {
            "status": "starting",
            "percent": 0,
            "error": None,
            "version": version,
            "download_url": url,
            "silent_args": silent_args,
            "installer": str(dest),
        }

        async def _do_download():
            await updater.download_file(url, dest, app.state.update_state)

        asyncio.create_task(_do_download())
        return {"ok": True}

    @app.get("/api/update/progress")
    async def update_progress():
        return app.state.update_state

    @app.post("/api/update/apply")
    async def update_apply():
        if app.state.update_state.get("status") != "ready":
            return JSONResponse({"error": "download non pronto"}, status_code=409)
        installer = Path(app.state.update_state["installer"])
        if not installer.exists():
            return JSONResponse({"error": "installer non trovato"}, status_code=500)
        silent_args = app.state.update_state.get("silent_args", [])
        app.state.update_state["status"] = "installing"
        app.state.update_state["percent"] = 100

        restart_path = None
        if getattr(sys, "frozen", False):
            restart_path = sys.executable

        updater.schedule_install(installer, silent_args, restart_path, os.getpid())

        async def _shutdown():
            await asyncio.sleep(1.0)
            os._exit(0)

        asyncio.create_task(_shutdown())
        return {"closing": True}

    # ------------------------------------------------------------------
    # WebSocket – Aggiornamenti in tempo reale
    # ------------------------------------------------------------------

    @app.websocket("/ws/stream/{serial}")
    async def ws_stream(ws: WebSocket, serial: str):
        """WebSocket binario per streaming H264: il browser decodifica in hardware.

        Formato: 1 byte flag (1 = keyframe, 0 = delta) + access unit Annex-B.
        """
        if not _origin_allowed(ws.headers.get("origin")):
            return
        await ws.accept()
        stream = streams.get_stream(serial)
        if not stream:
            await ws.close(code=1008, reason="stream non attivo")
            return
        q = stream.subscribe()
        try:
            # Invia subito l'ultimo keyframe così il decoder parte immediatamente
            keyframe = stream.last_keyframe
            if keyframe:
                await ws.send_bytes(keyframe)
            while True:
                frame = await q.get()
                await ws.send_bytes(frame)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            stream.unsubscribe(q)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        if not _origin_allowed(ws.headers.get("origin")):
            return
        await ws.accept()
        log_queue = logs.subscribe()
        try:
            # Task per inviare aggiornamenti periodici
            async def send_updates():
                while True:
                    try:
                        devices = list(adb.devices.values())
                        msg = {
                            "type": "devices",
                            "data": [d.to_dict() for d in devices],
                            "broadcast": input_relay.broadcast_mode,
                            "focused": input_relay.focused_serial,
                        }
                        await ws.send_json(msg)
                    except Exception as exc:
                        logs.warn(f"Errore invio aggiornamenti WS: {exc}", throttle_s=30)
                    await asyncio.sleep(1.0)

            # Task per inviare log in tempo reale
            async def send_logs():
                while True:
                    entry = await log_queue.get()
                    await ws.send_json({
                        "type": "log",
                        "data": entry.to_dict(),
                    })

            # Task per ricevere comandi dal frontend
            async def receive_commands():
                while True:
                    raw = await ws.receive_text()
                    try:
                        cmd = json.loads(raw)
                        await _handle_ws_command(cmd)
                    except json.JSONDecodeError:
                        pass

            await asyncio.gather(
                send_updates(),
                send_logs(),
                receive_commands(),
                return_exceptions=True,
            )
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            logs.unsubscribe(log_queue)

    async def _handle_ws_command(cmd: dict) -> None:
        """Gestisce i comandi ricevuti via WebSocket."""
        if not isinstance(cmd, dict):
            return
        action = cmd.get("action")
        serial = cmd.get("serial", "")

        if action == "focus":
            input_relay.focused_serial = serial

        elif action == "broadcast":
            input_relay.broadcast_mode = cmd.get("enabled", False)

        elif action == "touch":
            # Evento touch grezzo dal browser: down / move / up.
            # Percorso a latenza minima via control socket scrcpy.
            await input_relay.touch(
                cmd.get("touch_action", ""),
                cmd.get("x", 0), cmd.get("y", 0),
                cmd.get("w", 0), cmd.get("h", 0),
                cmd.get("pressure", 1.0),
            )

        elif action == "scroll":
            await input_relay.scroll(
                cmd.get("x", 0), cmd.get("y", 0),
                cmd.get("w", 0), cmd.get("h", 0),
                cmd.get("hscroll", 0.0), cmd.get("vscroll", 0.0),
            )

        elif action == "tap":
            await input_relay.tap(
                cmd.get("x", 0), cmd.get("y", 0),
                cmd.get("w", 0), cmd.get("h", 0),
            )

        elif action == "swipe":
            await input_relay.swipe(
                cmd.get("x1", 0), cmd.get("y1", 0),
                cmd.get("x2", 0), cmd.get("y2", 0),
                cmd.get("duration", 300),
                cmd.get("w", 0), cmd.get("h", 0),
            )

        elif action == "keyevent":
            await input_relay.keyevent(
                cmd.get("keycode", 0),
                cmd.get("metastate", 0),
            )

        elif action == "text":
            await input_relay.text(cmd.get("text", ""))

        elif action == "macro_record":
            if cmd.get("recording", False):
                await input_relay.start_record()
            else:
                await input_relay.stop_record(cmd.get("name", ""))

        elif action == "label":
            adb.set_label(serial, cmd.get("label", ""))

        elif action == "tags":
            adb.set_tags(serial, cmd.get("tags", []))

        elif action == "select":
            dev = adb.get_device(serial)
            if dev:
                dev.selected = cmd.get("selected", True)

        elif action == "set_played":
            adb.set_played(serial, cmd.get("played", True))

        elif action == "reset_played":
            adb.reset_played()

        elif action == "start_stream":
            dev = adb.get_device(serial)
            if dev and dev.status == DeviceStatus.ONLINE:
                await streams.start_stream(serial)
                dev.streaming = True

        elif action == "stop_stream":
            await streams.stop_stream(serial)
            dev = adb.get_device(serial)
            if dev:
                dev.streaming = False

        elif action == "start_all_streams":
            for s, d in adb.devices.items():
                if d.status == DeviceStatus.ONLINE:
                    await streams.start_stream(s)
                    d.streaming = True

        elif action == "screen_on":
            await adb.screen_on(serial)

        elif action == "screen_off":
            await adb.screen_off(serial)

        elif action == "rotate":
            current = await adb.shell(serial, "settings get system accelerometer_rotation")
            if "1" in current:
                await adb.shell(serial, "settings put system accelerometer_rotation 0")
                current_orient = await adb.shell(serial, "settings get system user_rotation")
                next_orient = (int(current_orient or "0") + 1) % 4
                await adb.shell(serial, f"settings put system user_rotation {next_orient}")
            else:
                current_orient = await adb.shell(serial, "settings get system user_rotation")
                next_orient = (int(current_orient or "0") + 1) % 4
                await adb.shell(serial, f"settings put system user_rotation {next_orient}")

            # Riavvia lo stream per far rilevare a scrcpy le nuove dimensioni
            dev = adb.get_device(serial)
            if dev and dev.streaming:
                await streams.stop_stream(serial)
                await asyncio.sleep(0.5)
                await streams.start_stream(serial)

    # ------------------------------------------------------------------
    # Macro recorder
    # ------------------------------------------------------------------

    @app.get("/api/macros")
    async def api_list_macros():
        return {"macros": input_relay.list_macros(), "recording": input_relay.is_recording}

    @app.post("/api/macro/stop")
    async def api_stop_macro(name: str = Query("")):
        saved = await input_relay.stop_record(name)
        return {"ok": saved is not None, "name": saved}

    @app.post("/api/macro/{name}/replay")
    async def api_replay_macro(name: str):
        ok = await input_relay.replay_macro(name)
        return {"ok": ok}

    @app.delete("/api/macro/{name}")
    async def api_delete_macro(name: str):
        input_relay.delete_macro(name)
        return {"ok": True}

    return app
