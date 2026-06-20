param(
    [string]$HostName = "95.182.91.203",
    [string]$User = "root",
    [string]$Python = "python",
    [string]$PasswordFile = "$PSScriptRoot\..\secrets\control_backup_ssh_password.txt",
    [string]$OutputDir = "$PSScriptRoot\..\backups\control-server",
    [string]$LogPath = "$PSScriptRoot\..\backups\control-server\backup-task.log",
    [int]$KeepLocal = 10
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$script = Join-Path $repo "tools\backup_control_server.py"
if (-not (Test-Path -LiteralPath $script)) {
    throw "Backup script not found: $script"
}

$outputDirResolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
New-Item -ItemType Directory -Force -Path $outputDirResolved | Out-Null

$logPathResolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($LogPath)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPathResolved) | Out-Null

$setPasswordForProcess = $false
if (-not $env:CONTROL_BACKUP_SSH_PASSWORD) {
    if (-not (Test-Path -LiteralPath $PasswordFile)) {
        throw "Set CONTROL_BACKUP_SSH_PASSWORD or create password file: $PasswordFile"
    }
    $env:CONTROL_BACKUP_SSH_PASSWORD = (Get-Content -LiteralPath $PasswordFile -Raw).Trim()
    $setPasswordForProcess = $true
}

try {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPathResolved -Encoding UTF8 -Value "[$stamp] starting backup host=$HostName"
    $backupOutput = & $Python $script `
        --host $HostName `
        --user $User `
        --output-dir $outputDirResolved `
        --keep-local $KeepLocal 2>&1
    $backupOutput | ForEach-Object {
        $line = [string]$_
        Write-Host $line
        Add-Content -LiteralPath $logPathResolved -Encoding UTF8 -Value $line
    }
    if ($LASTEXITCODE -ne 0) {
        throw "backup_control_server.py failed with exit code $LASTEXITCODE"
    }
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $logPathResolved -Encoding UTF8 -Value "[$stamp] backup completed"
} finally {
    if ($setPasswordForProcess) {
        Remove-Item Env:CONTROL_BACKUP_SSH_PASSWORD -ErrorAction SilentlyContinue
    }
}
