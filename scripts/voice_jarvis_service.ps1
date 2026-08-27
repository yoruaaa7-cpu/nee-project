# Jarvis voice mode — auto-start manager (no admin rights needed).
#
# Puts a hidden launcher in the Windows Startup folder so voice_jarvis.py
# runs automatically at every logon, invisibly. Works for standard users —
# no scheduled tasks, no elevation.
#
# Usage (regular PowerShell):
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

$SrcDir   = Join-Path $env:LOCALAPPDATA "OpenJarvis\src"
$LogFile  = Join-Path $env:LOCALAPPDATA "OpenJarvis\voice_jarvis.log"
$Startup  = [Environment]::GetFolderPath("Startup")
$Launcher = Join-Path $Startup "JarvisVoice.vbs"

function Get-JarvisProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*voice_jarvis.py*" }
}

function Stop-Jarvis {
    # stop the voice backend AND the desktop widget window
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*voice_jarvis.py*" -or $_.CommandLine -like "*jarvis_widget.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Start-Widget {
    # frameless always-on-top desktop panel (separate GUI process); waits for
    # the backend's dashboard server to come up first
    $wcmd = "Start-Sleep -Seconds 12; Set-Location '$SrcDir'; uv run python jarvis_widget.py"
    Start-Process -FilePath "powershell.exe" -WindowStyle Hidden `
        -ArgumentList "-NoProfile", "-Command", $wcmd
}

function New-LauncherFile {
    # -u = unbuffered, so [voice] messages & errors reach the log immediately.
    $psCommand = "`$env:PYTHONUNBUFFERED='1'; Set-Location '$SrcDir'; uv run python -u voice_jarvis.py --browser --desktop --remote *>> '$LogFile'"
    $vbs = @"
' Auto-generated: starts Jarvis voice mode hidden at logon.
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -WindowStyle Hidden -Command ""$psCommand""", 0, False
"@
    Set-Content -Path $Launcher -Value $vbs -Encoding ASCII
}

function Start-Jarvis {
    if (-not (Test-Path $Launcher)) { New-LauncherFile }
    if (Get-JarvisProcess) {
        Write-Host "[ok]   Jarvis voice is already running." -ForegroundColor Green
        return
    }
    Start-Process -FilePath "wscript.exe" -ArgumentList "`"$Launcher`""
    Start-Widget
    Write-Host "[ok]   Started (hidden). The Jarvis panel appears on the side in ~15s." -ForegroundColor Green
    Write-Host "       Log: $LogFile"
}

switch ($Command) {
    "install" {
        if (-not (Test-Path (Join-Path $SrcDir "voice_jarvis.py"))) {
            Write-Host "[fail] voice_jarvis.py not found in $SrcDir - download it there first." -ForegroundColor Red
            exit 1
        }
        New-LauncherFile
        Write-Host "[ok]   Launcher placed in your Startup folder:" -ForegroundColor Green
        Write-Host "       $Launcher"
        Write-Host "[ok]   Jarvis voice will auto-start (hidden) at every logon." -ForegroundColor Green
        Start-Jarvis
    }

    "uninstall" {
        Stop-Jarvis
        Remove-Item $Launcher -ErrorAction SilentlyContinue
        Write-Host "[ok]   Removed. Jarvis voice will no longer auto-start." -ForegroundColor Green
    }

    "start" { Start-Jarvis }

    "stop" {
        Stop-Jarvis
        Write-Host "[ok]   Stopped (starts again at next logon; 'uninstall' removes it for good)." -ForegroundColor Green
    }

    "status" {
        if (Test-Path $Launcher) {
            Write-Host "Auto-start : installed ($Launcher)"
        } else {
            Write-Host "Auto-start : NOT installed"
        }
        $running = Get-JarvisProcess
        if ($running) {
            Write-Host "Process    : RUNNING (pid $(($running | Select-Object -First 1).ProcessId))" -ForegroundColor Green
        } else {
            Write-Host "Process    : not running" -ForegroundColor Yellow
        }
        if (Test-Path $LogFile) {
            Write-Host "`nLast log lines:"
            Get-Content $LogFile -Tail 8
        }
    }
}
