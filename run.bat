@echo off
rem Learning note: move to this script's folder, prepare venv, then start the API.
rem OCR models are optional; install .[ocr] separately before requesting OCR.
setlocal
rem setlocal keeps the variables below from changing the caller's environment.

cd /d "%~dp0"

set "VENV_DIR=.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8000"

if not exist "%PYTHON_EXE%" (
    rem Try Python 3.12 first, then let the launcher choose an installed version.
    echo [SETUP] Creating the Python virtual environment...
    py -3.12 -m venv "%VENV_DIR%" 2>nul
    if errorlevel 1 py -m venv "%VENV_DIR%" 2>nul
    if errorlevel 1 (
        echo [ERROR] Python 3.12 or newer could not be found.
        echo Install Python and make sure the py launcher is available.
        pause
        exit /b 1
    )
)

"%PYTHON_EXE%" -c "import fastapi, handover_ai, pymupdf, multipart" 2>nul
if errorlevel 1 (
    echo [SETUP] Installing project dependencies...
    "%PYTHON_EXE%" -m pip install -e ".[dev]"
    if errorlevel 1 (
        echo [ERROR] Project dependency installation failed.
        pause
        exit /b 1
    )
)

echo [START] Local LLM Handover API
echo [INFO] API:  http://%HOST%:%PORT%
echo [INFO] Docs: http://%HOST%:%PORT%/docs
echo [INFO] Press Ctrl+C to stop the server.
echo.

"%PYTHON_EXE%" -m uvicorn handover_ai.main:app --host "%HOST%" --port "%PORT%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] The server stopped with exit code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
