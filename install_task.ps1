param(
    [string]$Python = 'C:\Users\xc176\anaconda3\python.exe',
    [string]$TaskName = 'Assistant Daily Brief',
    [string]$ServiceTaskName = 'Assistant Plan Service'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root 'daily.py'
if (-not (Test-Path -LiteralPath $Python)) { throw "Conda base Python not found: $Python" }
if (-not (Test-Path -LiteralPath $script)) { throw "Daily script not found: $script" }
$pythonw = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
$serviceScript = Join-Path $root 'plan_service.py'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "Conda base pythonw.exe not found: $pythonw" }
if (-not (Test-Path -LiteralPath $serviceScript)) { throw "Plan service script not found: $serviceScript" }

$action = New-ScheduledTaskAction -Execute $Python -Argument ('"' + $script + '"') -WorkingDirectory $root
$daily = New-ScheduledTaskTrigger -Daily -At '07:30'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($daily, $logon) -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered task: $TaskName (daily 07:30 and at logon)"

$serviceAction = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $serviceScript + '"') -WorkingDirectory $root
$serviceSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $ServiceTaskName -Action $serviceAction -Trigger $logon -Principal $principal -Settings $serviceSettings -Force | Out-Null
Start-ScheduledTask -TaskName $ServiceTaskName
Write-Host "Registered and started task: $ServiceTaskName (at logon)"
