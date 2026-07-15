param(
    [string]$TaskName = "Cudy Managed Route Agent",
    [string]$PhysicalInterfaceAlias = "",
    [string]$Gateway = "",
    [int]$LocalPort = 18765,
    [switch]$KeepTaskEnabled
)

$ErrorActionPreference = "Continue"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Argument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

if (-not (Test-IsAdministrator)) {
    $forward = [System.Collections.Generic.List[string]]::new()
    $forward.Add("-NoProfile") | Out-Null
    $forward.Add("-ExecutionPolicy") | Out-Null
    $forward.Add("Bypass") | Out-Null
    $forward.Add("-File") | Out-Null
    $forward.Add((Quote-Argument $PSCommandPath)) | Out-Null
    if ($TaskName) { $forward.Add("-TaskName"); $forward.Add((Quote-Argument $TaskName)) }
    if ($PhysicalInterfaceAlias) { $forward.Add("-PhysicalInterfaceAlias"); $forward.Add((Quote-Argument $PhysicalInterfaceAlias)) }
    if ($Gateway) { $forward.Add("-Gateway"); $forward.Add((Quote-Argument $Gateway)) }
    $forward.Add("-LocalPort"); $forward.Add([string]$LocalPort)
    if ($KeepTaskEnabled) { $forward.Add("-KeepTaskEnabled") }
    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList ($forward -join " ") -Wait
    exit $LASTEXITCODE
}

Write-Host "Cudy agent emergency shutdown" -ForegroundColor Yellow

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $KeepTaskEnabled) {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    }
    Write-Host "Scheduled task stopped$($(if ($KeepTaskEnabled) { '' } else { ' and disabled' }))."
}

$managedRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$managedProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $command = [string]$_.CommandLine
    $executable = [string]$_.ExecutablePath
    ($command -and $command.IndexOf($managedRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
    ($executable -and $executable.IndexOf($managedRoot, [StringComparison]::OrdinalIgnoreCase) -eq 0)
}
foreach ($process in $managedProcesses | Sort-Object ProcessId -Descending) {
    if ($process.ProcessId -eq $PID) { continue }
    Write-Host "Stopping managed process pid=$($process.ProcessId) name=$($process.Name)"
    & taskkill.exe /PID $process.ProcessId /T /F 2>$null | Out-Null
}

Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    $process = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -in @("ssh", "powershell", "pwsh")) {
        Write-Host "Stopping control listener pid=$($process.Id) port=$LocalPort"
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
    }
}

$managedAdapters = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
    $_.InterfaceDescription -in @("sing-tun Tunnel", "WireGuard Tunnel") -or
    $_.Name -match '^(proxy|lokvpn-|AmneziaVPN|Nash)'
})
$managedIfIndexes = @($managedAdapters | Select-Object -ExpandProperty InterfaceIndex -Unique)

Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like 'AmneziaWGTunnel$*'
} | ForEach-Object {
    Write-Host "Stopping AWG transport service $($_.Name)"
    Stop-Service -Name $_.Name -Force -ErrorAction SilentlyContinue
}

if (Test-Path -LiteralPath "$PSScriptRoot\Stop-SingBoxTransport.ps1") {
    & "$PSScriptRoot\Stop-SingBoxTransport.ps1" -All
}

foreach ($ifIndex in $managedIfIndexes) {
    Get-NetRoute -AddressFamily IPv4 -InterfaceIndex $ifIndex -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}

foreach ($prefix in @("0.0.0.0/1", "128.0.0.0/1")) {
    Get-NetRoute -AddressFamily IPv4 -DestinationPrefix $prefix -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
}

if (-not $PhysicalInterfaceAlias) {
    $candidate = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -ne "0.0.0.0" -and $_.InterfaceIndex -notin $managedIfIndexes } |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if ($candidate) {
        $PhysicalInterfaceAlias = [string]$candidate.InterfaceAlias
        if (-not $Gateway) { $Gateway = [string]$candidate.NextHop }
    }
}

if (-not $PhysicalInterfaceAlias) {
    $candidateAdapter = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
        Where-Object Status -eq "Up" | Select-Object -First 1
    if ($candidateAdapter) { $PhysicalInterfaceAlias = [string]$candidateAdapter.Name }
}

if ($PhysicalInterfaceAlias) {
    $physical = Get-NetAdapter -Name $PhysicalInterfaceAlias -ErrorAction SilentlyContinue
    if ($physical) {
        Set-NetIPInterface -InterfaceIndex $physical.InterfaceIndex -AddressFamily IPv4 -InterfaceMetric 1 -ErrorAction SilentlyContinue
        if (-not $Gateway) {
            $Gateway = [string](Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $physical.InterfaceIndex -ErrorAction SilentlyContinue |
                Where-Object NextHop -ne "0.0.0.0" | Select-Object -First 1 -ExpandProperty NextHop)
        }
        if ($Gateway) {
            $default = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $physical.InterfaceIndex -ErrorAction SilentlyContinue |
                Where-Object NextHop -eq $Gateway | Select-Object -First 1
            if (-not $default) {
                New-NetRoute -DestinationPrefix "0.0.0.0/0" -InterfaceIndex $physical.InterfaceIndex -NextHop $Gateway -RouteMetric 0 -PolicyStore ActiveStore -ErrorAction SilentlyContinue | Out-Null
            }
            Set-DnsClientServerAddress -InterfaceIndex $physical.InterfaceIndex -ServerAddresses @($Gateway, "1.1.1.1") -ErrorAction SilentlyContinue
            Write-Host "Direct route restored via $Gateway on $PhysicalInterfaceAlias."
        }
    }
}

Clear-DnsClientCache -ErrorAction SilentlyContinue

Write-Host "`nRemaining default routes:"
Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object DestinationPrefix -in @("0.0.0.0/0", "0.0.0.0/1", "128.0.0.0/1") |
    Sort-Object DestinationPrefix, RouteMetric, InterfaceMetric |
    Format-Table DestinationPrefix, InterfaceAlias, NextHop, RouteMetric, InterfaceMetric -AutoSize

Write-Host "Connectivity check:"
ping.exe -4 -n 2 1.1.1.1
curl.exe -4 --connect-timeout 5 --max-time 12 https://ifconfig.me/ip
Write-Host ""
Write-Host "Emergency shutdown complete. The agent will stay off until its scheduled task is enabled again." -ForegroundColor Green
