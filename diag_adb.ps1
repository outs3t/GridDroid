# Diagnostica ADB GridDroid - raccoglie info su chiavi e device
# Uso: powershell -ExecutionPolicy Bypass -File diag_adb.ps1
# Output: diag_adb_out.txt nella stessa cartella - incollami tutto

$out = @()
function W($t) { $script:out += $t; Write-Host $t }

W "=== DIAGNOSTICA ADB GRIDDROID ==="
W "Data: $(Get-Date)"
W "Utente: $env:USERNAME"
W ""

# --- 1. Chiavi adb trovate sul sistema ---
W "--- CHIAVI ADB (adbkey / adbkey.pub) ---"
$searchRoots = @(
    "$env:USERPROFILE\.android",
    "$env:USERPROFILE\Downloads",
    "$env:LOCALAPPDATA",
    "$env:APPDATA",
    "C:\ProgramData"
)
foreach ($r in $searchRoots) {
    if (Test-Path $r) {
        Get-ChildItem -Path $r -Recurse -Depth 3 -Include "adbkey","adbkey.pub" -ErrorAction SilentlyContinue |
            ForEach-Object { W "  $($_.FullName)  ($($_.Length) bytes, $($_.LastWriteTime))" }
    }
}
W ""

# --- 2. Env var rilevanti ---
W "--- ENV VAR ---"
foreach ($v in "ANDROID_SDK_HOME","ANDROID_USER_HOME","ADB_VENDOR_KEYS","ANDROID_ADB_SERVER_PORT") {
    W "  $v = $([Environment]::GetEnvironmentVariable($v))"
}
W ""

# --- 3. Binari adb sul sistema ---
W "--- BINARI ADB ---"
Get-ChildItem -Path "$env:USERPROFILE","C:\Program Files","C:\Program Files (x86)","$env:LOCALAPPDATA" -Recurse -Depth 4 -Filter "adb.exe" -ErrorAction SilentlyContinue |
    ForEach-Object { W "  $($_.FullName)" }
W ""

# --- 4. Stato server e device ---
$adb = Get-Command adb.exe -ErrorAction SilentlyContinue
if (-not $adb) {
    $cand = Get-ChildItem -Path "$env:LOCALAPPDATA","$env:USERPROFILE\Downloads" -Recurse -Depth 4 -Filter "adb.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cand) { $adb = $cand.FullName }
}
if ($adb) {
    $adbPath = if ($adb.Source) { $adb.Source } else { $adb }
    W "--- ADB USATO: $adbPath ---"
    W "--- adb devices -l ---"
    & $adbPath devices -l 2>&1 | ForEach-Object { W "  $_" }
    W ""
    W "--- fingerprint chiave server (~/.android/adbkey.pub) ---"
    $pub = "$env:USERPROFILE\.android\adbkey.pub"
    if (Test-Path $pub) { W "  $(Get-Content $pub)" } else { W "  (non trovata)" }
} else {
    W "!!! adb.exe non trovato"
}
W ""

# --- 5. Processi adb in esecuzione ---
W "--- PROCESSI ADB ATTIVI ---"
Get-Process adb -ErrorAction SilentlyContinue | ForEach-Object {
    W "  PID $($_.Id): $($_.Path)"
}

$out | Out-File -FilePath "diag_adb_out.txt" -Encoding utf8
W ""
W "=== FATTO - incollami diag_adb_out.txt ==="
