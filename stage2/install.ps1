# SigmaSteamBot installer: autologon, power settings, scheduled task.
# Run on the VM:  powershell -ExecutionPolicy Bypass -File C:\Users\alex\gamebot\stage2\install.ps1
$ErrorActionPreference = 'Stop'

$Base = 'C:\Users\alex\gamebot\stage2'
$Venv = 'C:\Users\alex\gamebot\venv\Scripts'
$PyW  = Join-Path $Venv 'pythonw.exe'
$Py   = Join-Path $Venv 'python.exe'
if (-not (Test-Path $PyW)) { $PyW = $Py }
$User = 'alex'
$Pass = '1'

New-Item -ItemType Directory -Force -Path $Base, (Join-Path $Base 'logs') | Out-Null

# --- Autologon ---
$wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $wl -Name AutoAdminLogon   -Value '1'
Set-ItemProperty $wl -Name DefaultUserName  -Value $User
Set-ItemProperty $wl -Name DefaultPassword  -Value $Pass
Set-ItemProperty $wl -Name DefaultDomainName -Value $env:COMPUTERNAME
Remove-ItemProperty $wl -Name AutoLogonCount -ErrorAction SilentlyContinue
Write-Output "[ok] autologon set for $User@$env:COMPUTERNAME"

# --- Never sleep / blank screen ---
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change monitor-timeout-ac 0 | Out-Null
powercfg /change disk-timeout-ac 0    | Out-Null
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -Value '0' -ErrorAction SilentlyContinue
Write-Output "[ok] sleep/screensaver disabled"

# --- Scheduled task: SigmaSteamBot (at logon) ---
$action  = New-ScheduledTaskAction -Execute $PyW -Argument 'supervisor.py' -WorkingDirectory $Base
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$princ   = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
              -RestartInterval (New-TimeSpan -Minutes 2) -RestartCount 999 `
              -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'SigmaSteamBot' -Action $action -Trigger $trigger `
    -Principal $princ -Settings $set -Force | Out-Null
Write-Output "[ok] task SigmaSteamBot registered (exe: $PyW)"

# --- Scheduled task: SigmaNav (runs UI sequences with real window focus) ---
$navAct = New-ScheduledTaskAction -Execute (Join-Path $Base '_navrun.cmd') -WorkingDirectory $Base
$navSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 8)
Register-ScheduledTask -TaskName 'SigmaNav' -Action $navAct `
    -Principal (New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest) `
    -Settings $navSet -Force | Out-Null
Write-Output "[ok] task SigmaNav registered"

# --- Scheduled task: SigmaConsoleGuard (tscon session back to console on RDP disconnect) ---
$cgAct  = New-ScheduledTaskAction -Execute 'powershell.exe' `
              -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $Base 'console_guard.ps1') + '"')
$cgTrig = New-CimInstance -CimClass (Get-CimClass MSFT_TaskEventTrigger root/Microsoft/Windows/TaskScheduler) -ClientOnly
$cgTrig.Enabled = $true
$cgTrig.Subscription = @'
<QueryList><Query Id="0" Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"><Select Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational">*[System[(EventID=24 or EventID=40)]]</Select></Query></QueryList>
'@
Register-ScheduledTask -TaskName 'SigmaConsoleGuard' -Action $cgAct -Trigger $cgTrig `
    -Principal (New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest) `
    -Settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew) -Force | Out-Null
Write-Output "[ok] task SigmaConsoleGuard registered (RDP-disconnect -> tscon console)"

# --- Firewall rule for the Web UI port (webui.host:webui.port from config.json) ---
$WebPort = 8080
try {
  $cfgFile = Join-Path $Base 'config.json'
  if (Test-Path $cfgFile) {
    $wj = (Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json).webui
    if ($wj -and $wj.port) { $WebPort = [int]$wj.port }
  }
} catch { Write-Output "[--] could not read webui.port from config.json, using $WebPort" }
Get-NetFirewallRule -DisplayName 'SigmaSteamBot Web UI' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName 'SigmaSteamBot Web UI' -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $WebPort -Profile Any | Out-Null
Write-Output "[ok] firewall: inbound TCP $WebPort allowed (Web UI). Open http://<vm-ip>:$WebPort/  (login admin/admin, change on first use)"

# --- панель игроков (playerweb.enabled/port из config.json): своё правило фаервола ---
Get-NetFirewallRule -DisplayName 'SigmaSteamBot Player Panel' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
try {
  $pj = $null
  if (Test-Path (Join-Path $Base 'config.json')) { $pj = (Get-Content (Join-Path $Base 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json).playerweb }
  if ($pj -and $pj.enabled) {
    $PlPort = 80
    if ($pj.port) { $PlPort = [int]$pj.port }
    # HTTPS (playerweb.https, включается в «Настройках») — порт открываем заранее
    $HttpsPort = 443
    if ($pj.https -and $pj.https.port) { $HttpsPort = [int]$pj.https.port }
    New-NetFirewallRule -DisplayName 'SigmaSteamBot Player Panel' -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $PlPort, $HttpsPort -Profile Any | Out-Null
    Write-Output "[ok] firewall: inbound TCP $PlPort, $HttpsPort allowed (player panel, HTTPS)"
  }
} catch { Write-Output "[--] playerweb: $_" }

Get-ScheduledTask -TaskName 'SigmaSteamBot', 'SigmaNav', 'SigmaConsoleGuard' | Select-Object TaskName, State | Format-Table -AutoSize
Write-Output "Done. Start now with:  Start-ScheduledTask -TaskName SigmaSteamBot"
