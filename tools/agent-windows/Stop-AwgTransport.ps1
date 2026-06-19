param(
    [string]$TunnelName = "AmneziaVPN",
    [string]$ServiceName = "",
    [switch]$Delete
)

$ErrorActionPreference = "Continue"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Stop-AwgTransport.ps1 must be run as Administrator."
}

if (-not $ServiceName) {
    $ServiceName = "AmneziaWGTunnel`$$TunnelName"
}

Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
if ($Delete) {
    sc.exe delete $ServiceName | Out-Null
}

Get-Service -Name $ServiceName -ErrorAction SilentlyContinue |
    Format-Table Name, DisplayName, Status, StartType -AutoSize
