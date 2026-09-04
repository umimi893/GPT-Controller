@echo off
setlocal
cd /d "%~dp0"
echo GPT Controller installer
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap-windows.ps1"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo Installation finished successfully.
  echo See C:\GPT-Controller\CHATGPT_SETUP.txt for the final ChatGPT connection step.
  pause
  exit /b 0
)
echo Installation failed with exit code %RC%.
pause
exit /b %RC%
