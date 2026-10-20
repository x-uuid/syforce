<#
.SYNOPSIS
  Install syforce as a Windows Scheduled Task (at startup + at logon, restarts on exit, hidden, no console).

.DESCRIPTION
  Run from an ELEVATED PowerShell (an admin user's OpenSSH session is elevated):
      powershell -NoProfile -ExecutionPolicy Bypass -File service\windows\install-task.ps1
  Steps: copy app\ to -InstallDir (config.json there is kept), create venv, pip install -r requirements.txt,
  register task "syforce" (runs as you via S4U, limited rights, pythonw + supervisor), open TCP -Port on the
  Private firewall profile, start it and wait for /healthz. Re-running upgrades in place.
#>
[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "syforce"),
  [int]$Port = 9001,
  [string]$Bind = "0.0.0.0",
  [string]$Python = "",                     # python.exe to build the venv with; default: py -3, then python
  [string]$TaskName = "syforce",
  [string[]]$FirewallProfile = @("Private"),
  [switch]$NoFirewall,
  [switch]$PublicLanSubnet,                 # also allow the local subnet on Public-profile networks (LAN marked Public)
  [switch]$NoStart
)
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[syforce] $m" }

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this from an elevated PowerShell (needed for the startup trigger and the firewall rule)."
}

$Kit = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$SrcApp = Join-Path $Kit "app"
if (-not (Test-Path (Join-Path $SrcApp "server.py"))) { throw "app\server.py not found under $Kit" }
$App = Join-Path $InstallDir "app"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# ---- stop a running copy before touching files
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Say "stopping existing task"
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
$esc = [regex]::Escape($InstallDir)
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match $esc } |
  ForEach-Object { Say "killing old pid $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ---- copy files (keeps config.json, venv and logs in the install dir)
if ((Resolve-Path $SrcApp).Path -ne [IO.Path]::GetFullPath($App)) {
  Say "copying app -> $App"
  robocopy $SrcApp $App /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ venv .venv /XF config.json "syforce.log*" "*.bak" | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
}
foreach ($f in "syforce-supervisor.pyw", "syforce-ctl.ps1", "uninstall-task.ps1") { Copy-Item (Join-Path $PSScriptRoot $f) (Join-Path $InstallDir $f) -Force }

# ---- python + venv
function Test-Py($exe, $pre) {
  try {
    $out = & $exe @pre -c "import sys; print(sys.version_info >= (3, 9)); print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out[0] -eq "True" -and $out[1] -notmatch "WindowsApps") { return $out[1] }
  } catch {}
  return $null
}
$Venv = Join-Path $InstallDir "venv"
$VPy = Join-Path $Venv "Scripts\python.exe"
$VPyw = Join-Path $Venv "Scripts\pythonw.exe"
if (-not (Test-Path $VPy)) {
  $base = $null
  if ($Python) { $base = Test-Py $Python @() }
  if (-not $base -and (Get-Command py -ErrorAction SilentlyContinue)) { $base = Test-Py "py" @("-3") }
  if (-not $base -and (Get-Command python -ErrorAction SilentlyContinue)) { $base = Test-Py "python" @() }
  if (-not $base) { throw "Python >= 3.9 not found (the Microsoft Store alias does not work for tasks). Install from python.org or pass -Python C:\path\python.exe" }
  Say "creating venv with $base"
  & $base -m venv $Venv
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
Say "pip install -r requirements.txt"
& $VPy -m pip install --disable-pip-version-check -q -r (Join-Path $App "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $VPy -c "import psutil, sys; sys.path.insert(0, r'$App'); import syforce.collectors.windows"
if ($LASTEXITCODE -ne 0) { throw "import check failed" }

# ---- scheduled task: S4U = runs as this user without a stored password, with or without a logon session
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name     # $env:USERDOMAIN is "WORKGROUP" under OpenSSH
$sup = Join-Path $InstallDir "syforce-supervisor.pyw"
$action = New-ScheduledTaskAction -Execute $VPyw -Argument "`"$sup`" --bind $Bind --port $Port" -WorkingDirectory $App
$triggers = @((New-ScheduledTaskTrigger -AtStartup), (New-ScheduledTaskTrigger -AtLogOn -User $user))
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -Hidden
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
  -Description "syforce dashboard on ${Bind}:$Port (read-only system monitor)" | Out-Null
Say "task '$TaskName' registered (startup + logon, user $user)"

# ---- firewall
if (-not $NoFirewall) {
  Get-NetFirewallRule -Name "syforce-tcp-*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  New-NetFirewallRule -Name "syforce-tcp-$Port" -DisplayName "syforce (TCP $Port)" -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile ($FirewallProfile -join ",") | Out-Null
  Say "firewall: TCP $Port allowed on profile(s) $($FirewallProfile -join ',')"
  if ($PublicLanSubnet) {
    New-NetFirewallRule -Name "syforce-tcp-$Port-publan" -DisplayName "syforce (TCP $Port, Public LAN subnet)" -Direction Inbound `
      -Action Allow -Protocol TCP -LocalPort $Port -Profile Public -RemoteAddress LocalSubnet | Out-Null
    Say "firewall: TCP $Port allowed from the local subnet on Public networks"
  }
}

function Wait-Health($sec) {
  for ($i = 0; $i -lt $sec; $i++) {
    Start-Sleep -Seconds 1
    try { if ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$Port/healthz").Content -eq "ok") { return $true } } catch {}
  }
  return $false
}
if (-not $NoStart) {
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep 3
  $queued = (Get-ScheduledTask -TaskName $TaskName).State -eq "Queued"
  $ok = if ($queued) { $false } else { Wait-Health 20 }
  if (-not $ok -and $queued) {
    # a wedged Task Scheduler leaves every task "Queued" until reboot; start the supervisor via WMI so it outlives
    # this (ssh) session. The supervisor is single-instance, so the task starting later is harmless.
    Say "Task Scheduler left the task Queued; starting the supervisor directly until the next boot/logon"
    $cl = "`"$VPyw`" `"$sup`" --bind $Bind --port $Port"
    Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cl; CurrentDirectory = $App } | Out-Null
    $ok = Wait-Health 30
  }
  if ($ok) { Say "running: http://localhost:$Port/" }
  else { Say "not answering yet; see $InstallDir\syforce.log" }
}
