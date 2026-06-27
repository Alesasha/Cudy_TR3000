param(
    [string]$GoExe = "C:\Users\Alexander\sdk\go1.26.4\Go\bin\go.exe",
    [ValidateSet("arm64", "mipsle", "mips", "amd64")]
    [string]$GoArch = "arm64",
    [string]$OutputDir = "build\cudy",
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo

if (-not (Test-Path $GoExe)) {
    throw "go.exe was not found at $GoExe. Install Go or pass -GoExe."
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

function Invoke-Go {
    param([string[]]$Arguments)
    & $GoExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "go $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$suffix = "linux-$GoArch"
$output = Join-Path $OutputDir "cudy-fallback-$suffix"
if ($IsWindows -or $env:OS -eq "Windows_NT") {
    # Keep the OpenWrt artifact extensionless.
}

$ldflags = "-s -w"
if ($Version) {
    $ldflags = "$ldflags -X main.version=$Version"
}

Invoke-Go @("test", "./cmd/cudy-fallback")

$env:CGO_ENABLED = "0"
$env:GOOS = "linux"
$env:GOARCH = $GoArch

Invoke-Go @("build", "-trimpath", "-ldflags", $ldflags, "-o", $output, "./cmd/cudy-fallback")

$item = Get-Item $output
[PSCustomObject]@{
    Path = $item.FullName
    Bytes = $item.Length
    GOOS = $env:GOOS
    GOARCH = $env:GOARCH
    SHA256 = (Get-FileHash -Algorithm SHA256 $item.FullName).Hash
}
