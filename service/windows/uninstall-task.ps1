<#
.SYNOPSIS
  Remove the syforce Scheduled Task and firewall rule. Files stay unless -RemoveFiles.
  Run from an elevated PowerShell:  powershell -NoProfile -ExecutionPolicy Bypass -File service\windows\uninstall-task.ps1
#>
[CmdletBinding()]
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "syforce"),
  [string]$TaskName = "syforce",
  [switch]$RemoveFiles
)
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[syforce] $m" }

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Say "task '$TaskName' removed"
} else { Say "task '$TaskName' not found" }

$esc = [regex]::Escape($InstallDir)
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match $esc } |
  ForEach-Object { Say "stopping pid $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$rules = Get-NetFirewallRule -Name "syforce-tcp-*" -ErrorAction SilentlyContinue
if ($rules) { $rules | Remove-NetFirewallRule; Say "firewall rule(s) removed" }

if ($RemoveFiles -and (Test-Path $InstallDir)) {
  Start-Sleep -Seconds 1
  Remove-Item -Recurse -Force $InstallDir
  Say "removed $InstallDir"
} elseif (Test-Path $InstallDir) { Say "files kept in $InstallDir (use -RemoveFiles to delete)" }
