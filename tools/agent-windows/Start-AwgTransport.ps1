param(
    [string]$ConfigPath = "",
    [string]$ServiceExe = "C:\Program Files\AmneziaVPN\AmneziaVPN-service.exe",
    [string]$TunnelName = "AmneziaVPN",
    [string]$ServiceName = "",
    [string]$DisplayName = "",
    [string]$AllowedIPs = "0.0.0.0/0",
    [switch]$NoPeer
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Start-AwgTransport.ps1 must be run as Administrator."
    }
}

function Convert-Base64ToHex {
    param([string]$Value)
    $bytes = [Convert]::FromBase64String($Value.Trim())
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Parse-AwgConfig {
    param([string]$Text)
    $section = ""
    $interface = [ordered]@{}
    $peer = [ordered]@{}
    foreach ($raw in ($Text -split "`r?`n")) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        if ($line -eq "[Interface]") {
            $section = "interface"
            continue
        }
        if ($line -eq "[Peer]") {
            $section = "peer"
            continue
        }
        $parts = $line -split "\s*=\s*", 2
        if ($parts.Count -ne 2) {
            continue
        }
        if ($section -eq "interface") {
            $interface[$parts[0]] = $parts[1]
        } elseif ($section -eq "peer") {
            $peer[$parts[0]] = $parts[1]
        }
    }
    return [pscustomobject]@{ Interface = $interface; Peer = $peer }
}

function Endpoint-ToUapi {
    param([string]$Endpoint)
    $hostPart, $portPart = $Endpoint.Trim() -split ":", 2
    if (-not $hostPart -or -not $portPart) {
        throw "Endpoint must look like host:port, got: $Endpoint"
    }
    $addresses = [System.Net.Dns]::GetHostAddresses($hostPart) |
        Where-Object { $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork }
    if (-not $addresses) {
        throw "Could not resolve endpoint host to IPv4: $hostPart"
    }
    return "$($addresses[0].IPAddressToString):$portPart"
}

Assert-Admin

if (-not $ServiceName) {
    $ServiceName = "AmneziaWGTunnel`$$TunnelName"
}
if (-not $DisplayName) {
    $DisplayName = "NashVPN AWG transport $TunnelName"
}
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $PSScriptRoot "client-awg.conf"
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config not found: $ConfigPath"
}
if (-not (Test-Path -LiteralPath $ServiceExe)) {
    throw "Service executable not found: $ServiceExe"
}

$parsed = Parse-AwgConfig (Get-Content -Raw -LiteralPath $ConfigPath)
$iface = $parsed.Interface
$peer = $parsed.Peer

foreach ($required in @("PrivateKey", "Address")) {
    if (-not $iface.Contains($required)) {
        throw "Missing [Interface] $required in $ConfigPath"
    }
}
if (-not $NoPeer) {
    foreach ($required in @("PublicKey", "Endpoint")) {
        if (-not $peer.Contains($required)) {
            throw "Missing [Peer] $required in $ConfigPath"
        }
    }
}

$interfaceLines = New-Object System.Collections.Generic.List[string]
$interfaceLines.Add("[Interface]")
# AmneziaVPN stores Jmin/Jmax in exported profiles but does not pass them to
# tunneldaemon. Passing them through creates an interface that never handshakes.
foreach ($key in @("PrivateKey", "Address", "MTU", "Jc", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5")) {
    if ($iface.Contains($key) -and $iface[$key] -ne "") {
        $interfaceLines.Add("$key = $($iface[$key])")
    }
}
$interfaceLines.Add("Table = off")
$interfaceConfig = ($interfaceLines -join "`r`n") + "`r`n"

Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
sc.exe delete $ServiceName | Out-Null
Start-Sleep -Milliseconds 500

$binaryPath = "`"$ServiceExe`" tunneldaemon `"$interfaceConfig`""
New-Service -Name $ServiceName `
    -BinaryPathName $binaryPath `
    -DisplayName $DisplayName `
    -StartupType Manual `
    -DependsOn @("Nsi", "TcpIp") | Out-Null
sc.exe sidtype $ServiceName unrestricted | Out-Null
Start-Service -Name $ServiceName

$deadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $svc = Get-Service -Name $ServiceName -ErrorAction Stop
    if ($svc.Status -eq "Running") {
        break
    }
} while ([DateTime]::UtcNow -lt $deadline)

if ((Get-Service -Name $ServiceName).Status -ne "Running") {
    throw "Transport service did not reach Running state."
}

Write-Host "Transport service is running: $ServiceName"

if ($NoPeer) {
    Write-Host "Peer update skipped because -NoPeer was passed."
    exit 0
}

$endpoint = Endpoint-ToUapi $peer["Endpoint"]
$uapi = New-Object System.Collections.Generic.List[string]
$uapi.Add("set=1")
$uapi.Add("public_key=$(Convert-Base64ToHex $peer["PublicKey"])")
if ($peer.Contains("PresharedKey") -and $peer["PresharedKey"]) {
    $uapi.Add("preshared_key=$(Convert-Base64ToHex $peer["PresharedKey"])")
}
$uapi.Add("endpoint=$endpoint")
$uapi.Add("replace_allowed_ips=true")
$keepalive = "25"
if ($peer.Contains("PersistentKeepalive") -and $peer["PersistentKeepalive"]) {
    $keepalive = $peer["PersistentKeepalive"]
}
$uapi.Add("persistent_keepalive_interval=$keepalive")
foreach ($item in ($AllowedIPs -split ",")) {
    $allowed = $item.Trim()
    if ($allowed) {
        $uapi.Add("allowed_ip=$allowed")
    }
}

$reply = & "$PSScriptRoot\Send-AwgUapi.ps1" -TunnelName $TunnelName -Command ($uapi -join "`n")
Write-Host "UAPI reply:"
Write-Host $reply

Write-Host ""
Get-NetAdapter -Name $TunnelName -ErrorAction SilentlyContinue |
    Format-Table Name, InterfaceDescription, Status, InterfaceIndex -AutoSize
