param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("smart1", "de1", "ru1", "nl1", "fr1", "se1", "smart2", "de2", "ru2", "nl2", "fr2", "se2")]
    [string]$Profile,
    [string]$Name = "",
    [string]$SubUrl = $env:LOKVPN_SUB_URL,
    [string]$SubCache = "$PSScriptRoot\lokvpn-subscription.json",
    [string]$OutputPath = "",
    [switch]$RestartIfChanged
)

$ErrorActionPreference = "Stop"

if (-not $Name) {
    $Name = "lokvpn-$Profile"
}
if (-not $OutputPath) {
    $OutputPath = Join-Path $PSScriptRoot "transports\$Name.json"
}

function Get-ExistingOutbound {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    try {
        $config = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
        $out = @($config.outbounds | Where-Object { $_.tag -eq "proxy-out" })[0]
        if ($out.server -and $out.server_port -and $out.uuid) {
            return "$($out.server):$($out.server_port):$($out.uuid)"
        }
    } catch {
    }
    return ""
}

$old = Get-ExistingOutbound -Path $OutputPath

$args = @(
    "-Profile", $Profile,
    "-Name", $Name,
    "-SubCache", $SubCache,
    "-OutputPath", $OutputPath
)
if ($SubUrl) {
    $args += @("-SubUrl", $SubUrl)
}

& "$PSScriptRoot\New-LokVpnConfig.ps1" @args | Out-Null
& "$PSScriptRoot\Find-SingBox.ps1" | ForEach-Object {
    & $_ check -c $OutputPath
}

$new = Get-ExistingOutbound -Path $OutputPath
if ($RestartIfChanged -and $old -and $old -ne $new) {
    & "$PSScriptRoot\Stop-SingBoxTransport.ps1" -Name $Name
    & "$PSScriptRoot\Start-SingBoxTransport.ps1" -Name $Name -ConfigPath $OutputPath
}

[pscustomobject]@{
    profile = $Profile
    name = $Name
    output_path = $OutputPath
    outbound = $new
    changed = ($old -ne $new)
} | ConvertTo-Json -Depth 5
