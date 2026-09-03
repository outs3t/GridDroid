# GridDroid

Applicazione desktop per la gestione, il monitoraggio e il controllo remoto di una farm di smartphone Android (20-30 dispositivi) collegati via USB.

## Caratteristiche

- **Griglia di streaming** in tempo reale (MJPEG a basso consumo, FPS configurabili)
- **Controllo singolo** con mouse/tastiera a bassa latenza
- **Broadcast mode** per replicare input su tutti i dispositivi selezionati
- **Operazioni di massa**: installazione APK, riavvio, shell custom, push file
- **Identificazione stabile** per seriale USB con etichette personalizzabili
- **Riconnessione automatica** dei dispositivi offline/unauthorized
- **Log centralizzato** con stato per dispositivo
- **Tema dark minimal** ottimizzato per lunghe sessioni

## Per l'utente finale

1. **Copia `GridDroid.exe`** sul PC Windows (penna USB, rete locale, ecc.)
2. **Collega i telefoni** via USB al PC
3. Attiva **Debug USB** su ogni telefono Android
4. Fai **doppio clic su `GridDroid.exe`**
5. Se appare **SmartScreen**, clicca **"Ulteriori informazioni" → "Esegui comunque"**
6. Attendi che si apra la finestra: l’app rileva automaticamente i dispositivi

> **Nota:** `GridDroid.exe` è **autonomo**. Non serve installare Python, ADB o copiare la cartella `tools/`. Tutto è già incluso nell’eseguibile. Al primo avvio crea la cartella `%USERPROFILE%\.griddroid\` per configurazioni, etichette e tag.

## Requisiti di sviluppo

- Python 3.11+
- Hub USB con alimentazione esterna (consigliato per 20-30 dispositivi)
- scrcpy (opzionale, per streaming video in tempo reale invece del fallback a screenshot)

## Installazione (sviluppo)

```bash
pip install -r requirements.txt
```

## Avvio

```bash
python -m griddroid
```

Oppure in modalità browser (senza finestra nativa):

```bash
python -m griddroid --browser
```

## Build Eseguibile

```bash
pip install pyinstaller
pyinstaller griddroid.spec
```

oppure semplicemente:

```bash
build_onefile.bat
```

Il file `dist/GridDroid.exe` sarà **autonomo**: basta copiarlo su un altro PC Windows (senza Python né `tools/`) e avviarlo.

## Sito di presentazione (Vercel)

La cartella `landing/` contiene un sito statico moderno per presentare GridDroid, con download e link PayPal.
Vedi `landing/README.md` per le istruzioni di pubblicazione su Vercel.

## Installazione su Linux (Arch/Debian)

1. Clona o copia il repository sul PC Linux.
2. Installa le dipendenze di sistema e prepara l'ambiente:

```bash
./install_linux.sh
```

3. Collega i telefoni Android con **Debug USB attivo**.
4. Avvia GridDroid:

```bash
griddroid
```

Se `~/.local/bin` non è nel `PATH`, esegui:

```bash
export PATH="$HOME/.local/bin:$PATH"
griddroid
```

Lo script supporta Arch/Manjaro e Debian/Ubuntu: installa `python3`, `adb` (pacchetto `android-tools` o `android-tools-adb`), crea un virtualenv in `~/.local/share/griddroid` e aggiunge un launcher `griddroid` e un'opzione nel menu applicazioni.

## Struttura

```
griddroid/
├── __main__.py          # Entry point
├── app.py               # FastAPI application
├── config.py            # Configurazione e settings
├── adb_manager.py       # Discovery, reconnect, pool ADB
├── device.py            # Modello dispositivo e stato
├── stream_engine.py     # Wrapper scrcpy / MJPEG streaming
├── input_relay.py       # Relay input singolo e broadcast
├── bulk_actions.py      # Operazioni di massa
├── log_manager.py       # Sistema log centralizzato
├── static/              # Frontend (HTML/CSS/JS)
│   ├── index.html
│   ├── app.js
│   └── style.css
```
