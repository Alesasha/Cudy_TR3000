param(
    [string]$CudyAddress = "192.168.8.1",
    [string]$ComputerAddress = "192.168.8.200"
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $PSCommandPath),
        "-CudyAddress", $CudyAddress,
        "-ComputerAddress", $ComputerAddress
    )
    Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
    exit
}

$root = Split-Path -Parent $PSScriptRoot
$recovery = Join-Path $PSScriptRoot "emergency_recover_cudy.py"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$windowLog = Join-Path $logDir ("cudy-lan-recovery-window-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$exitCode = 0

Start-Transcript -Path $windowLog -Force | Out-Null
try {
    Write-Host "Keep AmneziaVPN APP running as the temporary Internet channel."
    Write-Host "Move the Ethernet cable from the main router to a LAN port on Cudy."
    Read-Host "Press Enter after the cable is connected"

    Set-NetIPInterface -InterfaceAlias "Ethernet" -AddressFamily IPv4 -Dhcp Disabled
    Get-NetIPAddress -InterfaceAlias "Ethernet" -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
    Get-NetRoute -InterfaceAlias "Ethernet" -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object DestinationPrefix -eq "0.0.0.0/0" |
        Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
    New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress $ComputerAddress -PrefixLength 24 -DefaultGateway $CudyAddress | Out-Null
    Set-DnsClientServerAddress -InterfaceAlias "Ethernet" -ServerAddresses @($CudyAddress, "1.1.1.1")
    Set-NetIPInterface -InterfaceAlias "Ethernet" -AddressFamily IPv4 -AutomaticMetric Disabled -InterfaceMetric 50

    Write-Host "Waiting for Cudy LAN at $CudyAddress ..."
    $reachable = $false
    foreach ($attempt in 1..30) {
        if (Test-Connection -ComputerName $CudyAddress -Count 1 -Quiet) {
            $reachable = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $reachable) {
        throw "Cudy LAN did not become reachable. Move the cable back and run Restore-DirectEthernet.ps1."
    }

    Write-Host "`n== management ports =="
    foreach ($port in 22, 80, 443, 8765) {
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $pending = $client.BeginConnect($CudyAddress, $port, $null, $null)
            $open = $pending.AsyncWaitHandle.WaitOne(2000)
            if ($open) {
                try { $client.EndConnect($pending); $open = $true } catch { $open = $false }
            }
            Write-Host ("port {0}: {1}" -f $port, $(if ($open) { "open" } else { "closed/timeout" }))
        } finally {
            $client.Dispose()
        }
    }

    Push-Location $root
    try {
        & python $recovery --host $CudyAddress
        if ($LASTEXITCODE -ne 0) {
            throw "Cudy recovery command failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }

    Write-Host "`n== client probes =="
    ping.exe -n 3 $CudyAddress
    ping.exe -n 3 1.1.1.1
    curl.exe -4 --connect-timeout 5 --max-time 15 https://ifconfig.me/ip
    Write-Host "`nCudy is in safe direct mode. Go route apply and PBR are disabled."
} catch {
    $exitCode = 1
    Write-Host "`nRECOVERY FAILED: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "The window will remain open. Log: $windowLog"
    Write-Host "If needed, move Ethernet back to the main LAN and run Restore-DirectEthernet.ps1."
} finally {
    Stop-Transcript | Out-Null
    Read-Host "Press Enter to close"
}
exit $exitCode
