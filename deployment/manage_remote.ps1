param(
    [ValidateSet('Install', 'Start', 'Stop', 'Status', 'Uninstall')]
    [string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$supervisorScript = Join-Path $PSScriptRoot 'remote_supervisor.py'
$pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
$configJson = & $pythonExe -X utf8 -c "import json,sys; sys.path.insert(0, sys.argv[1]); from app_config import load_config; print(json.dumps(load_config(sys.argv[1])))" $workspace
if ($LASTEXITCODE -ne 0) { throw '无法加载工作台配置。' }
$workbenchConfig = $configJson | ConvertFrom-Json
$runtime = Join-Path $workbenchConfig.runtime_root 'remote-access'
$privateConfig = Join-Path $runtime 'config.json'
$pythonWindowless = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup '案例工作台完整公网.lnk'
$taskName = 'CaseWorkbenchRemoteAccess'
$supervisorArguments = '-B -X utf8 "' + $supervisorScript + '" --config "' + $privateConfig + '"'

function Get-RemoteTask {
    $task = Get-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and (@($task.Actions).Count -ne 1 -or @($task.Actions | Where-Object { $_.Arguments -eq $supervisorArguments -and $_.Execute -eq $pythonWindowless }).Count -ne 1)) {
        throw '同名计划任务不属于此工作台，未修改。'
    }
    return $task
}

function Check-Remote {
    if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) { throw '没有找到 Python 后台运行组件。' }
    & $pythonExe -B -X utf8 $supervisorScript --config $privateConfig --check
    if ($LASTEXITCODE -ne 0) { throw '公网私有配置未就绪，未启动或设置自动运行。' }
}

function Start-Remote {
    if (Get-RemoteTask) {
        Enable-ScheduledTask -TaskPath '\' -TaskName $taskName | Out-Null
        Start-ScheduledTask -TaskPath '\' -TaskName $taskName
        return
    }
    Start-Process -FilePath $pythonWindowless -ArgumentList $supervisorArguments -WorkingDirectory $workspace -WindowStyle Hidden
}

function Stop-Remote {
    # Disable recovery first so an intentional stop stays stopped.
    $task = Get-RemoteTask
    if ($task) { Disable-ScheduledTask -TaskPath '\' -TaskName $taskName | Out-Null }
    & $pythonExe -B -X utf8 $supervisorScript --stop
    if ($LASTEXITCODE -ne 0) { throw '公网进程尚未完全退出，请查看状态。' }
    if ($task) {
        # Disable does not cancel an instance already starting but not yet locked.
        Stop-ScheduledTask -TaskPath '\' -TaskName $taskName
        $finalStatus = & $pythonExe -B -X utf8 $supervisorScript --status | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or $finalStatus.running) { throw '公网进程尚未完全退出，请查看状态。' }
    }
}

switch ($Action) {
    'Install' {
        Check-Remote
        $null = Get-RemoteTask
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $scheduledAction = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $supervisorArguments -WorkingDirectory $workspace
        $loginTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        # A repeating trigger also recovers a clean/unexpected supervisor exit.
        # Its process lock makes invocation safe if a manual instance is alive.
        $recoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval ([TimeSpan]::FromMinutes(1))
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval ([TimeSpan]::FromMinutes(1)) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskPath '\' -TaskName $taskName -Action $scheduledAction -Trigger @($loginTrigger, $recoveryTrigger) -Settings $settings -Principal $principal -Description '完整工作台公网连接；登录启动，每分钟检查并恢复退出的管理进程。' -Force | Out-Null
        if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
        Start-Remote
        Write-Output '已设置登录启动及每分钟自动恢复，并启动完整工作台公网连接。'
    }
    'Start' {
        Check-Remote
        Start-Remote
        Write-Output '已启动完整工作台公网管理进程；连接状态请使用 Status 查看。'
    }
    'Stop' {
        Stop-Remote
        Write-Output '已停止本工作台的公网连接及自动恢复；使用 Start 可重新启用。本机工作台和其他应用的隧道继续运行。'
    }
    'Status' {
        & $pythonExe -B -X utf8 $supervisorScript --status
        if ($LASTEXITCODE -ne 0) { throw '无法读取公网连接状态。' }
        $task = Get-RemoteTask
        if ($task) { Write-Output ('自动恢复计划任务：' + $task.State) }
        else { Write-Output '自动恢复计划任务：未安装' }
    }
    'Uninstall' {
        Stop-Remote
        if (Get-RemoteTask) { Unregister-ScheduledTask -TaskPath '\' -TaskName $taskName -Confirm:$false }
        if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
        Write-Output '已停止公网连接并取消当前用户自动启动；保留私有配置与 Cloudflare 设置。'
    }
}
