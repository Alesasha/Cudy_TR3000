[CmdletBinding()]
param(
    [string]$EthernetAlias = "Ethernet",
    [int]$WifiInterfaceIndex = 23,
    [string]$CudyAddress = "192.168.8.1",
    [int]$VpnDisconnectWaitSeconds = 120,
    [string]$LogPath = (Join-Path $PSScriptRoot "..\logs\cudy-only-test.log")
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$logDirectory = Split-Path -Parent $LogPath
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-TestLog {
    param([string]$Message)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Message
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Restore-Fallback {
    Write-TestLog "ROLLBACK: restoring Wi-Fi and Ethernet DNS."
    Get-NetAdapter -InterfaceIndex $WifiInterfaceIndex -ErrorAction SilentlyContinue |
        Enable-NetAdapter -Confirm:$false -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 8
    Set-DnsClientServerAddress -InterfaceAlias $EthernetAlias -ResetServerAddresses -ErrorAction SilentlyContinue
    Write-TestLog "ROLLBACK completed."
}

Set-Content -Path $LogPath -Value "" -Encoding UTF8
$success = $false
try {
    Write-TestLog "READY: waiting up to ${VpnDisconnectWaitSeconds}s for the AmneziaVPN profile to disconnect."
    $deadline = (Get-Date).AddSeconds($VpnDisconnectWaitSeconds)
    do {
        $vpnAdapter = Get-NetAdapter -Name "AmneziaVPN" -ErrorAction SilentlyContinue
        if (-not $vpnAdapter -or $vpnAdapter.Status -ne "Up") {
            break
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($vpnAdapter -and $vpnAdapter.Status -eq "Up") {
        throw "Timed out waiting for the AmneziaVPN profile to disconnect."
    }

    Write-TestLog "AmneziaVPN is down; disabling Wi-Fi fallback immediately."
    Get-NetAdapter -InterfaceIndex $WifiInterfaceIndex -ErrorAction Stop |
        Disable-NetAdapter -Confirm:$false
    Set-DnsClientServerAddress -InterfaceAlias $EthernetAlias -ServerAddresses @($CudyAddress, "1.1.1.1")
    Clear-DnsClientCache
    Start-Sleep -Seconds 4

    $wifi = Get-NetAdapter -InterfaceIndex $WifiInterfaceIndex
    if ($wifi.Status -ne "Disabled") {
        throw "Wi-Fi adapter was not disabled (status=$($wifi.Status))."
    }

    $ethernetAddress = Get-NetIPAddress -InterfaceAlias $EthernetAlias -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -like "192.168.8.*" } |
        Select-Object -First 1
    if (-not $ethernetAddress) {
        throw "Ethernet has no 192.168.8.x address."
    }

    if (-not (Test-Connection -ComputerName $CudyAddress -Count 2 -Quiet)) {
        throw "Cudy LAN address is unreachable."
    }
    if (-not (Test-Connection -ComputerName "1.1.1.1" -Count 2 -Quiet)) {
        throw "Internet ICMP probe failed through Cudy."
    }

    $resolved = Resolve-DnsName -Name "ifconfig.me" -Type A -DnsOnly |
        Where-Object { $_.IPAddress } |
        Select-Object -First 1
    if (-not $resolved) {
        throw "DNS resolution failed through Cudy."
    }

    $egress = (& curl.exe -4 --silent --show-error --max-time 20 https://ifconfig.me/ip).Trim()
    if ($LASTEXITCODE -ne 0 -or $egress -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
        throw "External IPv4 probe failed through Cudy."
    }

    $chatgptCode = & curl.exe -4 --silent --show-error --output NUL --max-time 20 --write-out "%{http_code}" https://chatgpt.com/
    if ($LASTEXITCODE -ne 0 -or $chatgptCode -eq "000") {
        throw "ChatGPT HTTPS probe failed through Cudy."
    }

    $geminiCode = & curl.exe -4 --silent --show-error --output NUL --max-time 20 --write-out "%{http_code}" https://gemini.google.com/
    if ($LASTEXITCODE -ne 0 -or $geminiCode -eq "000") {
        throw "Gemini HTTPS probe failed through Cudy."
    }

    $telegram = Test-NetConnection -ComputerName "149.154.167.51" -Port 443 -InformationLevel Quiet
    if (-not $telegram) {
        throw "Telegram TCP probe failed through Cudy."
    }

    Write-TestLog "SUCCESS: Ethernet=$($ethernetAddress.IPAddress), DNS=$($resolved.IPAddress), egress=$egress, chatgpt_http=$chatgptCode, gemini_http=$geminiCode, telegram_tcp=ok."
    $success = $true
}
catch {
    Write-TestLog "FAILED: $($_.Exception.Message)"
    Restore-Fallback
    throw
}
finally {
    if ($success) {
        Write-TestLog "Cudy-only mode is active; Wi-Fi remains disabled."
    }
}
