param(
    [string]$Name = "",
    [switch]$All
)

$ErrorActionPreference = "Continue"

function Remove-StaleTunAdapter {
    param([string]$AdapterName)
    $adapter = Get-NetAdapter -Name $AdapterName -ErrorAction SilentlyContinue
    if (-not $adapter) {
        return
    }
    if ([string]$adapter.InterfaceDescription -ne "sing-tun Tunnel" -and [string]$adapter.ComponentID -ne "Wintun") {
        Write-Warning "Refusing to remove non sing-box adapter '$AdapterName' ($($adapter.InterfaceDescription))."
        return
    }
    $instanceId = [string]$adapter.PnPDeviceID
    if (-not $instanceId) {
        Write-Warning "Cannot remove stale adapter '$AdapterName': PnPDeviceID is empty."
        return
    }
    Write-Host "Removing stale sing-box adapter $AdapterName ($instanceId)"
    & pnputil.exe /remove-device "$instanceId" | Out-Null
    Start-Sleep -Seconds 1
}

$stateDir = Join-Path $PSScriptRoot "run"
if (-not (Test-Path -LiteralPath $stateDir) -and -not $Name) {
    return
}

$pidFiles = if ($All) {
    Get-ChildItem -LiteralPath $stateDir -Filter "*.pid" -File
} elseif ($Name) {
    Get-Item -LiteralPath (Join-Path $stateDir "$Name.pid") -ErrorAction SilentlyContinue
} else {
    throw "Pass -Name or -All."
}

if ($Name -and -not $pidFiles) {
    Remove-StaleTunAdapter -AdapterName $Name
    return
}

foreach ($pidFile in @($pidFiles)) {
    if (-not $pidFile) {
        continue
    }
    $nameFromFile = $pidFile.BaseName
    $raw = (Get-Content -Raw -LiteralPath $pidFile.FullName).Trim()
    if ($raw -match "^[0-9]+$") {
        Stop-Process -Id ([int]$raw) -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped sing-box transport $($pidFile.BaseName) pid=$raw"
    }
    Remove-Item -LiteralPath $pidFile.FullName -Force -ErrorAction SilentlyContinue
    Remove-StaleTunAdapter -AdapterName $nameFromFile
}
