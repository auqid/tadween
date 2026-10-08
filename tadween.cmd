@echo off
rem tadween.cmd                                open the app in your browser
rem tadween.cmd transcribe FILE [--speakers N]  write "FILE - transcript.txt" next to the recording
setlocal
set "DIR=%~dp0"
if not exist "%DIR%.venv\Scripts\python.exe" (
  echo Run setup first:  powershell -ExecutionPolicy Bypass -File "%DIR%setup.ps1" 1>&2
  exit /b 1
)
set "PYTHONPATH=%DIR%;%PYTHONPATH%"
set "PYTHONUTF8=1"
"%DIR%.venv\Scripts\python.exe" -m tadween %*
