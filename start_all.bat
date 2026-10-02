@echo off
rem start_all.bat -- start everything Seat Watch needs on this PC, in order:
rem   1. debug Chrome (port 9222)   used by the seat holder to hold seats
rem   2. website + public link      start_site.bat  (bms_server.py + cloudflared), hidden
rem   3. watcher                    run_watcher.bat (restarts itself if it crashes)
rem   4. seat holder                run_holder.bat  (needs Chrome from step 1)
rem Each runs in its own window. Anything already running is left alone, so it's
rem safe to run this again: it only starts what's missing (and never opens a
rem second public link).
rem   start_all.bat status   only shows what's running, starts nothing

cd /d %~dp0
if /i "%~1"=="status" goto status
echo %date% %time% start_all.bat run>> "%~dp0start_all.log"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

echo.
echo  Seat Watch - starting up
echo  ------------------------

rem ---- 1. debug Chrome ------------------------------------------------------
call :chrome_up
if %errorlevel%==0 (
    echo  [chrome ]  already running on port 9222
) else (
    echo  [chrome ]  starting...
    start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%~dp0chrome-profile"
    rem up to 60s: a first Chrome start after a reboot can be slow
    for /l %%i in (1,1,60) do (
        call :chrome_up && goto chrome_ready
        timeout /t 1 /nobreak >nul
    )
    echo  [chrome ]  did not come up on port 9222 - the seat holder will wait for it
)
:chrome_ready

rem ---- 2. website + public link ----------------------------------------------
call :running "start_site.py"
if %errorlevel%==0 (
    echo  [website]  already running
) else (
    echo  [website]  starting hidden ^(new public link goes to your ntfy status topic^)
    rem hidden: no window to close by accident when restarting the other parts
    rem (full path: a bare "start_site.bat" isn't found by the hidden cmd)
    powershell -NoProfile -Command "Start-Process -FilePath cmd.exe -ArgumentList '/c','%~dp0start_site.bat' -WorkingDirectory '%~dp0.' -WindowStyle Hidden"
)

rem ---- 3. watcher ------------------------------------------------------------
call :running "bms_seat_watch.py"
if %errorlevel%==0 (
    echo  [watcher]  already running
) else (
    echo  [watcher]  starting
    echo %date% %time%   starting watcher>> "%~dp0start_all.log"
    start "Seat Watch - watcher" cmd /c run_watcher.bat
)

rem ---- 4. seat holder --------------------------------------------------------
call :running "seat_holder.py*--listen"
if %errorlevel%==0 (
    echo  [holder ]  already running
) else (
    echo  [holder ]  starting
    echo %date% %time%   starting holder>> "%~dp0start_all.log"
    start "Seat Watch - holder" cmd /c run_holder.bat
)

echo.
echo  Done. Each part has its own window; close a window to stop that part.
echo  The website runs hidden (no window); its log is server.log.
echo  Admin portal: http://localhost:8080/admin  (key is in server.log)
echo.
timeout /t 15
exit /b 0

rem ---- helpers ---------------------------------------------------------------
:chrome_up
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:9222/json/version -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
exit /b %errorlevel%

:running
rem exit 0 if a python process whose command line matches %1 is running
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*%~1*' }) { exit 0 } else { exit 1 }"
exit /b %errorlevel%

:status
echo.
echo  Seat Watch - status
echo  -------------------
call :chrome_up && (echo  [chrome ]  running on port 9222) || (echo  [chrome ]  NOT running)
call :running "start_site.py" && (echo  [website]  running) || (echo  [website]  NOT running)
call :running "bms_seat_watch.py" && (echo  [watcher]  running) || (echo  [watcher]  NOT running)
call :running "seat_holder.py*--listen" && (echo  [holder ]  running) || (echo  [holder ]  NOT running)
echo.
exit /b 0
