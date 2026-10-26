param(
    [ValidateSet('Install', 'Start', 'Stop', 'Status', 'Uninstall')]
    [string]$Action = 'Status'
)
$ErrorActionPreference = 'Stop'
$taskName = 'CaseWorkbenchTextPublicSync'
$workspace = Split-Path -Parent $PSScriptRoot
$syncScript = Join-Path $PSScriptRoot 'sync_text_site.py'
$pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
$configJson = & $pythonExe -X utf8 -c "import json,sys; sys.path.insert(0, sys.argv[1]); from app_config import load_config; print(json.dumps(load_config(sys.argv[1])))" $workspace
if ($LASTEXITCODE -ne 0) { throw '无法加载工作台配置。' }
$config = $configJson | ConvertFrom-Json
$runtime = Join-Path $config.runtime_root 'text-public-sync'
$pythonWindowless = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
$nodeExe = (Get-Command node.exe).Source
$syncArguments = '-X utf8 "' + $syncScript + '" --node "' + $nodeExe + '"'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw '没有找到 Python 后台运行组件。' }
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup '案例工作台文字自动同步.lnk'

function Start-Sync {
    # The daemon owns a process lock, so invoking this twice is harmless.
    Start-Process -FilePath $pythonWindowless -ArgumentList $syncArguments -WorkingDirectory $workspace -WindowStyle Hidden
}

function Stop-Sync {
    # Only stop this workspace's exact Python daemon. Never stop the workbench.
    $targets = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and $_.CommandLine -and
        $_.CommandLine.Contains('"' + $syncScript + '"')
    }
    foreach ($target in $targets) { Stop-Process -Id $target.ProcessId -ErrorAction SilentlyContinue }
}

switch ($Action) {
    'Install' {
        if (-not (Test-Path -LiteralPath $syncScript)) { throw '同步程序尚未就绪。' }
        try {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $scheduledAction = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $syncArguments -WorkingDirectory $workspace
            $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1)) -MultipleInstances IgnoreNew
            $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
            Register-ScheduledTask -TaskName $taskName -Action $scheduledAction -Trigger $trigger -Settings $settings -Principal $principal -Description '本机工作台已保存的文字变化后自动更新 Cloudflare 公网文字站。' -Force | Out-Null
            if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
            Start-ScheduledTask -TaskName $taskName
            Write-Output '已设置登录后自动同步，并启动后台同步。'
        } catch {
            # Per-user Startup needs no administrator permission.
            $shellObject = New-Object -ComObject WScript.Shell
            $shortcut = $shellObject.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = $pythonWindowless
            $shortcut.Arguments = $syncArguments
            $shortcut.WorkingDirectory = $workspace
            $shortcut.Description = '案例工作台文字自动同步'
            $shortcut.WindowStyle = 7
            $shortcut.Save()
            Start-Sync
            Write-Output '已设置登录后自动同步，并启动后台同步（用户启动项）。'
        }
    }
    'Start' { Start-Sync; Write-Output '已启动后台同步。' }
    'Stop' {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
        Stop-Sync
        Write-Output '已停止后台同步。下次登录仍会自动启动。'
    }
    'Uninstall' {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
        if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
        Stop-Sync
        Write-Output '已停止同步并取消自动启动，公网保留最后一次内容。'
    }
    'Status' { & $pythonExe $syncScript --status }
}
