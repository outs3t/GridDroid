# GridDroid — note per agenti

Gestore farm Android via ADB: discovery multi-server, streaming video
scrcpy (H264 TCP → WebSocket → browser), input touch/keyboard via
control channel, lettura saldi CDP, matrice Saldi, script bulk.

## Verifica rapida

```bash
python -m compileall -q griddroid/        # tutta la sintassi Python
node --check griddroid/static/app.js      # sintassi JS
node --check griddroid/static/decoder-worker.js
python -c "import sys; sys.path.insert(0,'.'); from griddroid.app import create_app; create_app()"
```

Smoke test server (senza device):

```bash
python -c "import sys,uvicorn; sys.path.insert(0,'.'); from griddroid.app import create_app; uvicorn.run(create_app(), host='127.0.0.1', port=9999)"
curl http://127.0.0.1:9999/ http://127.0.0.1:9999/api/devices
```

Versione pubblicata: `griddroid/__init__.py::__version__` — la CI
(`.github/workflows/build.yml`) builda l'installer e pubblica
`version.json` su gh-pages a ogni push su main. Bumpare la versione
quando un fix deve arrivare agli utenti via updater.

## Regole architetturali (NON rompere)

- **Event loop mai bloccato**: processi brevi (adb shell/forward/devices)
  via `adb_manager.run_proc` (Popen+communicate in `asyncio.to_thread`).
  Solo i processi long-lived (scrcpy-server, ffmpeg, getevent) usano
  `asyncio.create_subprocess_exec` perche' servono pipe asyncio.
- **Parsing Annex-B in C**: `_find_start_code` (Python) e `findStartCode`
  (JS) usano `find`/`indexOf`, MAI loop byte-per-byte (erano ~ms/frame).
- **Nessun `i-frame-interval`**: instabile sull'encoder MediaCodec dei
  device — causa reset in loop. I keyframe si chiedono solo via
  `reset_video` (re-init completa: costosa, va razionata).
- **Nessun resubscribe a cuor leggero**: ogni subscribe mid-GOP chiede
  un keyframe → reset_video. AutoWatch NON chiude il WS allo scroll-out
  (pausa solo decode/draw), MSE fa eccezione (chiude, SourceBuffer).
- **Multi-server ADB**: mai `adb -P <porta>` su porta senza server in
  ascolto — auto-avvia un daemon clone che ruba i device (Panda/5038).
  `_adb_port_listening` prima di interrogare porte extra.
- **Letture saldi**: solo CDP (chrome_devtools_remote via forward
  persistente `_cdp_fwd`), jitter per device + `Semaphore(2)` globale.
  MAI `uiautomator dump` in automatico (congela la UI dei telefoni).
- **Screen off via SurfaceControl/scrcpy** non e' visibile in dumpsys:
  lo script si fida del ritorno di `screen_off`, non ri-querya.
- **Log**: `logs.*` accoda a un writer thread (flush per riga su file
  di sessione) — non chiamare `self._file.write` direttamente.
- **MSE**: `isTypeSupported` mente — esiste il test reale con
  `addSourceBuffer` all'avvio; il remuxer marca `failed` e non riprova.
- Bump `?v=` in `index.html`/`app.js` quando cambiano gli statici,
  altrimenti i browser servono la cache.

## Convenzioni

- Commenti, log, label UI e toast in **italiano**.
- Fix mirati alla causa radice; riusare i pattern esistenti.
- Ogni modifica: py_compile/node --check prima del commit.
