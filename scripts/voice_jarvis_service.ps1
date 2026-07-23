# Jarvis voice mode — auto-start manager.
#
# Registers voice_jarvis.py as a Windows scheduled task that launches
# hidden at every logon, so "Hey Jarvis" is always available.
#
# Usage (regular PowerShell, no admin needed):
#   powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 install
#   powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 status
#   powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 stop
#   powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 start
#   powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 uninstall
#
# Output of the hidden Jarvis goes to:  %LOCALAPPDATA%\OpenJarvis\voice_jarvis.log
# Watch it live with:  Get-Content $env:LOCALAPPDATA\OpenJarvis\voice_jarvis.log -Wait -Tail 20

param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "start", "stop", "status")]
    [string]$Command = "install"
)

$TaskName = "Jarvis Voice"
$SrcDir   = Join-Path $env:LOCALAPPDATA "OpenJarvis\src"
$LogFile  = Join-Path $env:LOCALAPPDATA "OpenJarvis\voice_jarvis.log"

function Stop-JarvisProcesses {
    # Stop the task plus any python it spawned from the OpenJarvis venv.
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*voice_jarvis.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

switch ($Command) {
    "install" {
        if (-not (Test-Path (Join-Path $SrcDir "voice_jarvis.py"))) {
            Write-Host "[fail] voice_jarvis.py not found in $SrcDir - download it there first." -ForegroundColor Red
            exit 1
        }

        $inner  = "Set-Location '$SrcDir'; uv run python voice_jarvis.py *>> '$LogFile'"
        $action = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -WindowStyle Hidden -Command `"$inner`""

        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $trigger.Delay = "PT20S"   # give Ollama and audio devices time to come up

        $settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable

        Register-ScheduledTask -TaskName $TaskName -Action $action `
            -Trigger $trigger -Settings $settings -Force | Out-Null

        Write-Host "[ok]   Task '$TaskName' registered - Jarvis will auto-start at every logon." -ForegroundColor Green
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "[ok]   Started now. Give it ~30s to load models, then say 'Hey Jarvis'." -ForegroundColor Green
        Write-Host "       Log: $LogFile"
    }

    "uninstall" {
        Stop-JarvisProcesses
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "[ok]   Task removed. Jarvis voice will no longer auto-start." -ForegroundColor Green
    }

    "start" {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "[ok]   Started. Give it ~30s, then say 'Hey Jarvis'." -ForegroundColor Green
    }

    "stop" {
        Stop-JarvisProcesses
        Write-Host "[ok]   Stopped (will start again at next logon; 'uninstall' removes it for good)." -ForegroundColor Green
    }

    "status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Host "Not installed."
            exit 0
        }
        $info = $task | Get-ScheduledTaskInfo
        Write-Host "Task state : $($task.State)"
        Write-Host "Last run   : $($info.LastRunTime)"
        Write-Host "Last result: $($info.LastTaskResult)"
        $running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
            Where-Object { $_.CommandLine -like "*voice_jarvis.py*" }
        if ($running) {
            Write-Host "Jarvis voice process: RUNNING (pid $($running.ProcessId))" -ForegroundColor Green
        } else {
            Write-Host "Jarvis voice process: not running" -ForegroundColor Yellow
        }
        if (Test-Path $LogFile) {
            Write-Host "`nLast log lines:"
            Get-Content $LogFile -Tail 8
        }
    }
}
