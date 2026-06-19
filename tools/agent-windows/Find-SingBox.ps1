param(
    [string]$SingBoxExe = ""
)

$ErrorActionPreference = "Stop"

if ($SingBoxExe) {
    if (-not (Test-Path -LiteralPath $SingBoxExe)) {
        throw "sing-box executable not found: $SingBoxExe"
    }
    return (Resolve-Path -LiteralPath $SingBoxExe).Path
}

$candidates = @(
    (Join-Path $PSScriptRoot "runtime\sing-box.exe"),
    (Join-Path $PSScriptRoot "sing-box.exe"),
    "C:\Program Files\sing-box\sing-box.exe",
    "C:\ProgramData\chocolatey\bin\sing-box.exe"
)

foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        return (Resolve-Path -LiteralPath $candidate).Path
    }
}

$cmd = Get-Command sing-box.exe -ErrorAction SilentlyContinue
if ($cmd) {
    return $cmd.Source
}

throw "sing-box.exe not found. Put it into .\runtime\sing-box.exe or pass -SingBoxExe."
