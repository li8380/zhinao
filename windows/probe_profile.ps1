# Probe profile daemon (服务 #3). exit 0 = alive, 1 = dead.
$proc = Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object {
    try { (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine -match 'profile_daemon' } catch { $false }
}
if ($proc) { exit 0 } else { exit 1 }