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
echo.
if exist "C:\GPT-Controller\logs\install.log" (
  echo ---- Last installer log lines ----
  powershell.exe -NoLogo -NoProfile -Command "Get-Content -LiteralPath 'C:\GPT-Controller\logs\install.log' -Tail 60"
  echo ----------------------------------
  echo Full log: C:\GPT-Controller\logs\install.log
) else (
  echo No install log was created. The failure happened before the elevated installer started.
)
echo.
echo Please send the error text above when reporting an install problem.
pause
exit /b %RC%
