param(
    [string]$ServerId = "aktau",
    [string]$InterfaceAlias = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\agent.env.ps1"

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

python "$PSScriptRoot\route_agent.py" plan `
    --direct-baseline `
    --interface-map "${ServerId}=${InterfaceAlias}" `
    --post-status
