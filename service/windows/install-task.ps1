# Registers syforce as a per-user scheduled task that starts at logon and restarts on failure.
# Run in PowerShell from the kit's app\ folder:   powershell -ExecutionPolicy Bypass -File ..\service\windows\install-task.ps1
# Alternative: NSSM (https://nssm.cc):  nssm install syforce "<app>\venv\Scripts\python.exe" "<app>\server.py --port 9001"
$App = (Resolve-Path ".").Path
$Py  = Join-Path $App "venv\Scripts\pythonw.exe"
$Action   = New-ScheduledTaskAction -Execute $Py -Argument "`"$App\server.py`" --port 9001" -WorkingDirectory $App
$Trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 7
Register-ScheduledTask -TaskName "syforce" -Action $Action -Trigger $Trigger -Settings $Settings -Description "syforce — OS force-directed graph" -Force
Start-ScheduledTask -TaskName "syforce"
Write-Host "syforce task registered. Open http://localhost:9001/  (allow the port in Windows Firewall for LAN access)"
