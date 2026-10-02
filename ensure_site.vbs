' ensure_site.vbs -- runs ensure_site.bat with no window (used by Task Scheduler).
CreateObject("WScript.Shell").Run """C:\bms\ensure_site.bat""", 0, False
