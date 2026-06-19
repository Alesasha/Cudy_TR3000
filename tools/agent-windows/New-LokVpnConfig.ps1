param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("smart1", "de1", "ru1", "nl1", "fr1", "se1", "smart2", "de2", "ru2", "nl2", "fr2", "se2")]
    [string]$Profile,
    [string]$Name = "",
    [string]$SubUrl = $env:LOKVPN_SUB_URL,
    [string]$SubCache = "$PSScriptRoot\lokvpn-subscription.json",
    [string]$InterfaceName = "",
    [string]$TunAddress = "",
    [int]$Mtu = 1400,
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

$profileMap = @{
    smart1 = @(0, 1)
    de1 = @(1, 0)
    ru1 = @(2, 0)
    nl1 = @(3, 0)
    fr1 = @(4, 0)
    se1 = @(5, 0)
    smart2 = @(6, 1)
    de2 = @(7, 0)
    ru2 = @(8, 0)
    nl2 = @(9, 0)
    fr2 = @(10, 0)
    se2 = @(11, 0)
}

if (-not $Name) {
    $Name = "lokvpn-$Profile"
}
if (-not $InterfaceName) {
    $InterfaceName = $Name
}
if (-not $TunAddress) {
    $TunAddress = "172.42.$((Get-Random -Minimum 2 -Maximum 240)).1/30"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot "transports\$Name.json"
}

if ($SubUrl) {
    $headers = @{
        "X-App-Version" = "2.7.0"
        "X-Device-Locale" = "RU"
        "X-Device-OS" = "Windows"
        "X-Device-model" = "Ryzen7Pro4750G_x86_64"
        "X-HWID" = "3dadf61c-af37-4ea7-a8d3-ce044ce069d7"
        "X-Ver-OS" = "11_10.0.26200"
    }
    $raw = Invoke-WebRequest -UseBasicParsing -Uri $SubUrl -Headers $headers -TimeoutSec 60
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SubCache) | Out-Null
    Write-Utf8NoBom -Path $SubCache -Value $raw.Content
} elseif (Test-Path -LiteralPath $SubCache) {
    $raw = Get-Content -Raw -LiteralPath $SubCache
} else {
    throw "Set LOKVPN_SUB_URL or provide -SubUrl, or put subscription JSON into $SubCache."
}

$items = $raw.Content
if ($null -eq $items) {
    $items = $raw
}
$subscription = $items | ConvertFrom-Json
$idx = $profileMap[$Profile][0]
$ob = $profileMap[$Profile][1]
$outbound = $subscription[$idx].outbounds[$ob]
$vnext = $outbound.settings.vnext[0]
$user = $vnext.users[0]
$reality = $outbound.streamSettings.realitySettings

foreach ($pair in @(
    @("server", $vnext.address),
    @("port", $vnext.port),
    @("uuid", $user.id),
    @("flow", $user.flow),
    @("sni", $reality.serverName),
    @("public_key", $reality.publicKey),
    @("short_id", $reality.shortId)
)) {
    if (-not $pair[1]) {
        throw "Could not parse LokVPN $Profile field: $($pair[0])"
    }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null

$config = [ordered]@{
    log = [ordered]@{
        level = "info"
        timestamp = $true
    }
    inbounds = @(
        [ordered]@{
            type = "tun"
            tag = "$Name-tun"
            interface_name = $InterfaceName
            address = @($TunAddress)
            mtu = $Mtu
            auto_route = $false
            strict_route = $false
            stack = "gvisor"
        }
    )
    outbounds = @(
        [ordered]@{
            type = "vless"
            tag = "proxy-out"
            server = $vnext.address
            server_port = [int]$vnext.port
            uuid = $user.id
            flow = $user.flow
            tls = [ordered]@{
                enabled = $true
                server_name = $reality.serverName
                utls = [ordered]@{
                    enabled = $true
                    fingerprint = "chrome"
                }
                reality = [ordered]@{
                    enabled = $true
                    public_key = $reality.publicKey
                    short_id = $reality.shortId
                }
            }
        },
        [ordered]@{
            type = "direct"
            tag = "direct"
        },
        [ordered]@{
            type = "block"
            tag = "block"
        }
    )
    route = [ordered]@{
        auto_detect_interface = $true
        rules = @(
            [ordered]@{
                ip_cidr = @("$($vnext.address)/32")
                outbound = "direct"
            }
        )
        final = "proxy-out"
    }
}

$json = $config | ConvertTo-Json -Depth 30
Write-Utf8NoBom -Path $OutputPath -Value $json
Write-Host $OutputPath
