@echo off
echo ==================================================
echo [Quality Report] Starting Quality Report Server...
echo ==================================================

:: 1. Force kill existing Python processes to free Port 8888 (已停用，允許雙開)
:: taskkill /f /im python.exe >nul 2>nul

:: 2. Check Credentials
if not exist "client_secrets.json" (
    echo [WARNING] client_secrets.json not found!
    echo Please download OAuth client ID json from Google Cloud Console.
    echo --------------------------------------------------
)

:: 3. Set Python Command
if exist "venv\Scripts\python.exe" (
    set PY_CMD=venv\Scripts\python
) else (
    set PY_CMD=python
)

:: 4. Start Python Server and open browser
echo Starting Python FastAPI server on Port 8888 using %PY_CMD%...
start http://127.0.0.1:8888
%PY_CMD% -m uvicorn app:app --host 127.0.0.1 --port 8888

pause
