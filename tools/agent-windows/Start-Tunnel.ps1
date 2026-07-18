param(
    [string]$HostName = "95.182.91.203",
    [string]$User = "cudy-tunnel-windows",
    [string]$KeyPath = "$PSScriptRoot\uswest_control_tunnel_ed25519",
    [string]$KnownHostsPath = "$PSScriptRoot\known_hosts",
    [string]$ExpectedHostKeySha256 = "",
    [int]$LocalPort = 18765,
    [int]$RemotePort = 8765
)

$ErrorActionPreference = "Stop"

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-PhysicalDefaultRoute {
    $vpnPattern = "(?i)(amn|amnezia|wireguard|wintun|openvpn|tap|tun|wg)"
    $routes = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction Stop |
        Sort-Object RouteMetric, InterfaceMetric
    foreach ($route in $routes) {
        $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
        if ($null -eq $adapter -or $adapter.Status -ne "Up") {
            continue
        }
        if ("$($adapter.Name) $($adapter.InterfaceDescription)" -notmatch $vpnPattern) {
            return $route
        }
    }
    return $routes | Select-Object -First 1
}

function Get-KeyFingerprint {
    param([Parameter(Mandatory = $true)][string]$Path)
    $output = & ssh-keygen.exe -lf $Path -E sha256 2>$null
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    $match = [regex]::Match(($output -join "`n"), 'SHA256:[A-Za-z0-9+/=]+')
    if ($match.Success) { return $match.Value }
    return ""
}

function Confirm-ControlHostKey {
    param(
        [Parameter(Mandatory = $true)][string]$HostValue,
        [Parameter(Mandatory = $true)][string]$Fingerprint,
        [Parameter(Mandatory = $true)][string]$KnownHostsFile
    )
    if ($Fingerprint -notmatch '^SHA256:[A-Za-z0-9+/]{20,}={0,2}$') {
        throw "Invalid advertised control-server SSH fingerprint."
    }
    $temp = Join-Path $env:TEMP ("cudy-control-host-key-{0}" -f [guid]::NewGuid().ToString("N"))
    try {
        $known = @()
        if (Test-Path -LiteralPath $KnownHostsFile) {
            $known = @(& ssh-keygen.exe -F $HostValue -f $KnownHostsFile 2>$null |
                Where-Object { $_ -and -not $_.StartsWith('#') })
        }
        if ($known.Count -gt 0) {
            [IO.File]::WriteAllLines($temp, $known, [Text.UTF8Encoding]::new($false))
            if ((Get-KeyFingerprint -Path $temp) -eq $Fingerprint) {
                return
            }
        }

        $scanned = @(& ssh-keyscan.exe -T 12 -p 22 -t ed25519 $HostValue 2>$null |
            Where-Object { $_ -and -not $_.StartsWith('#') })
        if ($LASTEXITCODE -ne 0 -or $scanned.Count -eq 0) {
            throw "Cannot read the advertised control-server SSH host key."
        }
        [IO.File]::WriteAllLines($temp, $scanned, [Text.UTF8Encoding]::new($false))
        $actual = Get-KeyFingerprint -Path $temp
        if ($actual -ne $Fingerprint) {
            throw "Control-server SSH key mismatch: expected $Fingerprint, got $actual."
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $KnownHostsFile) | Out-Null
        if (-not (Test-Path -LiteralPath $KnownHostsFile)) {
            [IO.File]::WriteAllText($KnownHostsFile, "", [Text.UTF8Encoding]::new($false))
        }
        & ssh-keygen.exe -R $HostValue -f $KnownHostsFile *> $null
        Add-Content -LiteralPath $KnownHostsFile -Value $scanned -Encoding UTF8
    } finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

$existing = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "127.0.0.1:$LocalPort is already listening."
    return
}

if (Test-Path $KeyPath) {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    icacls $KeyPath /inheritance:r | Out-Null
    icacls $KeyPath /grant:r "${currentUser}:R" | Out-Null
}

$hostIp = ([System.Net.Dns]::GetHostAddresses($HostName) | Where-Object { $_.AddressFamily -eq "InterNetwork" } | Select-Object -First 1).IPAddressToString
if (-not $hostIp) {
    throw "Cannot resolve $HostName to IPv4."
}

if (Test-Admin) {
    $route = Get-PhysicalDefaultRoute
    if ($null -ne $route) {
        $dest = "$hostIp/32"
        Remove-NetRoute -DestinationPrefix $dest -PolicyStore ActiveStore -Confirm:$false -ErrorAction SilentlyContinue
        New-NetRoute -DestinationPrefix $dest -InterfaceIndex $route.InterfaceIndex -NextHop $route.NextHop -RouteMetric 1 -PolicyStore ActiveStore | Out-Null
        Write-Host "Pinned $dest via ifIndex=$($route.InterfaceIndex) nextHop=$($route.NextHop)."
    }
} else {
    Write-Warning "Run this script as Administrator to pin $hostIp outside the VPN before opening the SSH tunnel."
}

Write-Host "Opening SSH tunnel: http://127.0.0.1:$LocalPort -> ${User}@${HostName}:127.0.0.1:$RemotePort"
Write-Host "Keep this window open while the agent is running."

if ($ExpectedHostKeySha256) {
    Confirm-ControlHostKey -HostValue $HostName -Fingerprint $ExpectedHostKeySha256 -KnownHostsFile $KnownHostsPath
}
$strictHostMode = if (Test-Path -LiteralPath $KnownHostsPath) { "yes" } else { "accept-new" }
$sshArgs = @(
    "-i", $KeyPath,
    "-p", "22",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
    "-o", "StrictHostKeyChecking=$strictHostMode",
    "-o", "UserKnownHostsFile=$KnownHostsPath",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ConnectTimeout=12",
    "-o", "ConnectionAttempts=1",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-N", "-L", "${LocalPort}:127.0.0.1:${RemotePort}",
    "${User}@${HostName}"
)
& ssh @sshArgs
