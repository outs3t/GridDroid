# Tools bundled con GridDroid

Questa cartella contiene gli strumenti necessari per il funzionamento di GridDroid.

## ADB (Android Debug Bridge)

Per rendere l'applicazione plug-and-play, inserisci qui i file di **Android Platform Tools**:

1. Scarica da: https://developer.android.com/studio/releases/platform-tools
2. Estrai il contenuto dello zip
3. Copia questi file in questa cartella:
   - `adb.exe`
   - `AdbWinApi.dll`
   - `AdbWinUsbApi.dll`

GridDroid cercherà automaticamente `adb.exe` qui dentro prima di cercarlo nel PATH di sistema.

## scrcpy (opzionale)

Per lo streaming video in tempo reale (invece del fallback a screenshot):

1. Scarica da: https://github.com/Genymobile/scrcpy/releases
2. Copia `scrcpy.exe` e `scrcpy-server` in questa cartella
