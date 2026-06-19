param(
    [Parameter(Mandatory = $true)]
    [string]$Name,
    [Parameter(Mandatory = $true)]
    [string]$ProxyHost,
    [Parameter(Mandatory = $true)]
    [int]$ProxyPort,
    [ValidateSet("http", "socks")]
    [string]$ProxyType = "http",
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
    $TunAddress = "172.41.$((Get-Random -Minimum 2 -Maximum 240)).1/30"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot "transports\$Name.json"
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
            type = $ProxyType
            tag = "proxy-out"
            server = $ProxyHost
            server_port = $ProxyPort
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
                ip_cidr = @("$ProxyHost/32")
                outbound = "direct"
            }
        )
        final = "proxy-out"
    }
}

$json = $config | ConvertTo-Json -Depth 20
Write-Utf8NoBom -Path $OutputPath -Value $json
Write-Host $OutputPath
