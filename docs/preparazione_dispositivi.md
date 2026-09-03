# Preparazione dispositivi Android per GridDroid

Guida per abilitare le opzioni sviluppatore necessarie (USB debugging, sblocco OEM, autorizzazioni ADB) sui principali brand.

## Prerequisiti generali

1. **Abilitare Opzioni sviluppatore**:
   - Impostazioni > Info sul telefono > Numero build.
   - Tocca **Numero build** 7 volte fino al messaggio "Ora sei uno sviluppatore".
2. **Entra in Opzioni sviluppatore**:
   - Impostazioni > Sistema > Opzioni sviluppatore (o Impostazioni > Opzioni aggiuntive > Sviluppatore, dipende dalla ROM).

## Toggle da attivare (sempre)

- **Debug USB** / **USB debugging**: obbligatorio.
- **Sblocco OEM** / **OEM unlocking**: attivalo se presente. Serve per sbloccare il bootloader e in alcuni casi evita che ADB venga bloccato.
- **Configurazione USB** / **Default USB configuration**: imposta su **MTP (Trasferimento file)** o **Trasferimento file / Android Auto**, mai "Solo ricarica".
- **Rimani sveglio** / **Stay awake**: utile durante i test, evita che lo schermo si spenga.
- **Revoca autorizzazioni debug USB**: usa questo pulsante solo se il PC compare come `unauthorized`.

## Dopo il primo collegamento

1. Collega il telefono al PC con un cavo USB (meglio USB 2.0, evita hub).
2. Tira giù la tendina e imposta la connessione USB su **Trasferimento file / Android Auto**.
3. Sul telefono compare il popup **"Consenti il debug USB?"**: spunta **"Consenti sempre da questo computer"** e premi OK.
4. In GridDroid il dispositivo deve apparire nella lista. Se non appare, verifica con `adb devices` da terminale.

## Istruzioni per brand

### Google Pixel

- **Debug USB**: attiva.
- **Sblocco OEM**: compare dopo il primo avvio con SIM/connessione dati; attivalo se serve.
- Per sbloccare il bootloader: `fastboot flashing unlock` (cancella tutti i dati).

### Samsung

- **Debug USB**: attiva.
- **Sblocco OEM**: attivalo. Se manca, disattiva **Auto blocca** (Impostazioni > Sicurezza) e rimuovi gli account Google/Samsung.
- Se il telefono risulta `unauthorized`: revoca le autorizzazioni debug USB e riconnetti.

### Realme / OPPO / OnePlus (ColorOS / OxygenOS)

- **Debug USB**: attiva.
- **USB debugging (Security settings)** o **Install via USB**: attiva se presente, permette input ADB.
- **Sblocco OEM**: se grigio, serve connessione internet per circa 7 giorni, nessun account vincolato e in alcuni casi nessuna SIM.
- **Configurazione USB**: imposta su **Trasferimento file**.
- Disattiva il **controllo delle autorizzazioni** se il debug USB viene disattivato da solo.

### Xiaomi / Redmi / POCO (MIUI / HyperOS)

Su MIUI/HyperOS servono passaggi extra rispetto allo stock Android. Assicurati di avere **una SIM inserita** e una **connessione internet** (dati mobili o Wi-Fi).

#### Prima di iniziare

1. **Crea o accedi con un Mi Account**:
   - Impostazioni > Account Xiaomi > Accedi o Crea un account.
   - Verifica l'account con email/telefono.
   - Alcuni modelli lo richiedono obbligatorio per abilitare `Installa via USB`.

   ![Xiaomi - Accedi con Mi Account](img/xiaomi_01_mi_account.png)

2. **Abilita Opzioni sviluppatore**:
   - Impostazioni > Il mio dispositivo > Versione MIUI.
   - Tocca 7 volte su **Versione MIUI** finché non appare "Ora sei uno sviluppatore".

   ![Xiaomi - Abilita opzioni sviluppatore](img/xiaomi_02_developer_options.png)

#### Toggle da attivare nelle Opzioni sviluppatore

- **Debug USB**: attiva e conferma i popup.
- **Installa via USB** / **Install via USB**:
  - Toccalo, richiede l'accesso al **Mi Account**.
  - Inserisci le credenziali e conferma.
  - Se dà errore, prova con **dati mobili attivi** invece del Wi-Fi (alcune ROM contattano i server cinesi).

  ![Xiaomi - Installa via USB](img/xiaomi_03_install_via_usb.png)

- **Debug USB (Impostazioni di sicurezza)** / **USB debugging (Security settings)**:
  - Fondamentale per GridDroid, permette di inviare input `adb shell input`.
  - Toccalo e conferma **tutti e tre gli avvisi** (Next/Accept/OK).

  ![Xiaomi - Debug USB sicurezza](img/xiaomi_04_usb_debug_security.png)

- **Disattiva Ottimizzazioni MIUI**:
  - Opzioni sviluppatore > Disattiva ottimizzazioni MIUI.
  - Il telefono si riavvierà. Riabilita poi Debug USB e le opzioni sopra.

- **Configurazione USB**:
  - Impostala su **MTP (Trasferimento file)** o **Trasferimento file / Android Auto**.

#### Dopo aver collegato il cavo

1. Imposta la connessione USB su **Trasferimento file / Android Auto**.
2. Sul popup **Consenti debug USB?** spunta **"Consenti sempre da questo computer"**.
3. Se GridDroid non riesce a cliccare sul telefono, verifica che **Debug USB (Impostazioni di sicurezza)** sia davvero attivo.

> **Nota sulle immagini**: i file qui sopra sono placeholder. Sostituiscili con gli screenshot reali del tuo Xiaomi per avere la guida completa.


### Motorola

- **Debug USB**: attiva.
- **Sblocco OEM**: attivalo in opzioni sviluppatore. Per sbloccare il bootloader serve un codice richiesto dal sito Motorola.

### Huawei / Honor

- **Debug USB**: attiva.
- **Consenti debug ADB in modalità solo carica**: attiva se presente.
- **Sblocco OEM**: richiedeva un codice Huawei, non più rilasciato per modelli recenti.

### ASUS / ROG

- **Debug USB**: attiva.
- **Sblocco OEM**: attiva nelle opzioni sviluppatore; per il bootloader serve l'**ASUS Unlock Tool**.

### Sony Xperia

- **Debug USB**: attiva.
- **Sblocco OEM**: attivalo; per sbloccare il bootloader serve un codice richiesto dal sito Sony.

### Nokia

- **Debug USB**: attiva.
- **Sblocco OEM**: richiede codice dal sito Nokia/HMD per alcuni modelli.

## Risoluzione problemi comuni

- **`unauthorized`**: sul telefono accetta la chiave RSA. Se il popup non compare, premi **Revoca autorizzazioni debug USB** e riconnetti il cavo.
- **`offline`**: cambia cavo o porta USB, assicurati che la connessione sia su MTP, usa una porta USB 2.0, esegui `adb kill-server`.
- **`device` senza nome o `????????`**: driver USB ADB mancanti su Windows o regole udev mancanti su Linux.
- **OEM unlock grigio/non cliccabile**: serve connessione internet per 7 giorni, account rimosso o periodo di attesa del brand.
- **GridDroid non vede il telefono**: prima verifica che `adb devices` da terminale lo veda; se non lo vede, è un problema di cavo/driver/autorizzazione.
