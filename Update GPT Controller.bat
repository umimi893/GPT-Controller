@echo off
setlocal
set "SOURCE=C:\GPT-Controller\source"
if not exist "%SOURCE%\scripts\update.ps1" set "SOURCE=%~dp0"
where pwsh.exe >nul 2>&1
if errorlevel 1 (
  echo PowerShell 7 is required. Run Install GPT Controller.bat to repair prerequisites.
  pause
  exit /b 1
)
echo GPT Controller updater / repair
echo.
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SOURCE%\scripts\update.ps1"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo Update finished successfully.
  exit /b 0
)
echo Update failed with exit code %RC%.
pause
exit /b %RC%
