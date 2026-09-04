# GridDroid — Guida all'architettura

> Panoramica tecnica del sistema e del perché delle scelte fatte.

## 1. Cos'è GridDroid

GridDroid è un pannello locale per controllare contemporaneamente più dispositivi Android da PC via ADB. Si compone di:

- **Backend Python** con `FastAPI` + `asyncio`
- **Frontend HTML/CSS/JS vanilla** servito come statico
- **scrcpy-server** per catturare il video e ricevere input
- **ADB** per discovery, installazione app, shell, file, reboot

Il programma gira come un'applicazione desktop avvolta da una finestra nativa (webview) o, in alternativa, nel browser predefinito.

## 2. Stack tecnico

| Livello | Tecnologia | Perché |
|---------|-----------|--------|
| Web server | `uvicorn` + `FastAPI` | Async, REST + WebSocket, file statici |
| GUI | HTML/CSS/JS vanilla + WebCodecs API | Nessuna dipendenza frontend complessa, decodifica H.264 hardware nel browser |
| ADB | `adb.exe` bundled o di sistema | Comunicazione con i dispositivi Android |
| Video | `scrcpy-server.jar` | Stream H.264 a bassa latenza |
| Empaquetado | PyInstaller + Inno Setup (Windows) | Eseguibile unico + installer |
| Update | `version.json` su `gh-pages` | Auto-update via GitHub Pages |

## 3. Componenti del backend

Tutto risiede nella cartella `griddroid/`.

### 3.1 `__main__.py` — punto di ingresso

- Carica la configurazione (`config.load_settings`).
- Trova una porta libera tra quella configurata e le 50 successive.
- Avvia `uvicorn.Server` in un thread daemon con un loop asyncio dedicato.
- Aspetta che il server risponda, poi apre la finestra nativa (`webview`) o il browser (`--browser`).
- Al termine uccide processi figli e chiude socket.
- Usa un mutex Windows per evitare doppie istanze.

### 3.2 `app.py` — FastAPI REST + WebSocket

Crea l'applicazione web e i servizi:

- `AdbManager`: discovery e comandi ADB
- `StreamManager`: gestione degli stream video
- `InputRelay`: inoltro input touch/tasti
- `BulkActionRunner`: installazione APK, shell, push di massa
- `ScriptEngine`: script predefiniti del laboratorio

Espone:

- API REST per lista dispositivi, controllo, aggiornamento, installazione APK, script, bulk
- WebSocket `/ws/devices` per notificare in tempo reale lo stato
- WebSocket `/ws/stream/{serial}` per ricevere i frame H.264
- File statici in `griddroid/static/`

### 3.3 `adb_manager.py` — discovery ADB

Responsabilità:

- Verificare dove si trova `adb.exe` (preferisce quello di sistema; fallback su `tools/adb.exe`).
- Pollare continuamente `adb devices` + `adb devices -l` ogni `poll_interval_s` (default 2s).
- Parsing dell'output con regex per estrarre serial, stato, modello, product, usb, transport_id.
- Mantenere in memoria lo stato di ogni dispositivo (`DeviceState`).
- Fornire un `adb_command()` bloccante/thread-safe con un `asyncio.Lock` globale.
- Riavviare il server ADB (`kill-server` + `start-server`) su richiesta utente.

Perché il lock globale? ADB server è single-threaded per molte operazioni (in particolare `adb devices`). Serializzare i comandi evita race condition con hub USB e risposte troncate.

### 3.4 `stream_engine.py` — streaming video scrcpy

`DeviceStream` gestisce, per ogni dispositivo, una pipeline H.264:

1. Pusha `scrcpy-server.jar` in `/data/local/tmp/`
2. Crea un `adb forward tcp:PORT localabstract:scrcpy_XXXXXXXX`
3. Avvia scrcpy-server sul telefono con `app_process`
4. Si connette in TCP due volte sulla stessa porta:
   - prima socket = video
   - seconda socket = controllo input
5. Legge lo stream H.264 Annex-B, divide in Access Unit (NAL)
6. Distribuisce i frame ai client WebSocket

La decodifica avviene nel browser con `WebCodecs VideoDecoder`. Ogni frame viene inviato come pacchetto binario con prefisso `0x00`/`0x01` (delta/keyframe).

`StreamManager` controlla l'avvio concorrente con un semaforo (`max_concurrent_stream_starts`).

### 3.5 `control_channel.py` — input nativo scrcpy

Invece di lanciare `adb shell input` per ogni tocco (lento, ~300ms), usa il **control socket** binario di scrcpy.

Supporta:

- `touch` (down/move/up)
- `swipe`
- `text`
- `keycode`
- `back` / `home`

I messaggi vengono costruiti con `struct` secondo il protocollo scrcpy, dando una latenza di pochi millisecondi.

### 3.6 `input_relay.py` — broadcast input

Gestisce la modalità:

- **singolo dispositivo**: input va solo sul dispositivo in focus
- **broadcast**: input va a tutti i dispositivi online e selezionati

Riceve eventi dal frontend (coordinate normalizzate 0..1), le converte nelle dimensioni native di ogni dispositivo e le invia tramite il `ControlChannel` attivo.

Supporta anche la registrazione e riproduzione di macro.

### 3.7 `bulk_actions.py` — operazioni di massa

Esegue azioni su più dispositivi in parallelo con un semaforo (`max_concurrent_installs`):

- Installazione APK (`adb install -r -d`)
- Comandi shell arbitrari
- Push di file
- Riavvio

Mantiene una barra di progresso per frontend.

### 3.8 `scripts.py` — automazioni

