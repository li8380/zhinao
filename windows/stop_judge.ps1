# Stop judge_daemon.py if running. Used by watchdog drills / recovery.
# 2026-09-03: daemon 跑在 pythonw.exe（无窗口），需同时匹配 python.exe 与 pythonw.exe
$ErrorActionPreference = 'SilentlyContinue'
$p = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'"
$d = $p | Where-Object { $_.CommandLine -match 'judge_daemon\.py' }
if ($d) {
    $d | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Output 'stopped'
} else {
    Write-Output 'not-running'
}