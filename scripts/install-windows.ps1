<#
.SYNOPSIS
    把 campus-net-login 注册成 Windows 计划任务，实现开机自启 + 断线自动重连。

.DESCRIPTION
    创建两个触发器：
      * 登录时      -> 常驻运行 `watch`（后台看门狗，静默窗口）
      * 每 5 分钟   -> 运行 `watch --once` 兜底（万一常驻进程挂了也能自己起来）

    因为密码用 Windows DPAPI 加密，密文只能被“保存它的那个用户”解开，
    所以任务必须跑在你的用户账号下（登录后启动），不能用 SYSTEM。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -IntervalMinutes 2
    powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'CampusNetLogin',
    [int]$IntervalMinutes = 5,
    [string]$Python = '',
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "[*] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[+] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[!] $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    foreach ($name in @($TaskName, "$TaskName-Fallback")) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Ok "已删除计划任务 $name"
        } else {
            Write-Warn2 "没有找到计划任务 $name"
        }
    }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*campus_login*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    return
}

# ---------------------------------------------------------------- locate
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'campus_login.py'
if (-not (Test-Path $script)) { throw "找不到 $script" }

$configCandidates = @(
    $env:CAMPUS_LOGIN_CONFIG,
    (Join-Path $repo 'config.json'),
    (Join-Path $env:APPDATA 'campus-net-login\config.json')
)
$config = $configCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $config) {
    throw "还没有配置文件。先运行:  python `"$script`" init"
}
Write-Ok "配置文件: $config"

# pythonw.exe 启动时不会弹黑框
if (-not $Python) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw 'PATH 里找不到 python，请用 -Python 指定完整路径' }
    $cand = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
    $Python = if (Test-Path $cand) { $cand } else { $py }
}
Write-Ok "解释器  : $Python"

# ---------------------------------------------------------------- register
Write-Step "注册计划任务 $TaskName"

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$script`" --config `"$config`" watch" `
    -WorkingDirectory $repo

$onceAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$script`" --config `"$config`" watch --once" `
    -WorkingDirectory $repo

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes))
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -Hidden

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

# 触发器 1 跑常驻 watch，触发器 2 跑 --once 兜底
$t1 = $triggers[0]
$t2 = $triggers[1]

$task = New-ScheduledTask -Action $action -Trigger $t1 -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

$fallback = New-ScheduledTask -Action $onceAction -Trigger $t2 -Settings $settings -Principal $principal
Register-ScheduledTask -TaskName "$TaskName-Fallback" -InputObject $fallback -Force | Out-Null

Write-Ok "计划任务已注册: $TaskName  /  $TaskName-Fallback"

if (-not $NoStart) {
    Write-Step '立刻启动一次'
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Ok "任务状态: $state"
}

Write-Host ''
Write-Ok '完成。现在断线后会自动重连。'
Write-Host "    查看日志: Get-Content `"$env:APPDATA\campus-net-login\campus-login.log`" -Tail 30 -Wait"
Write-Host "    卸载    : powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
