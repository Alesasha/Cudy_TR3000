param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [string]$SingBoxExe = "",
    [int]$StartupSeconds = 6,
    [switch]$Restart,
    [switch]$QuietIfRunning
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Start-SingBoxTransport.ps1 must be run as Administrator."
    }
}

function Read-PidFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $raw = (Get-Content -Raw -LiteralPath $Path).Trim()
    if ($raw -notmatch "^[0-9]+$") {
        return $null
    }
    return [int]$raw
}

function Remove-StaleTunAdapter {
    param([string]$AdapterName)
    $adapter = Get-NetAdapter -Name $AdapterName -ErrorAction SilentlyContinue
    if (-not $adapter) {
        return
    }
    if ([string]$adapter.InterfaceDescription -ne "sing-tun Tunnel" -and [string]$adapter.ComponentID -ne "Wintun") {
        throw "Refusing to remove non sing-box adapter '$AdapterName' ($($adapter.InterfaceDescription))."
    }
    $instanceId = [string]$adapter.PnPDeviceID
    if (-not $instanceId) {
        throw "Cannot remove stale adapter '$AdapterName': PnPDeviceID is empty."
    }
    Write-Host "Removing stale sing-box adapter $AdapterName ($instanceId)"
    & pnputil.exe /remove-device "$instanceId" | Out-Null
    Start-Sleep -Seconds 2
    if (Get-NetAdapter -Name $AdapterName -ErrorAction SilentlyContinue) {
        throw "Stale adapter '$AdapterName' still exists after removal attempt."
    }
}

Assert-Admin

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "sing-box config not found: $ConfigPath"
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$SingBoxExe = & "$PSScriptRoot\Find-SingBox.ps1" -SingBoxExe $SingBoxExe

$stateDir = Join-Path $PSScriptRoot "run"
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $stateDir, $logDir | Out-Null
$pidPath = Join-Path $stateDir "$Name.pid"
$stdoutLogPath = Join-Path $logDir "$Name.out.log"
$stderrLogPath = Join-Path $logDir "$Name.err.log"

$transportPid = Read-PidFile $pidPath
if ($transportPid) {
    $proc = Get-Process -Id $transportPid -ErrorAction SilentlyContinue
    if ($proc -and -not $Restart) {
        $adapter = Get-NetAdapter -Name $Name -ErrorAction SilentlyContinue
        if (-not $adapter) {
            Write-Host "sing-box transport process is running but adapter is missing; restarting: $Name pid=$transportPid"
            Stop-Process -Id $transportPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
            Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
            $proc = $null
        } elseif ([string]$adapter.InterfaceDescription -ne "sing-tun Tunnel" -and [string]$adapter.ComponentID -ne "Wintun") {
            throw "Adapter '$Name' already exists and is not a sing-box adapter ($($adapter.InterfaceDescription))."
        } else {
            if (-not $QuietIfRunning) {
                Write-Host "sing-box transport already running: $Name pid=$transportPid"
            }
            return
        }
    }
    if ($proc -and -not (Get-Process -Id $transportPid -ErrorAction SilentlyContinue)) {
        $proc = $null
    }
    if ($proc -and -not $Restart) {
        if (-not $QuietIfRunning) {
            Write-Host "sing-box transport already running: $Name pid=$transportPid"
        }
        return
    }
    if ($proc) {
        Stop-Process -Id $transportPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    if (-not (Get-Process -Id $transportPid -ErrorAction SilentlyContinue)) {
        Remove-StaleTunAdapter -AdapterName $Name
    }
    if (-not $proc) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        Remove-StaleTunAdapter -AdapterName $Name
    }
} else {
    $existingAdapter = Get-NetAdapter -Name $Name -ErrorAction SilentlyContinue
    if ($existingAdapter -and -not $Restart) {
        if ([string]$existingAdapter.InterfaceDescription -eq "sing-tun Tunnel" -or [string]$existingAdapter.ComponentID -eq "Wintun") {
            if (-not $QuietIfRunning) {
                Write-Host "sing-box transport adapter already present: $Name"
            }
            return
        }
        throw "Adapter '$Name' already exists and is not a sing-box adapter ($($existingAdapter.InterfaceDescription))."
    }
    Remove-StaleTunAdapter -AdapterName $Name
}

& $SingBoxExe check -c $ConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "sing-box check failed: $ConfigPath"
}

$proc = Start-Process -WindowStyle Hidden -FilePath $SingBoxExe -ArgumentList @(
    "run",
    "-c", "`"$ConfigPath`""
) -RedirectStandardOutput $stdoutLogPath -RedirectStandardError $stderrLogPath -PassThru

Set-Content -LiteralPath $pidPath -Value $proc.Id -Encoding ASCII
Start-Sleep -Seconds $StartupSeconds

$running = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
if (-not $running) {
    $tail = ""
    if (Test-Path -LiteralPath $stdoutLogPath) {
        $tail += "== stdout ==`n" + (Get-Content -Tail 80 -LiteralPath $stdoutLogPath | Out-String)
    }
    if (Test-Path -LiteralPath $stderrLogPath) {
        $tail += "== stderr ==`n" + (Get-Content -Tail 80 -LiteralPath $stderrLogPath | Out-String)
    }
    throw "sing-box transport exited: $Name`n$tail"
}

Write-Host "sing-box transport running: $Name pid=$($proc.Id) config=$ConfigPath"
