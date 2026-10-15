@echo off
rem Use the downloaded local model; variables remain scoped to this launcher.
setlocal
set "LLM_PROVIDER=transformers"
set "LLM_MODEL_PATH=%~dp0data\models\Qwen3-4B"
rem Apply the adapter activated by "train.bat --activate RUN_ID", if any.
if exist "%~dp0data\models\adapters\active.txt" (
    set /p ACTIVE_ADAPTER=<"%~dp0data\models\adapters\active.txt"
)
if defined ACTIVE_ADAPTER (
    set "LLM_ADAPTER_PATH=%~dp0data\models\adapters\%ACTIVE_ADAPTER%"
    echo [INFO] LoRA adapter: %ACTIVE_ADAPTER%
)
rem Includes cold model loading and one possible format correction attempt.
set "LLM_TIMEOUT_SECONDS=120"
call "%~dp0run.bat" %*
exit /b %ERRORLEVEL%
