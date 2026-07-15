param([string]$TaskName = "Cudy Agent Safety Watchdog")

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-TaskName", "`"$TaskName`""
    ) -PassThru -Wait
    exit $process.ExitCode
}

$ErrorActionPreference = "SilentlyContinue"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath (Join-Path $PSScriptRoot "run\watchdog.armed") -Force -ErrorAction SilentlyContinue
Write-Host "Removed watchdog task: $TaskName"
