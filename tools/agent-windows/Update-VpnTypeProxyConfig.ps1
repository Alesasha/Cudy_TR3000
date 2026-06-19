param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("proxygb", "proxyca", "proxyfr", "proxyby", "proxyae", "proxyhk", "proxykz", "proxytr", "proxyil", "proxycz", "proxypl", "proxyfi", "proxynl", "proxyal", "proxyru", "proxyus", "proxyde")]
    [string]$Provider,
    [string]$Name = "",
    [string]$Auth = $env:VPNTYPE_AUTH_DEFAULT,
    [string]$Uuid = $env:VPNTYPE_UUID_DEFAULT,
    [string]$ProxyListJson = "",
    [string]$OutputPath = "",
    [string]$ProxyCheckUrl = "https://ifconfig.me/ip",
    [switch]$SkipVerify,
    [switch]$RestartIfChanged
)

$ErrorActionPreference = "Stop"

if (-not $Name) {
    $Name = $Provider
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot "transports\$Name.json"
}
if (-not $Auth -or -not $Uuid) {
    throw "Set VPNTYPE_AUTH_DEFAULT and VPNTYPE_UUID_DEFAULT in the environment or agent.env.ps1."
}

$providerMeta = @{
    proxygb = @{ country = "GB"; candidates = @(142, 85) }
    proxyca = @{ country = "CA"; candidates = @(143, 82) }
    proxyfr = @{ country = "FR"; candidates = @(145, 81) }
    proxyby = @{ country = "BY"; candidates = @(146, 80) }
    proxyae = @{ country = "AE"; candidates = @(147, 79) }
    proxyhk = @{ country = "HK"; candidates = @(148, 78) }
    proxykz = @{ country = "KZ"; candidates = @(149, 77) }
    proxytr = @{ country = "TR"; candidates = @(150, 76) }
    proxyil = @{ country = "IL"; candidates = @(151, 75) }
    proxycz = @{ country = "CZ"; candidates = @(152, 74) }
    proxypl = @{ country = "PL"; candidates = @(153, 61) }
    proxyfi = @{ country = "FI"; candidates = @(154, 60) }
    proxynl = @{ country = "NL"; candidates = @(155, 59) }
    proxyal = @{ country = "AL"; candidates = @(156, 58) }
    proxyru = @{ country = "RU"; candidates = @(157, 57) }
    proxyus = @{ country = "US"; candidates = @(158, 56) }
    proxyde = @{ country = "DE"; candidates = @(159, 55) }
}

function Invoke-VpnTypePost {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,
        [hashtable]$Fields
    )
    $args = @(
        "-fsS",
        "--connect-timeout", "10",
        "--max-time", "25",
        "-X", "POST",
        $Uri,
        "-H", "Authorization: $Auth"
    )
    foreach ($key in $Fields.Keys) {
        $args += @("-F", "$key=$($Fields[$key])")
    }
    $output = & curl.exe @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "VPNtype API call failed: $Uri"
    }
    return ($output | Out-String).Trim()
}

function Get-CandidateIds {
    param([string]$ListJson, [string]$Country, [int[]]$BaseCandidates)
    $ids = New-Object System.Collections.Generic.List[int]
    foreach ($id in $BaseCandidates) {
        if (-not $ids.Contains($id)) {
            $ids.Add($id) | Out-Null
        }
    }
    if ($ListJson) {
        try {
            $list = $ListJson | ConvertFrom-Json
            foreach ($item in @($list)) {
                if ([string]$item.country_id -eq $Country -and $item.id) {
                    $id = [int]$item.id
                    if (-not $ids.Contains($id)) {
                        $ids.Add($id) | Out-Null
                    }
                }
            }
        } catch {
            Write-Warning "Could not parse VPNtype proxy-list JSON; using built-in candidate ids."
        }
    }
    return $ids.ToArray()
}

function Test-ProxyEndpoint {
    param([string]$Server, [int]$Port)
    if ($SkipVerify) {
        return $true
    }
    & curl.exe -4 -fsS --connect-timeout 5 --max-time 12 -x "http://${Server}:$Port" $ProxyCheckUrl *> $null
    return ($LASTEXITCODE -eq 0)
}

function Get-ExistingProxy {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    try {
        $config = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
        $out = @($config.outbounds | Where-Object { $_.tag -eq "proxy-out" })[0]
        if ($out.server -and $out.server_port) {
            return "$($out.server):$($out.server_port)"
        }
    } catch {
    }
    return ""
}

$meta = $providerMeta[$Provider]
if (-not $ProxyListJson) {
    $ProxyListJson = Invoke-VpnTypePost -Uri "https://vpntypedev.com/api/chrome/proxy-list" -Fields @{
        version = "1.1.1"
        uuid = $Uuid
    }
}

$candidateIds = Get-CandidateIds -ListJson $ProxyListJson -Country $meta.country -BaseCandidates $meta.candidates
$selected = $null
foreach ($candidateId in $candidateIds) {
    $json = $null
    try {
        $json = Invoke-VpnTypePost -Uri "https://vpntypedev.com/api/chrome/proxy-credentials" -Fields @{
            version = "1.1.1"
            uuid = $Uuid
            proxy_id = $candidateId
        }
        $reply = $json | ConvertFrom-Json
        $credentials = [string]$reply.credentials
        if (-not $credentials -or $credentials -notmatch "^([^:]+):([0-9]+)$") {
            continue
        }
        $server = $Matches[1]
        $port = [int]$Matches[2]
        if (Test-ProxyEndpoint -Server $server -Port $port) {
            $selected = [pscustomobject]@{
                Server = $server
                Port = $port
                ProxyId = $candidateId
            }
            break
        }
        Write-Warning "$Provider candidate failed: id=$candidateId endpoint=${server}:$port"
    } catch {
        Write-Warning "$Provider candidate failed: id=$candidateId"
    }
}

if (-not $selected) {
    throw "No working VPNtype endpoint for $Provider. Candidates: $($candidateIds -join ',')"
}

$old = Get-ExistingProxy -Path $OutputPath
$new = "$($selected.Server):$($selected.Port)"
& "$PSScriptRoot\New-SingBoxHttpProxyConfig.ps1" -Name $Name -ProxyHost $selected.Server -ProxyPort $selected.Port -OutputPath $OutputPath | Out-Null
& "$PSScriptRoot\Find-SingBox.ps1" | ForEach-Object {
    & $_ check -c $OutputPath
}

if ($RestartIfChanged -and $old -and $old -ne $new) {
    & "$PSScriptRoot\Stop-SingBoxTransport.ps1" -Name $Name
    & "$PSScriptRoot\Start-SingBoxTransport.ps1" -Name $Name -ConfigPath $OutputPath
}

[pscustomobject]@{
    provider = $Provider
    name = $Name
    output_path = $OutputPath
    endpoint = $new
    proxy_id = $selected.ProxyId
    changed = ($old -ne $new)
} | ConvertTo-Json -Depth 5
