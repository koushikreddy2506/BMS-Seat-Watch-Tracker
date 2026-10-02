@echo off
rem restart_site.bat -- restart the website (hidden) and its fixed link.
rem Leaves the tracker (Chrome, holder, watcher) alone. Downtime: a few seconds.
cd /d %~dp0
echo  stopping the website...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -and $_.CommandLine -match 'start_site.py|bms_server.py') -or $_.Name -eq 'cloudflared.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 2 /nobreak >nul
echo  starting it again (hidden)...
call "%~dp0ensure_site.bat"
echo.
echo  Done. Your site is back within ~30 seconds.
timeout /t 8
