param(
    [string]$ServerId = "aktau",
    [string]$InterfaceAlias = "AmneziaVPN",
    [string]$CachePath = "$PSScriptRoot\test-policy-cache.json"
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Apply-Test-Routes.ps1 must be run as Administrator."
}

if (-not (Test-Path -LiteralPath $CachePath)) {
    throw "Test policy cache not found: $CachePath"
}

python "$PSScriptRoot\route_agent.py" `
    --cache "$CachePath" `
    apply `
    --cached `
    --direct-baseline `
    --interface-map "${ServerId}=${InterfaceAlias}" `
    --yes
