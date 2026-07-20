param(
    [string]$TaskName = "Cudy Control UI Tunnel",
    [string]$HostName = "95.182.91.203",
    [string]$User = "cudy-tunnel-windows",
    [string]$KeyPath = "$PSScriptRoot\..\..\secrets\agents\isasha_R7_Cudy-windows\uswest_control_tunnel_ed25519",
    [int]$LocalPort = 18765,
    [int]$RemotePort = 8765,
    [string]$LogPath = "$env:LOCALAPPDATA\CudyAgent\control-ui-tunnel.log",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

function Quote-Arg {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

$script = Join-Path $PSScriptRoot "Start-ControlUiTunnel.ps1"
$resolvedScript = (Resolve-Path -LiteralPath $script).Path
$resolvedKey = (Resolve-Path -LiteralPath $KeyPath).Path
$arguments = @(
    "-WindowStyle", "Hidden",
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-Arg $resolvedScript),
    "-HostName", (Quote-Arg $HostName),
    "-User", (Quote-Arg $User),
    "-KeyPath", (Quote-Arg $resolvedKey),
    "-LocalPort", "$LocalPort",
    "-RemotePort", "$RemotePort",
    "-LogPath", (Quote-Arg $LogPath)
) -join " "

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$settings.Hidden = $true

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null

    if ($RunNow) {
        Start-ScheduledTask -TaskName $TaskName
    }
    Write-Host "Installed scheduled task: $TaskName"
} catch {
    $isAccessDenied = $_.Exception.HResult -eq -2147024891 `
        -or $_.FullyQualifiedErrorId -match "0x80070005|AccessDenied" `
        -or $_.Exception.Message -match "(?i)access is denied|0x80070005"
    if (-not $isAccessDenied) {
        throw
    }

    $startup = [Environment]::GetFolderPath([Environment+SpecialFolder]::Startup)
    $launcher = Join-Path $startup "Cudy-Control-UI-Tunnel.cmd"
    $command = "@start `"`" /min powershell.exe $arguments"
    Set-Content -LiteralPath $launcher -Value $command -Encoding ASCII
    Write-Host "Scheduled Tasks requires elevation; installed per-user Startup launcher instead."
    Write-Host "Launcher: $launcher"

    if ($RunNow) {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Run("powershell.exe $arguments", 0, $false)
    }
}

Write-Host "UI: http://127.0.0.1:$LocalPort/"
Write-Host "Log: $LogPath"
