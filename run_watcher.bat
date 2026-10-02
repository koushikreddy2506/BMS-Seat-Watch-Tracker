@echo off
REM run_watcher.bat -- start the BMS watcher and keep it alive.
REM Restarts after a crash, but honours a remote "stop" sent from your phone.

cd /d C:\bms

if exist ".venv\Scripts\python.exe" (
    set PY=.venv\Scripts\python.exe
) else (
    set PY=python
)

if exist stop.flag del stop.flag

:loop
echo.
echo ============================================================
echo  starting watcher at %date% %time%
echo ============================================================
%PY% bms_seat_watch.py --config watch_config.json --force

if exist stop.flag (
    echo.
    echo  stopped from your phone. Not restarting.
    echo  Run this file again to start watching.
    del stop.flag
    timeout /t 8 /nobreak >nul
    goto end
)

echo.
echo  watcher exited at %date% %time% -- restarting in 15s
echo  (close this window to stop it for good)
timeout /t 15 /nobreak >nul
goto loop

:end
