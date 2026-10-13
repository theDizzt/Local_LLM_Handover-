@echo off
rem Use the downloaded local model; variables remain scoped to this launcher.
setlocal
set "LLM_PROVIDER=transformers"
set "LLM_MODEL_PATH=%~dp0data\models\Qwen3-4B"
rem Includes cold model loading and one possible format correction attempt.
set "LLM_TIMEOUT_SECONDS=120"
call "%~dp0run.bat" %*
exit /b %ERRORLEVEL%
