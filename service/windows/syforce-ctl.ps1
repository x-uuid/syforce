<#
.SYNOPSIS
  syforce-ctl.ps1 start|stop|restart|status   (run elevated; works when the install dir is the default)
  start   = Start-ScheduledTask; if Task Scheduler leaves it Queued, launch the supervisor via WMI instead
  stop    = Stop-ScheduledTask and stop the supervisor + server processes of this install
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)][ValidateSet("start", "stop", "restart", "status")][string]$Command = "status",
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "syforce"),
  [string]$TaskName = "syforce"
)
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[syforce] $m" }
$esc = [regex]::Escape($InstallDir)
function Get-Procs { Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match $esc } }
function Get-Port {
  $a = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).Actions | Select-Object -First 1
  if ($a -and $a.Arguments -match "--port (\d+)") { return [int]$Matches[1] }
  return 9001
}
function Test-Up($port) {
  try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/healthz").Content -eq "ok" } catch { return $false }
}
function Stop-All {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Get-Procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Say "stopped"
}
function Start-All {
  $port = Get-Port
  if (Test-Up $port) { Say "already running on :$port"; return }
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep 3                                   # a refused localhost connect costs ~2 s on Windows: poll sparingly
  if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Queued") { for ($i = 0; $i -lt 5 -and -not (Test-Up $port); $i++) { Start-Sleep 1 } }
  if (-not (Test-Up $port)) {
    $a = (Get-ScheduledTask -TaskName $TaskName).Actions | Select-Object -First 1
    Say "task state $((Get-ScheduledTask -TaskName $TaskName).State); starting the supervisor via WMI"
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "`"$($a.Execute)`" $($a.Arguments)"; CurrentDirectory = $a.WorkingDirectory } | Out-Null
    for ($i = 0; $i -lt 20 -and -not (Test-Up $port); $i++) { Start-Sleep 1 }
  }
  if (Test-Up $port) { Say "running on :$port" } else { Say "not answering; see $InstallDir\syforce.log" }
}
switch ($Command) {
  "stop" { Stop-All }
  "start" { Start-All }
  "restart" { Stop-All; Start-Sleep 2; Start-All }
  "status" {
    $port = Get-Port
    Say ("task: " + (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State + "  http :$port " + $(if (Test-Up $port) { "up" } else { "down" }))
    Get-Procs | ForEach-Object { Say ("pid {0,-6} {1}" -f $_.ProcessId, $_.CommandLine) }
  }
}
