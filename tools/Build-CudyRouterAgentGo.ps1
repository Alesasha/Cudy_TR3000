param(
    [string]$GoExe = "C:\Users\Alexander\sdk\go1.26.4\Go\bin\go.exe",
    [ValidateSet("arm64", "mipsle", "mips", "amd64")]
    [string]$GoArch = "arm64",
    [string]$OutputDir = "build\cudy"
)

$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repo
if (-not (Test-Path $GoExe)) { throw "go.exe was not found at $GoExe" }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

& $GoExe test ./cmd/cudy-router-agent
if ($LASTEXITCODE -ne 0) { throw "cudy-router-agent tests failed" }

$output = Join-Path $OutputDir "cudy-router-agent-linux-$GoArch"
$oldCgo, $oldGoos, $oldGoarch = $env:CGO_ENABLED, $env:GOOS, $env:GOARCH
try {
    $env:CGO_ENABLED = "0"
    $env:GOOS = "linux"
    $env:GOARCH = $GoArch
    & $GoExe build -trimpath -ldflags "-s -w" -o $output ./cmd/cudy-router-agent
    if ($LASTEXITCODE -ne 0) { throw "cudy-router-agent build failed" }
} finally {
    $env:CGO_ENABLED, $env:GOOS, $env:GOARCH = $oldCgo, $oldGoos, $oldGoarch
}

$item = Get-Item $output
[PSCustomObject]@{
    Path = $item.FullName
    Bytes = $item.Length
    GOARCH = $GoArch
    SHA256 = (Get-FileHash -Algorithm SHA256 $item.FullName).Hash
}