Libreria di script predefiniti del laboratorio, ciascuno con parametri dichiarativi. Ogni script può essere:

- una sequenza di comandi shell
- un handler Python speciale (es. sblocco PIN che deve leggere il keyguard)

Vengono eseguiti in parallelo sui dispositivi selezionati.

### 3.9 `updater.py` — aggiornamento automatico

1. Scarica `https://outs3t.github.io/GridDroid/version.json` con cache-buster (`?_=timestamp`) e header anti-cache.
2. Confronta `version` remota con `__version__` locale.
3. Se più recente, scarica il nuovo installer dall'URL indicato.
4. Crea un `.bat` temporaneo che:
   - attende la chiusura di GridDroid
   - lancia il nuovo installer
   - riavvia GridDroid

### 3.10 `config.py` — configurazione

Usa `pydantic` per validare impostazioni. Il file `~/.griddroid/config.json` memorizza:

- host/porta
- percorso adb
- parametri di stream (fps, risoluzione, bitrate)
- colonne della griglia
- max installazioni concorrenti

Etichette, tag e dispositivi "giocati" sono salvati in `labels.json`, `tags.json`, `played.json`.

### 3.11 `device.py` — modelli dati

- `DeviceStatus`: enum `online/offline/unauthorized/disconnected`
- `DeviceInfo`: serial, model, product, usb_port, transport_id
- `DeviceState`: stato runtime completo (etichetta, tag, streaming, batteria, errori, ecc.)

## 4. Componenti del frontend

Trovi tutto in `griddroid/static/`:

- `index.html`: layout della griglia, controlli, modali
- `style.css`: tema scuro, griglia responsive
- `app.js`: logica principale, WebSocket, WebCodecs decoder, comandi utente, aggiornamento

Il frontend:

1. Si connette a `/ws/devices` per ricevere la lista aggiornata
2. Per ogni dispositivo online apre `/ws/stream/{serial}`
3. Decodifica i frame H.264 con `VideoDecoder`
4. Renderizza i frame su `<canvas>`
5. Cattura mouse/touch e invia comandi via REST/websocket

## 5. Flusso dei dati

### Discovery

```
adb devices / adb devices -l
    -> AdbManager._refresh_devices()
    -> DeviceState
    -> WebSocket /ws/devices
    -> frontend aggiorna la griglia
```

### Streaming

```
frontend richiede stream
    -> StreamManager.start_stream(serial)
    -> DeviceStream (push jar, forward, scrcpy-server, socket TCP)
    -> H.264 frames
    -> WebSocket /ws/stream/{serial}
    -> frontend: VideoDecoder -> canvas
```

### Input

```
frontend: mouse/touch su canvas
    -> REST POST /input con coordinate normalizzate
    -> InputRelay
    -> ControlChannel.write() (protocollo scrcpy binario)
    -> socket TCP -> scrcpy-server -> telefono
```

### Bulk / script

```
frontend upload APK / richiesta script
    -> REST endpoint
    -> BulkActionRunner / ScriptEngine
    -> adb_command per ogni dispositivo selezionato (con semaforo)
    -> progresso via REST polling
```

## 6. Build e deploy

Il repository ha due branch:

- `main`: codice sorgente
- `gh-pages`: installer e `version.json` pubblici

`.github/workflows/build.yml`:

1. Esegue i test
2. Compila con PyInstaller (`griddroid.spec`)
3. Crea l'installer con Inno Setup
4. Sposta `GridDroid_Setup.exe` e `version.json` nel branch `gh-pages`

Localmente, per testare senza build:

```bash
python -m griddroid
# oppure
python -m griddroid --browser
```

## 7. Decisioni tecniche e perché

| Decisione | Motivo |
|-----------|--------|
| FastAPI + uvicorn | Facile, async, WebSocket integrati, file statici |
| scrcpy-server invece di screenshot | FPS alti, latenza bassa, input nativo |
| Control socket scrcpy invece di `adb shell input` | Latenza ~1ms vs ~300ms, supporto multi-touch/drag |
| WebCodecs nel browser | Decodifica GPU senza plugin, basso overhead CPU |
| Lock globale ADB | Il daemon ADB non ama comandi concorrenti aggressivi; serializzare evita liste troncate |
| `adb devices` polling | Più robusto di `adb track-devices` su grandi farm e ricollegamenti USB |
| Config in `~/.griddroid` | Persistenza utente al di fuori dell'installazione |
| webview per GUI nativa | Finestra desktop senza dipendenze da Electron/Qt |

## 8. File rilevanti

```
griddroid/
  __main__.py         # entry point
  app.py              # FastAPI + WebSocket
  adb_manager.py      # discovery e comandi ADB
  stream_engine.py    # pipeline H.264 scrcpy
  control_channel.py  # input nativo binario
  input_relay.py      # broadcast / singolo dispositivo
  bulk_actions.py     # install/mass operations
  scripts.py          # automazioni laboratorio
  updater.py          # auto-update
  config.py           # impostazioni
  device.py           # modelli dati
  static/
    index.html
    style.css
    app.js
tools/
  adb.exe
  scrcpy-server.jar
```

## 9. Note per il debug

- I log sono in `%USERPROFILE%\.griddroid\griddroid.log` (Windows) o `~/.griddroid/griddroid.log`.
- Se l'app non vede i dispositivi, controlla prima `adb devices -l` manualmente.
- Se gli stream restano neri, verifica che `scrcpy-server.jar` esista in `tools/` e che la porta TCP forward funzioni.
- Il mutex impedisce due istanze di GridDroid contemporaneamente.
