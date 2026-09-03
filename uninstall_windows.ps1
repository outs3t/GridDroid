# Disinstallatore per GridDroid
$appName = "GridDroid"
$installDir = Join-Path $env:LOCALAPPDATA "Programs\$appName"

if (Test-Path $installDir) {
    Remove-Item $installDir -Recurse -Force
}

$startMenu = [Environment]::GetFolderPath("StartMenu")
$appProgramsDir = Join-Path $startMenu "Programs\$appName"
if (Test-Path $appProgramsDir) {
    Remove-Item $appProgramsDir -Recurse -Force
}

$desktop = [Environment]::GetFolderPath("Desktop")
$desktopLink = Join-Path $desktop "$appName.lnk"
if (Test-Path $desktopLink) {
    Remove-Item $desktopLink -Force
}

$regPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$appName"
if (Test-Path $regPath) {
    Remove-Item $regPath -Recurse -Force
}

Write-Host "GridDroid e stato disinstallato."
