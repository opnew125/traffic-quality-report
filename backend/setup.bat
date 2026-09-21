@echo off
echo ==================================================
echo [Quality Report] Initializing dependencies...
echo ==================================================

:: 1. Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found! Please install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

:: 2. Try to setup pip if missing
python -m pip --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [INFO] pip not found in this portable Python. Attempting to install pip...
    python -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', 'get-pip.py')" >nul 2>nul
    if exist "get-pip.py" (
        python get-pip.py --user
        del get-pip.py
    ) else (
        echo [ERROR] Failed to download pip installer. Please check your internet connection.
        pause
        exit /b 1
    )
)

:: 3. Try to create venv, fallback to direct install if venv is missing
echo Checking virtual environment venv capability...
python -c "import venv" >nul 2>nul
if %errorlevel% equ 0 (
    echo [INFO] venv module is available. Creating virtual environment venv...
    if not exist "venv" (
        python -m venv venv
    )
    set PY_CMD=venv\Scripts\python
) else (
    echo [WARNING] venv module not found - Portable Python detected.
    echo Falling back to installing dependencies directly to the main Python environment.
    set PY_CMD=python
)

:: 4. Install Python Dependencies
echo Installing Python requirements...
%PY_CMD% -m pip install --upgrade pip
%PY_CMD% -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python packages!
    pause
    exit /b 1
)
echo [SUCCESS] Python environment setup complete.

:: 5. Check & Install Node.js Dependencies
where npm >nul 2>nul
if %errorlevel% neq 0 (
    echo [WARNING] npm Node.js not found in PATH!
    echo If you use portable Node.js, please add node.exe directory to PATH.
    echo Otherwise, please install Node.js from https://nodejs.org/
    pause
    exit /b 1
)

echo Installing Node.js packages...
call npm install
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Node.js packages!
    pause
    exit /b 1
)
echo [SUCCESS] Node.js dependencies setup complete.

echo ==================================================
echo [FINISH] All environment dependencies installed successfully!
echo Please make sure client_secrets.json is placed in this folder.
echo Next, you can run test.bat to authenticate.
echo ==================================================
pause
