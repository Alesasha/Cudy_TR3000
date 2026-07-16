param(
    [switch]$DisconnectWifi
)

$ErrorActionPreference = "Continue"
$stateDir = Join-Path $env:ProgramData "CudyVPN\maintenance-guard"
$statePath = Join-Path $stateDir "state.json"
$pidPath = Join-Path $stateDir "monitor.pid"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    $args = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"{0}"' -f $PSCommandPath))
    if ($DisconnectWifi) { $args += "-DisconnectWifi" }
    $process = Start-Process powershell.exe -Verb RunAs -ArgumentList ($args -join " ") -Wait -PassThru
    exit $process.ExitCode
}

$state = $null
if (Test-Path -LiteralPath $statePath) {
    try { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch {}
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath $pidPath) {
    $monitorPid = [int](Get-Content -LiteralPath $pidPath -Raw)
    Stop-Process -Id $monitorPid -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

if ($state -and $state.tunnel_endpoint -and $state.wifi_interface_index) {
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "$($state.tunnel_endpoint)/32" -InterfaceIndex ([int]$state.wifi_interface_index) -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}

if ($DisconnectWifi -and $state -and $state.wifi_alias) {
    & netsh.exe wlan disconnect interface="$($state.wifi_alias)" | Out-Null
}

Write-Host "Cudy maintenance guard is DISARMED." -ForegroundColor Green
