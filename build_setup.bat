@echo off
chcp 65001 >nul
setlocal

set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

if not exist "%ISCC%" (
    echo Inno Setup non trovato.
    echo Installalo con:
    echo   winget install --id JRSoftware.InnoSetup -e --silent
    exit /b 1
)

"%ISCC%" setup.iss
