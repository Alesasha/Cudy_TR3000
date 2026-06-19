param(
    [string]$LogPath = "logs\local-forward-monitor.log",
    [int]$IntervalSeconds = 5,
    [int]$MaxLines = 3000,
    [string]$CudyIp = "192.168.8.1",
    [string]$UpstreamIp = "192.168.1.1",
    [string]$InternetIp = "77.88.55.242"
)

$ErrorActionPreference = "SilentlyContinue"
$logFullPath = if ([System.IO.Path]::IsPathRooted($LogPath)) {
    $LogPath
} else {
    Join-Path (Get-Location) $LogPath
}
$logDir = Split-Path -Parent $logFullPath
if ($logDir -and -not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

function Test-PingStatus {
    param([string]$Target)
    $ping = & ping.exe -n 1 -w 1000 $Target 2>$null
    if ($LASTEXITCODE -eq 0) {
        return "ok"
    }
    return "fail"
}

function Trim-Log {
    if (-not (Test-Path -LiteralPath $logFullPath)) {
        return
    }
    $lineCount = (Get-Content -LiteralPath $logFullPath | Measure-Object -Line).Lines
    if ($lineCount -gt $MaxLines) {
        Get-Content -LiteralPath $logFullPath -Tail $MaxLines | Set-Content -LiteralPath "$logFullPath.tmp" -Encoding UTF8
        Move-Item -LiteralPath "$logFullPath.tmp" -Destination $logFullPath -Force
    }
}

while ($true) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $routeLine = (& route.exe print -4 0.0.0.0 2>$null | Select-String -Pattern "^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+" | Select-Object -First 1).ToString().Trim()
    $adapterLine = (& netsh.exe interface show interface name="Ethernet" 2>$null | Select-String -Pattern "^\s*(Enabled|Disabled)")
    $dnsOk = "fail"
    try {
        Resolve-DnsName mail.ru -Type A -QuickTimeout -ErrorAction Stop | Out-Null
        $dnsOk = "ok"
    } catch {
        $dnsOk = "fail"
    }

    $line = "{0} adapter=""{1}"" ping_cudy={2} ping_gw={3} ping_inet={4} dns_mail_ru={5} default_route=""{6}""" -f `
        $ts,
        $(if ($adapterLine) { $adapterLine.ToString().Trim() } else { "unknown" }),
        (Test-PingStatus $CudyIp),
        (Test-PingStatus $UpstreamIp),
        (Test-PingStatus $InternetIp),
        $dnsOk,
        $(if ($routeLine) { $routeLine } else { "none" })

    Add-Content -LiteralPath $logFullPath -Value $line -Encoding UTF8
    Trim-Log
    Start-Sleep -Seconds $IntervalSeconds
}
