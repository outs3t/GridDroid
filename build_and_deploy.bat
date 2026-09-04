@echo off
chcp 65001 >nul
setlocal

echo Build e deploy della landing page in corso...
echo.

python deploy_landing.py --build

if errorlevel 1 (
    echo.
    echo Errore durante il build/deploy.
    pause
    exit /b 1
)

echo.
echo Landing aggiornata e pubblicata.
pause
