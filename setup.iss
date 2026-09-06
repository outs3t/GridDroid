#define MyAppName "GridDroid"
#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif
#define MyAppPublisher "GridDroid"
#define MyAppExeName "GridDroid.exe"
#define MyAppURL "https://outs3t.github.io/GridDroid/"

[Setup]
AppId={{A7B3C1D2-E5F4-4A3B-8C7D-9E0F1A2B3C4D}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; Info versione visibili in "Programmi e funzionalita'" e nelle proprieta' del file
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} - Android Farm Manager
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCopyright=GridDroid
; Installazione per-utente: niente admin, niente UAC
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
; Solo Windows 10+ a 64 bit
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Wizard snello: niente pagina cartella/gruppo, l'utente fa solo Avanti-Avanti-Fine
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyMemo=no
; Aggiornamenti: chiude GridDroid se in esecuzione e lo riavvia a fine install.
; AppMutex rileva l'app anche se il Restart Manager non la vede.
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=yes
AppMutex=Global\GridDroid_Mutex
; Output
OutputDir=.\dist
OutputBaseFilename=GridDroid_Setup
SetupIconFile=.\logo.ico
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
; Disinstallatore pulito con icona e nome corretti
Uninstallable=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
; Aggiorna i timestamp dei file solo se cambiati (update piu' veloci)
UpdateUninstallLogAppName=yes

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Tasks]
Name: "desktopicon"; Description: "Crea un collegamento sul desktop"; GroupDescription: "Collegamenti:"

[Files]
Source: ".\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Disinstalla {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Avvia GridDroid"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Alla disinstallazione rimuove anche la voce "Avvia con Windows" dal registro
Filename: "reg"; Parameters: "delete ""HKCU\Software\Microsoft\Windows\CurrentVersion\Run"" /v GridDroid /f"; Flags: runhidden
