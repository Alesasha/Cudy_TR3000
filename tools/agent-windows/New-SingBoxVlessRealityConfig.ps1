param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [Parameter(Mandatory = $true)]
    [int]$ServerPort,
    [Parameter(Mandatory = $true)]
    [string]$Uuid,
    [string]$Flow = "",
    [Parameter(Mandatory = $true)]
    [string]$ServerName,
    [Parameter(Mandatory = $true)]
    [string]$PublicKey,
    [Parameter(Mandatory = $true)]
    [string]$ShortId,
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

if (-not $InterfaceName) {
    $InterfaceName = $Name
}
if (-not $TunAddress) {
    $TunAddress = "172.43.$((Get-Random -Minimum 2 -Maximum 240)).1/30"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot "transports\$Name.json"
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OutputPath) | Out-Null

$proxyOut = [ordered]@{
    type = "vless"
    tag = "proxy-out"
    server = $Server
    server_port = $ServerPort
    uuid = $Uuid
    tls = [ordered]@{
        enabled = $true
        server_name = $ServerName
        utls = [ordered]@{
            enabled = $true
            fingerprint = "chrome"
        }
        reality = [ordered]@{
            enabled = $true
            public_key = $PublicKey
            short_id = $ShortId
        }
    }
}
if ($Flow) {
    $proxyOut["flow"] = $Flow
}

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
        $proxyOut,
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
                ip_cidr = @("$Server/32")
                outbound = "direct"
            }
        )
        final = "proxy-out"
    }
}

$json = $config | ConvertTo-Json -Depth 30
Write-Utf8NoBom -Path $OutputPath -Value $json
Write-Host $OutputPath
