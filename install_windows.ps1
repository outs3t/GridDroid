# Installer per GridDroid (installazione utente, nessun admin richiesto)
$ErrorActionPreference = "Stop"

$appName = "GridDroid"
$sourceRoot = $PSScriptRoot
$sourceExe = $null

foreach ($rel in @("GridDroid.exe", "dist\GridDroid.exe")) {
    $cand = Join-Path $sourceRoot $rel
    if (Test-Path $cand) {
        $sourceExe = $cand
        break
    }
}

if (-not $sourceExe) {
    throw "GridDroid.exe non trovato. Assicurati che install_windows.ps1 sia nella stessa cartella di GridDroid.exe."
}

$installDir = Join-Path $env:LOCALAPPDATA "Programs\$appName"
$exePath = Join-Path $installDir "$appName.exe"
$uninstallScript = Join-Path $installDir "uninstall_windows.ps1"
$isUpdate = Test-Path $exePath

if ($isUpdate) {
    Write-Host "Installazione esistente trovata. Aggiornamento in corso..."
} else {
    Write-Host "Nuova installazione in corso..."
}

New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Copy-Item -Path $sourceExe -Destination $exePath -Force

$uninstallSource = Join-Path $sourceRoot "uninstall_windows.ps1"
if (Test-Path $uninstallSource) {
    Copy-Item -Path $uninstallSource -Destination $uninstallScript -Force
}

$wsh = New-Object -ComObject WScript.Shell
$startMenu = [Environment]::GetFolderPath("StartMenu")
$programsDir = Join-Path $startMenu "Programs"
$appProgramsDir = Join-Path $programsDir $appName
New-Item -ItemType Directory -Path $appProgramsDir -Force | Out-Null

$mainLink = Join-Path $appProgramsDir "$appName.lnk"
$mainShortcut = $wsh.CreateShortcut($mainLink)
$mainShortcut.TargetPath = $exePath
$mainShortcut.WorkingDirectory = $installDir
$mainShortcut.IconLocation = "$exePath,0"
$mainShortcut.Save()

$desktop = [Environment]::GetFolderPath("Desktop")
$desktopLink = Join-Path $desktop "$appName.lnk"
$desktopShortcut = $wsh.CreateShortcut($desktopLink)
$desktopShortcut.TargetPath = $exePath
$desktopShortcut.WorkingDirectory = $installDir
$desktopShortcut.IconLocation = "$exePath,0"
$desktopShortcut.Save()

$uninstallLink = Join-Path $appProgramsDir "Disinstalla $appName.lnk"
$uninstallShortcut = $wsh.CreateShortcut($uninstallLink)
$uninstallShortcut.TargetPath = "powershell.exe"
$uninstallShortcut.Arguments = "-ExecutionPolicy Bypass -File `"$uninstallScript`""
$uninstallShortcut.IconLocation = "shell32.dll,31"
$uninstallShortcut.Save()

$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$appName"
if (-not (Test-Path $regPath)) { New-Item -Path $regPath -Force | Out-Null }
Set-ItemProperty -Path $regPath -Name "DisplayName" -Value $appName
Set-ItemProperty -Path $regPath -Name "UninstallString" -Value "powershell.exe -ExecutionPolicy Bypass -File `"$uninstallScript`""
Set-ItemProperty -Path $regPath -Name "DisplayIcon" -Value $exePath
Set-ItemProperty -Path $regPath -Name "Publisher" -Value $appName
Set-ItemProperty -Path $regPath -Name "DisplayVersion" -Value "1.0.0"
Set-ItemProperty -Path $regPath -Name "InstallLocation" -Value $installDir

if ($isUpdate) {
    Write-Host "GridDroid aggiornato in $installDir"
} else {
    Write-Host "GridDroid installato in $installDir"
}
Write-Host "Scorciatoie create: Start Menu e Desktop."
