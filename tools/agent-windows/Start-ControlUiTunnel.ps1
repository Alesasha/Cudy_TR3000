param(
    [string]$HostName = "95.182.91.203",
    [string]$User = "cudy-tunnel-windows",
    [Parameter(Mandatory = $true)][string]$KeyPath,
    [int]$LocalPort = 18765,
    [int]$RemotePort = 8765,
    [string]$LogPath = "$env:LOCALAPPDATA\CudyAgent\control-ui-tunnel.log",
    [int]$RetrySeconds = 10
)

$ErrorActionPreference = "Stop"

function Write-TunnelLog {
    param([string]$Message)
    $parent = Split-Path -Parent $LogPath
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -LiteralPath $LogPath -Value "[$stamp] $Message" -Encoding UTF8
}

function Test-ControlHealth {
    try {
        $response = Invoke-RestMethod -UseBasicParsing -Uri "http://127.0.0.1:$LocalPort/healthz" -TimeoutSec 4
        return $response.ok -eq $true
    } catch {
        return $false
    }
}

function Stop-StaleManagedTunnel {
    param([Parameter(Mandatory = $true)]$Listener)

    $ownerPid = [int]$Listener.OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerPid" -ErrorAction SilentlyContinue
    if (-not $process -or $process.Name -ne "ssh.exe") {
        return $false
    }

    $forward = "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}"
    if ($process.CommandLine -notlike "*$forward*" -or $process.CommandLine -notlike "*${User}@${HostName}*") {
        return $false
    }

    Write-TunnelLog "Stopping unhealthy managed SSH forward pid=$ownerPid."
    Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    return $true
}

if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "SSH key not found: $KeyPath"
}
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) {
    throw "Windows OpenSSH client is not installed."
}

Write-TunnelLog "Control UI tunnel supervisor started: 127.0.0.1:$LocalPort -> ${HostName}:127.0.0.1:$RemotePort"

while ($true) {
    if (Test-ControlHealth) {
        Start-Sleep -Seconds 20
        continue
    }

    $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Start-Sleep -Seconds 2
        if (Test-ControlHealth) {
            continue
        }
        if (Stop-StaleManagedTunnel -Listener @($listener)[0]) {
            Start-Sleep -Seconds 2
            continue
        }
        Write-TunnelLog "Port $LocalPort is occupied by an unmanaged process; leaving it untouched."
        Start-Sleep -Seconds $RetrySeconds
        continue
    }

    Write-TunnelLog "Opening SSH forward via ${User}@${HostName}."
    $sshArgs = @(
        "-i", $KeyPath,
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=15",
        "-o", "ConnectionAttempts=2",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-N",
        "-L", "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}",
        "${User}@${HostName}"
    )

    $sshOutput = & ssh.exe @sshArgs 2>&1 | Out-String
    $sshExitCode = $LASTEXITCODE
    if ($sshOutput.Trim()) {
        Write-TunnelLog ("ssh output: " + ($sshOutput.Trim() -replace "[\r\n]+", " | "))
    }
    Write-TunnelLog "SSH forward exited with code $sshExitCode; retrying in ${RetrySeconds}s."
    Start-Sleep -Seconds $RetrySeconds
}
