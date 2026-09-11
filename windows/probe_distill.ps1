# Probe whether distill_daemon.py is running.
# Exit 0 if alive, 1 if dead. Used by router watchdog over SSH.
# 2026-09-03: daemon 跑在 pythonw.exe（无窗口），需同时匹配 python.exe 与 pythonw.exe
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
if ($p -and ($p.CommandLine -match 'distill_daemon\.py')) { exit 0 } else { exit 1 }