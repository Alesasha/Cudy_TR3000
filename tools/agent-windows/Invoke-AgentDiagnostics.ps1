param(
    [string]$OutputPath = "$PSScriptRoot\run\ui-diagnostics.txt",
    [string]$VpnInterfaceAlias = "AmneziaVPN"
)

$ErrorActionPreference = "Continue"
$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }

$report = & "$PSScriptRoot\Get-ManagedAgentStatus.ps1" -Network -VpnInterfaceAlias $VpnInterfaceAlias 2>&1 | Out-String -Width 220
Set-Content -LiteralPath $OutputPath -Encoding UTF8 -Value $report
Write-Host "Diagnostic report saved: $OutputPath"
