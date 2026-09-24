<#
  SigmaSteamBot — регистрация задач планировщика, энергосбережение, (опц.) автологон.
  Вызывается из setup.bat уже ПОСЛЕ создания venv и копирования файлов в $BaseDir.

  Пример ручного запуска:
    powershell -ExecutionPolicy Bypass -File install.ps1 -BaseDir C:\Users\<USER>\sigmabot -Autologon -User <USER> -Password <ПАРОЛЬ>
#>
param(
  [string]$BaseDir = "$env:USERPROFILE\sigmabot",
  [switch]$Autologon,
  [string]$User = $env:USERNAME,
  [string]$Password = ""
)
$ErrorActionPreference = 'Stop'

$Venv = Join-Path $BaseDir 'venv\Scripts'
$PyW  = Join-Path $Venv 'pythonw.exe'
$Py   = Join-Path $Venv 'python.exe'
if (-not (Test-Path $PyW)) { $PyW = $Py }
if (-not (Test-Path $Py))  { throw "venv не найден в $Venv — сначала отработает setup.bat" }

New-Item -ItemType Directory -Force -Path $BaseDir, (Join-Path $BaseDir 'logs') | Out-Null

# --- энергосбережение: не гасить экран, не спать ---
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change monitor-timeout-ac 0 | Out-Null
powercfg /change disk-timeout-ac 0    | Out-Null
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name ScreenSaveActive -Value '0' -ErrorAction SilentlyContinue
Write-Output "[ok] сон/скринсейвер отключены"

# --- автологон (опционально) ---
if ($Autologon) {
  if (-not $Password) { throw "-Autologon требует -Password" }
  $wl = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
  Set-ItemProperty $wl -Name AutoAdminLogon   -Value '1'
  Set-ItemProperty $wl -Name DefaultUserName  -Value $User
  Set-ItemProperty $wl -Name DefaultPassword  -Value $Password
  Set-ItemProperty $wl -Name DefaultDomainName -Value $env:COMPUTERNAME
  Remove-ItemProperty $wl -Name AutoLogonCount -ErrorAction SilentlyContinue
  Write-Output "[ok] автологон для $User@$env:COMPUTERNAME"
} else {
  Write-Output "[--] автологон пропущен (нет -Autologon)"
}

# --- SigmaSteamBot: supervisor при входе в систему ---
$act  = New-ScheduledTaskAction -Execute $PyW -Argument 'supervisor.py' -WorkingDirectory $BaseDir
$trig = New-ScheduledTaskTrigger -AtLogOn -User $User
$prin = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest
$set  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
          -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
          -RestartInterval (New-TimeSpan -Minutes 2) -RestartCount 999 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'SigmaSteamBot' -Action $act -Trigger $trig -Principal $prin -Settings $set -Force | Out-Null
Write-Output "[ok] задача SigmaSteamBot ($PyW)"

# --- SigmaNav: прогон UI-последовательностей с реальным фокусом окна ---
$navAct = New-ScheduledTaskAction -Execute (Join-Path $BaseDir '_navrun.cmd') -WorkingDirectory $BaseDir
$navSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 8)
Register-ScheduledTask -TaskName 'SigmaNav' -Action $navAct `
    -Principal (New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Highest) `
    -Settings $navSet -Force | Out-Null
Write-Output "[ok] задача SigmaNav"

# --- SigmaConsoleGuard: вернуть сессию на консоль при отключении RDP ---
$cgAct  = New-ScheduledTaskAction -Execute 'powershell.exe' `
            -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $BaseDir 'console_guard.ps1') + '"')
$cgTrig = New-CimInstance -CimClass (Get-CimClass MSFT_TaskEventTrigger root/Microsoft/Windows/TaskScheduler) -ClientOnly
$cgTrig.Enabled = $true
$cgTrig.Subscription = '<QueryList><Query Id="0" Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational"><Select Path="Microsoft-Windows-TerminalServices-LocalSessionManager/Operational">*[System[(EventID=24 or EventID=40)]]</Select></Query></QueryList>'
Register-ScheduledTask -TaskName 'SigmaConsoleGuard' -Action $cgAct -Trigger $cgTrig `
    -Principal (New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest) `
    -Settings (New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew) -Force | Out-Null
Write-Output "[ok] задача SigmaConsoleGuard (RDP-disconnect -> tscon console)"

# --- ярлык "Запустить SigmaSteamBot" на рабочем столе ---
$startCmd = Join-Path $BaseDir 'start-bot.cmd'
if (Test-Path $startCmd) {
  try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $lnk = Join-Path $desktop 'Запустить SigmaSteamBot.lnk'
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath        = $startCmd
    $sc.WorkingDirectory  = $BaseDir
    $sc.IconLocation      = 'shell32.dll,25'
    $sc.Description        = 'Включить и запустить задачу SigmaSteamBot (после /stopbot)'
    $sc.Save()
    Write-Output "[ok] ярлык на рабочем столе: $lnk"
  } catch {
    Write-Output "[--] ярлык на рабочем столе не создан: $_"
  }
} else {
  Write-Output "[--] start-bot.cmd не найден в $BaseDir — ярлык пропущен"
}

# --- правило фаервола на порт веб-панели (webui.host:webui.port из config.json) ---
$WebPort = 8080
try {
  $cfgFile = Join-Path $BaseDir 'config.json'
  if (Test-Path $cfgFile) {
    $wj = (Get-Content $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json).webui
    if ($wj -and $wj.port) { $WebPort = [int]$wj.port }
  }
} catch { Write-Output "[--] не удалось прочитать webui.port из config.json, беру $WebPort" }
Get-NetFirewallRule -DisplayName 'SigmaSteamBot Web UI' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName 'SigmaSteamBot Web UI' -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $WebPort -Profile Any | Out-Null
Write-Output "[ok] фаервол: входящий TCP $WebPort разрешён (веб-панель) -> http://<ip-vm>:$WebPort/  (вход admin/admin, смените при первом входе)"

Get-ScheduledTask -TaskName 'SigmaSteamBot','SigmaNav','SigmaConsoleGuard' | Select-Object TaskName, State | Format-Table -AutoSize

# --- запуск сейчас, чтобы после setup.bat панель уже работала без ручных шагов ---
try {
  Start-ScheduledTask -TaskName 'SigmaSteamBot'
  Write-Output "[ok] задача SigmaSteamBot запущена"
  Start-Sleep -Seconds 5
  try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$WebPort/" -TimeoutSec 5
    Write-Output "[ok] веб-панель отвечает: http://127.0.0.1:$WebPort/  (HTTP $($r.StatusCode))"
  } catch {
    Write-Output "[--] веб-панель пока не отвечает на порту $WebPort — это нормально в первые 10-15 сек (запуск Python), либо смотрите $BaseDir\logs\supervisor.log"
  }
} catch {
  Write-Output "[--] не удалось запустить задачу SigmaSteamBot автоматически: $_"
  Write-Output "     Запустите вручную:  Start-ScheduledTask -TaskName SigmaSteamBot"
}

Write-Output ""
Write-Output "Готово. Панель: http://<ip-этой-машины>:$WebPort/  (вход admin/admin, панель попросит сменить пароль)."
Write-Output "Telegram-токен, пути к игре и всё остальное — во вкладке «Настройки» самой панели, редактировать config.json руками не обязательно."
Write-Output "Ярлык 'Запустить SigmaSteamBot' на рабочем столе — если задачу когда-нибудь остановят (/stopbot), включить обратно можно им."
Write-Output "Логи: $BaseDir\logs\supervisor.log"
