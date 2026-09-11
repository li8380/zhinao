# Stop profile daemon (服务 #3).
$targets = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'profile_daemon' }
foreach ($t in $targets) {
    try { Stop-Process -Id $t.ProcessId -Force; Write-Output "stopped $($t.ProcessId)" } catch { Write-Output "err $($t.ProcessId): $_" }
}
if (-not $targets) { Write-Output 'no profile daemon running' }