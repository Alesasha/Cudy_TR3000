param(
    [string]$ServerId = "aktau",
    [string]$InterfaceAlias = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\agent.env.ps1"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Apply-Routes.ps1 must be run as Administrator."
}

if (-not $InterfaceAlias) {
    $vpnPattern = "(?i)(amn|amnezia|wireguard|wintun|openvpn|tap|tun|wg)"
    $adapter = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq "Up" -and "$($_.Name) $($_.InterfaceDescription)" -match $vpnPattern } |
        Select-Object -First 1
    if ($null -eq $adapter) {
        throw "VPN interface was not detected. Pass -InterfaceAlias explicitly."
    }
    $InterfaceAlias = $adapter.Name
}

python "$PSScriptRoot\route_agent.py" apply `
    --direct-baseline `
    --interface-map "${ServerId}=${InterfaceAlias}" `
    --yes `
    --post-status
