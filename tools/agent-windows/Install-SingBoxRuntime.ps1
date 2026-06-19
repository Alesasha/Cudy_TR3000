param(
    [string]$Version = "latest",
    [string]$RuntimeDir = "$PSScriptRoot\runtime",
    [string]$Repo = "SagerNet/sing-box"
)

$ErrorActionPreference = "Stop"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

function Get-Release {
    param([string]$Repo, [string]$Version)
    $uri = if ($Version -eq "latest") {
        "https://api.github.com/repos/$Repo/releases/latest"
    } else {
        "https://api.github.com/repos/$Repo/releases/tags/v$($Version.TrimStart('v'))"
    }
    Invoke-RestMethod -Headers @{ "User-Agent" = "cudy-route-agent" } -Uri $uri -TimeoutSec 60
}

function Select-WindowsAmd64Asset {
    param($Release)
    $assets = @($Release.assets)
    $asset = $assets |
        Where-Object { $_.name -match '^sing-box-.*-windows-amd64\.zip$' } |
        Select-Object -First 1
    if (-not $asset) {
        $names = ($assets | ForEach-Object { $_.name }) -join ", "
        throw "Could not find windows-amd64 sing-box asset in release $($Release.tag_name). Assets: $names"
    }
    return $asset
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
$RuntimeDir = (Resolve-Path -LiteralPath $RuntimeDir).Path

$release = Get-Release -Repo $Repo -Version $Version
$asset = Select-WindowsAmd64Asset $release

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("sing-box-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
$zipPath = Join-Path $tmpDir $asset.name

try {
    Write-Host "Downloading $($asset.name) from $Repo $($release.tag_name)"
    Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $zipPath -TimeoutSec 300
    Expand-Archive -LiteralPath $zipPath -DestinationPath $tmpDir -Force
    $exe = Get-ChildItem -LiteralPath $tmpDir -Recurse -Filter "sing-box.exe" -File | Select-Object -First 1
    if (-not $exe) {
        throw "Downloaded archive does not contain sing-box.exe"
    }
    Copy-Item -LiteralPath $exe.FullName -Destination (Join-Path $RuntimeDir "sing-box.exe") -Force
    $installed = Join-Path $RuntimeDir "sing-box.exe"
    & $installed version
    Write-Host "Installed: $installed"
} finally {
    Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
