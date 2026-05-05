@echo off
chcp 65001 >nul 2>&1
:: ============================================================
:: JARVIS - One-Click Launcher
:: Double-click this file to start Jarvis.
:: Starts the backend API + voice agent in parallel.
:: ============================================================

title JARVIS - Starting Up...
color 0B
echo.
echo  ========================================================
echo.
echo       JJJJJ  AAAAA  RRRR   V   V  III  SSSS
echo         J    A   A  R   R  V   V   I  S
echo         J    AAAAA  RRRR    V V    I   SSS
echo    J    J    A   A  R  R    V V    I      S
echo     JJJJ     A   A  R   R    V    III  SSSS
echo.
echo         Your Digital Chief of Staff
echo         Say "Jarvis" or double-clap.
echo.
echo  ========================================================
echo.

set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"

:: -- Check if backend venv exists --
if not exist "backend\.venv\Scripts\python.exe" (
    echo  [!] Backend virtual environment not found.
    echo      Run: cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

:: -- Start Backend API (in background) --
echo  [1/2] Starting Jarvis Backend API on :8000 ...
start /min "Jarvis-Backend" cmd /c "cd /d %PROJECT_DIR%\backend && .venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000"
timeout /t 3 /nobreak >nul

:: -- Start Voice Agent --
echo  [2/2] Starting Voice Agent ...
echo.
echo  ========================================================
echo    JARVIS IS ONLINE. Say "Jarvis" to activate.
echo    Press Ctrl+C to shut down.
echo  ========================================================
echo.

cd /d "%PROJECT_DIR%\voice_agent"

:: Use backend venv for voice agent too (shared dependencies)
"%PROJECT_DIR%\backend\.venv\Scripts\python.exe" main.py

:: -- Cleanup on exit --
echo.
echo  Shutting down Jarvis...
taskkill /FI "WINDOWTITLE eq Jarvis-Backend" /F >nul 2>&1
echo  Goodbye, Boss.
pause
