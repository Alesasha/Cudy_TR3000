param([int]$DelaySeconds = 8)

Start-Sleep -Seconds ([Math]::Max(1, $DelaySeconds))
$ui = Join-Path $PSScriptRoot "Cudy-Agent.ps1"
if (Test-Path -LiteralPath $ui) {
    Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @(
        "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$ui`""
    ) | Out-Null
}
