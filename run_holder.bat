@echo off
rem Seat holder: waits for hold requests from the watcher (hold_topic) and holds
rem seats in the debug Chrome. Needs Chrome running with --remote-debugging-port=9222.
rem Settings live in the "holder" section of watch_config.json ("armed" false = select only).
rem If the holder stops (e.g. Chrome wasn't ready yet after a reboot) it's started
rem again after 15s. Close this window, or run stop_all.bat, to stop it for good.
rem Log: holder.log
cd /d %~dp0
if exist ".venv\Scripts\python.exe" (set PY=.venv\Scripts\python.exe) else (set PY=python)

:loop
echo.
echo  starting seat holder at %date% %time%
%PY% seat_holder.py --config watch_config.json --listen %*
echo.
echo  seat holder stopped at %date% %time% - starting it again in 15s
echo  (close this window to stop it for good; details in holder.log)
timeout /t 15 /nobreak >nul
goto loop
