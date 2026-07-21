param([switch]$Elevated)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-Elevated"
    )
    exit $LASTEXITCODE
}

$installRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$expectedRoot = [IO.Path]::GetFullPath((Join-Path $env:ProgramFiles "Cudy Agent")).TrimEnd('\')
if (-not [string]::Equals($installRoot, $expectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove unexpected install directory: $installRoot"
}

& (Join-Path $PSScriptRoot "Uninstall-AgentWatchdogTask.ps1") -ErrorAction SilentlyContinue
& (Join-Path $PSScriptRoot "Uninstall-ManagedAgentTask.ps1") -FullRollback
& (Join-Path $PSScriptRoot "Install-AgentUi.ps1") -Remove
& (Join-Path $PSScriptRoot "Register-CudyAgentInstallation.ps1") -Remove

$cleanupScript = Join-Path $env:TEMP ("cudy-agent-remove-{0}.ps1" -f [guid]::NewGuid().ToString("N"))
$quotedRoot = $installRoot.Replace("'", "''")
$quotedSelf = $cleanupScript.Replace("'", "''")
$cleanup = @"
Start-Sleep -Seconds 3
Remove-Item -LiteralPath '$quotedRoot' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath '$quotedSelf' -Force -ErrorAction SilentlyContinue
"@
[IO.File]::WriteAllText($cleanupScript, $cleanup, [Text.UTF8Encoding]::new($false))
Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$cleanupScript`""
) | Out-Null
Write-Host "Cudy Agent was removed."
