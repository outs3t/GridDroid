@echo off
chcp 65001 >nul
echo Controllo PyInstaller...
python -m pip install pyinstaller 2>nul
if errorlevel 1 (
    echo Errore: impossibile installare pyinstaller.
    pause
    exit /b 1
)

echo.
echo Build di GridDroid.exe in corso...
python -m PyInstaller griddroid.spec --clean --noconfirm
if errorlevel 1 (
    echo Errore durante la build.
    pause
    exit /b 1
)

echo.
echo Build completata.
echo.
echo File pronto per l'altro PC:
echo   dist\GridDroid.exe
echo.
echo Copia solo dist\GridDroid.exe, non servono altre cartelle.
pause
