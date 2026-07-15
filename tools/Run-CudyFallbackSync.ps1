param(
    [string]$Python = "python",
    [string]$SourcePasswordFile = "$PSScriptRoot\..\secrets\control_backup_ssh_password.txt",
    [string]$CudyPasswordFile = "$PSScriptRoot\..\secrets\cudy_ssh_password.txt",
    [string]$LogPath = "$PSScriptRoot\..\backups\control-server\cudy-fallback-sync.log",
    [int]$KeepRemote = 3,
    [int]$ConnectAttempts = 2,
    [int]$Timeout = 45
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$script = Join-Path $repo "tools\sync_control_state_to_cudy.py"
if (-not (Test-Path -LiteralPath $script)) {
    throw "Sync script not found: $script"
}

$logPathResolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LogPath)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPathResolved) | Out-Null

$setSourcePasswordForProcess = $false
$setCudyPasswordForProcess = $false

if (-not $env:CONTROL_BACKUP_SSH_PASSWORD) {
    if (-not (Test-Path -LiteralPath $SourcePasswordFile)) {
        throw "Set CONTROL_BACKUP_SSH_PASSWORD or create password file: $SourcePasswordFile"
    }
    $env:CONTROL_BACKUP_SSH_PASSWORD = (Get-Content -LiteralPath $SourcePasswordFile -Raw).Trim()
    $setSourcePasswordForProcess = $true
}

if (-not $env:CUDY_SSH_PASSWORD -and (Test-Path -LiteralPath $CudyPasswordFile)) {
    $env:CUDY_SSH_PASSWORD = (Get-Content -LiteralPath $CudyPasswordFile -Raw).Trim()
    $setCudyPasswordForProcess = $true
}

try {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPathResolved -Encoding UTF8 -Value "[$stamp] starting Cudy fallback sync"
    $syncOutput = & $Python $script `
        --keep-remote $KeepRemote `
        --connect-attempts $ConnectAttempts `
        --timeout $Timeout `
        --json 2>&1
    $syncOutput | ForEach-Object {
        $line = [string]$_
        Write-Host $line
        Add-Content -LiteralPath $logPathResolved -Encoding UTF8 -Value $line
    }
    if ($LASTEXITCODE -ne 0) {
        throw "sync_control_state_to_cudy.py failed with exit code $LASTEXITCODE"
    }
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPathResolved -Encoding UTF8 -Value "[$stamp] Cudy fallback sync completed"
} finally {
    if ($setSourcePasswordForProcess) {
        Remove-Item Env:CONTROL_BACKUP_SSH_PASSWORD -ErrorAction SilentlyContinue
    }
    if ($setCudyPasswordForProcess) {
        Remove-Item Env:CUDY_SSH_PASSWORD -ErrorAction SilentlyContinue
    }
}
