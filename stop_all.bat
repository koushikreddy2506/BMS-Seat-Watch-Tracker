@echo off
rem stop_all.bat -- stop the trackers (seat holder + watcher).
rem The website and its public link are NEVER stopped here: they stay up so
rem visitors can see the tracker is off and ask you to turn it on.
rem   stop_all.bat              holder + watcher
rem   stop_all.bat with-chrome  also closes the debug Chrome
rem                             (and any payment page left open by a hold)
rem   stop_all.bat list         only shows what would be stopped
rem The debug Chrome is left open by default so held seats aren't lost.

cd /d %~dp0
set "MODE=%~1"

if /i "%MODE%"=="list" goto list

echo.
rem the watcher's launcher loop restarts it after a crash; this flag tells it not to
echo stopped by stop_all.bat > stop.flag

call :stop python.exe      "seat_holder.py*--listen"    "holder "
call :stop cmd.exe         "run_holder.bat"             ""
rem tell the website right away (a killed holder can't write its own "stopped")
> holder_status.json echo {"running": false, "at": 0}
call :stop cmd.exe         "run_watcher.bat"            ""
call :stop python.exe      "bms_seat_watch.py"          "watcher"

echo  [website]  left running ^(always on^)

if /i "%MODE%"=="with-chrome" (
    call :stop chrome.exe  "remote-debugging-port=9222" "chrome "
) else (
    echo  [chrome ]  left open ^(held seats stay on their payment pages^)
)

echo.
echo  Done. start_all.bat starts the trackers again.
timeout /t 10
exit /b 0

:list
echo.
echo  Would stop:
powershell -NoProfile -Command "$pats = @(@('python.exe','seat_holder.py*--listen'),@('cmd.exe','run_holder.bat'),@('cmd.exe','run_watcher.bat'),@('python.exe','bms_seat_watch.py'),@('chrome.exe','remote-debugging-port=9222')); foreach ($x in Get-CimInstance Win32_Process) { if ($x.ProcessId -eq $PID) { continue }; foreach ($p in $pats) { if ($x.Name -eq $p[0] -and $x.CommandLine -like ('*' + $p[1] + '*')) { '  {0,-7} {1,-16} {2}' -f $x.ProcessId, $x.Name, $p[1]; break } } }"
echo.
exit /b 0

rem ---- helpers ---------------------------------------------------------------
:stop
rem stop every %1 process (and its children) whose command line matches %2;
rem report under label %3 (blank label = stop quietly). Matching the program
rem name too means other tools that merely mention these files are never hit.
powershell -NoProfile -Command "$n = 0; foreach ($x in Get-CimInstance Win32_Process) { if ($x.Name -eq '%~1' -and $x.ProcessId -ne $PID -and $x.CommandLine -like '*%~2*') { taskkill /T /F /PID $x.ProcessId 2>$null | Out-Null; $n++ } }; if ('%~3'.Trim()) { if ($n) { '  [%~3]  stopped' } else { '  [%~3]  was not running' } }"
exit /b 0
