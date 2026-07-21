param(
    [ValidateSet("On", "Off")]
    [string]$State,
    [string]$TaskName = "Cudy Managed Route Agent",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Argument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Write-Result {
    param([string]$Message)
    if ($OutputPath) {
        $parent = Split-Path -Parent $OutputPath
        if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
        Set-Content -LiteralPath $OutputPath -Encoding UTF8 -Value $Message
    }
    Write-Host $Message
}

if (-not (Test-IsAdministrator)) {
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Quote-Argument $PSCommandPath),
        "-State", $State,
        "-TaskName", (Quote-Argument $TaskName)
    )
    if ($OutputPath) { $arguments += @("-OutputPath", (Quote-Argument $OutputPath)) }
    try {
        $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList ($arguments -join " ") -Wait -PassThru
        exit $process.ExitCode
    } catch {
        Write-Result "Administrator approval was cancelled."
        exit 1
    }
}

try {
    if ($State -eq "On") {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) { throw "Agent task is not installed: $TaskName" }
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Result "Agent start requested. The control link may need up to one minute to become ready."
    } else {
        $lines = & "$PSScriptRoot\Emergency-Stop-Agent.ps1" -TaskName $TaskName 2>&1 | Out-String
        Write-Result ($lines.Trim())
    }
    exit 0
} catch {
    Write-Result ("Agent {0} failed: {1}" -f $State.ToLowerInvariant(), $_.Exception.Message)
    exit 1
}
