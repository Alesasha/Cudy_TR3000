param(
    [string]$ControlUrl = "http://127.0.0.1:18765",
    [string]$OutputPath = "$PSScriptRoot\run\ui-update-status.json"
)

$ErrorActionPreference = "Stop"
$result = [ordered]@{
    ok = $false
    current_name = "unknown"
    current_code = 0
    latest_name = "unavailable"
    latest_code = 0
    update_available = $false
    error = ""
}

try {
    $versionPath = Join-Path $PSScriptRoot "agent.version.json"
    if (Test-Path -LiteralPath $versionPath) {
        $current = Get-Content -Raw -LiteralPath $versionPath | ConvertFrom-Json
        $result.current_name = [string]$current.version_name
        $result.current_code = [int64]$current.version_code
    }
    . "$PSScriptRoot\agent.env.ps1"
    $headers = @{}
    if ($env:VPN_AGENT_TOKEN) { $headers.Authorization = "Bearer $($env:VPN_AGENT_TOKEN)" }
    $manifest = Invoke-RestMethod -UseBasicParsing -Uri ($ControlUrl.TrimEnd("/") + "/api/agent/app-version?platform=windows") -Headers $headers -TimeoutSec 15
    $result.latest_name = [string]$manifest.version_name
    $result.latest_code = [int64]$manifest.version_code
    $result.update_available = $result.latest_code -gt $result.current_code
    $result.ok = $true
} catch {
    $result.error = $_.Exception.Message
}

$parent = Split-Path -Parent $OutputPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
[System.IO.File]::WriteAllText($OutputPath, ($result | ConvertTo-Json -Depth 5), [System.Text.UTF8Encoding]::new($false))
if (-not $result.ok) { exit 1 }
