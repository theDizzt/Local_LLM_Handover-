@echo off
rem Train a LoRA adapter for the local Qwen model from unlabeled work PDFs.
rem Put PDFs in data\training-input, then run this file. See docs\training-pipeline.md.
rem
rem   train.bat                  run when new PDFs exist (resumes an interrupted run)
rem   train.bat --force          train again even without new PDFs
rem   train.bat --status         list training runs and the active adapter
rem   train.bat --activate ID    use a passed adapter in run-qwen.bat
rem   train.bat --deactivate     go back to the base model only
rem   train.bat --schedule 02:00 run automatically every day at that time
rem   train.bat --unschedule     remove the daily schedule
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"
set "TASK_NAME=LocalLLMHandover-Train"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "SCHEDULED="

if /i "%~1"=="--schedule" goto schedule
if /i "%~1"=="--unschedule" goto unschedule
if /i "%~1"=="--scheduled" (
    set "SCHEDULED=1"
    shift
)

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python environment not found. Run run.bat once first.
    goto fail
)
if not exist "data\training-input" mkdir "data\training-input"

rem The OCR, search and local LLM packages are prepared by the evaluation setup.
"%PYTHON_EXE%" -c "import handover_ai, torch, transformers, chromadb, paddleocr" 2>nul
if errorlevel 1 (
    echo [ERROR] OCR/search/LLM packages are missing.
    echo Install them with: "%PYTHON_EXE%" -m pip install -e ".[local-llm,rag,ocr]"
    goto fail
)
"%PYTHON_EXE%" -c "import peft, bitsandbytes" 2>nul
if errorlevel 1 (
    echo [SETUP] Installing training packages...
    "%PYTHON_EXE%" -m pip install -e ".[train]"
    if errorlevel 1 (
        echo [ERROR] Training package installation failed.
        goto fail
    )
)

"%PYTHON_EXE%" scripts\train_pipeline.py %1 %2 %3 %4 %5 %6 %7 %8 %9
set "EXIT_CODE=%ERRORLEVEL%"
if not defined SCHEDULED pause
exit /b %EXIT_CODE%

:schedule
set "AT=%~2"
if "%AT%"=="" set "AT=02:00"
schtasks /Create /F /SC DAILY /ST %AT% /TN "%TASK_NAME%" /TR "\"%~f0\" --scheduled"
if errorlevel 1 goto fail
echo [OK] Training will run every day at %AT% when new PDFs exist.
echo      Logs: data\training-runs\RUN_ID\pipeline.log
pause
exit /b 0

:unschedule
schtasks /Delete /F /TN "%TASK_NAME%"
pause
exit /b %ERRORLEVEL%

:fail
if not defined SCHEDULED pause
exit /b 1
