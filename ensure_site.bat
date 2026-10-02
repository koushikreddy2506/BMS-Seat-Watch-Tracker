@echo off
rem ensure_site.bat -- start the website (hidden) if it isn't already running.
rem Task Scheduler runs this at logon and every 5 minutes (through ensure_site.vbs,
rem so no window flashes), which keeps your site up.
cd /d %~dp0
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*start_site.py*' }) { exit 0 } else { exit 1 }"
if %errorlevel%==0 exit /b 0
if exist ".venv\Scripts\python.exe" (set "PY=%~dp0.venv\Scripts\python.exe") else (set "PY=python")
echo %date% %time% website was not running - starting it>> "%~dp0ensure_site.log"
powershell -NoProfile -Command "Start-Process -FilePath '%PY%' -ArgumentList 'start_site.py' -WorkingDirectory '%~dp0.' -WindowStyle Hidden"
