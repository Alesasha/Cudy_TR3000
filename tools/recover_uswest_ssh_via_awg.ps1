param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$AwgConfig = "",
    [string]$TunnelName = "UswestAdmin",
    [Parameter(Mandatory = $true)]
    [string]$PrivateSshHost,
    [string]$PasswordFile = "",
    [int]$ConnectAttempts = 6,
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from elevated PowerShell. It must create a temporary AWG tunnel."
    }
}

Assert-Admin

if ($PrivateSshHost -eq "10.8.1.1") {
    throw "10.8.1.1 is an AWG client peer, not a verified uswest management address. Pass the private SSH address configured on uswest."
}

if (-not $AwgConfig) {
    $AwgConfig = Join-Path $RepoRoot "secrets\agents\isasha_R7_Cudy-windows\uswest-awg.conf"
}
if (-not $PasswordFile) {
    $PasswordFile = Join-Path $RepoRoot "secrets\control_backup_ssh_password.txt"
}

$startAwg = Join-Path $RepoRoot "secrets\agents\isasha_R7_Cudy-windows\Start-AwgTransport.ps1"
$harden = Join-Path $RepoRoot "tools\harden_control_ssh.py"

foreach ($path in @($AwgConfig, $PasswordFile, $startAwg, $harden)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required file not found: $path"
    }
}

Write-Host "Starting temporary AWG tunnel '$TunnelName' to uswest..."
& $startAwg -ConfigPath $AwgConfig -TunnelName $TunnelName -AllowedIPs "$PrivateSshHost/32"

Write-Host "Testing private SSH endpoint ${PrivateSshHost}:22..."
$deadline = [DateTime]::UtcNow.AddSeconds(45)
do {
    $tcp = Test-NetConnection $PrivateSshHost -Port 22 -WarningAction SilentlyContinue
    if ($tcp.TcpTestSucceeded) {
        break
    }
    Start-Sleep -Seconds 2
} while ([DateTime]::UtcNow -lt $deadline)

if (-not $tcp.TcpTestSucceeded) {
    throw "Private SSH endpoint is not reachable through ${TunnelName}: ${PrivateSshHost}:22"
}

$password = (Get-Content -Raw -LiteralPath $PasswordFile).Trim()
if (-not $password) {
    throw "Empty SSH password file: $PasswordFile"
}

try {
    $env:USWEST_SSH_PASSWORD = $password
    Write-Host "Installing SSH hardening/watchdog through private AWG endpoint..."
    python $harden `
        --host $PrivateSshHost `
        --user root `
        --connect-attempts $ConnectAttempts `
        --timeout $TimeoutSeconds `
        --ignore-ip "10.8.1.0/24" `
        --ignore-ip "10.77.0.0/24"
} finally {
    Remove-Item Env:\USWEST_SSH_PASSWORD -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Checking public SSH after hardening..."
python (Join-Path $RepoRoot "tools\check_control_server_prod.py") --require-ssh --connect-attempts 3 --timeout 20
