param(
    [string]$TunnelName = "OpenAI-USWest"
)

$ErrorActionPreference = "Continue"
$stateDir = Join-Path $env:ProgramData "CudyVPN\openai-maintenance"
$statePath = Join-Path $stateDir "state.json"

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Stop-OpenAIMaintenanceTunnel.ps1 must be run as Administrator."
    }
}

Assert-Administrator
Unregister-ScheduledTask -TaskName "Cudy OpenAI Route Refresh" -Confirm:$false -ErrorAction SilentlyContinue
$state = $null
if (Test-Path -LiteralPath $statePath) {
    try { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch {}
}
foreach ($prefix in @($state.routes)) {
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix ([string]$prefix) `
        -InterfaceAlias ([string]$state.adapter_name) -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}
if ($state.endpoint_route_owned -and $state.endpoint_prefix -and $state.endpoint_interface_index) {
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix ([string]$state.endpoint_prefix) `
        -InterfaceIndex ([int]$state.endpoint_interface_index) -PolicyStore ActiveStore `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -eq [string]$state.endpoint_next_hop } |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath ([string]$state.standalone_exe)) {
    & ([string]$state.standalone_exe) /uninstalltunnelservice $TunnelName
}
if ($state.runtime_config) {
    Remove-Item -LiteralPath ([string]$state.runtime_config) -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $stateDir "Update-OpenAIMaintenanceRoutes.ps1") -Force -ErrorAction SilentlyContinue
Write-Host "OpenAI maintenance tunnel is DOWN: $TunnelName"
