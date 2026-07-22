param(
    [string]$SdkRoot = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$Serial = "",
    [int]$DurationMinutes = 240,
    [int]$SampleSeconds = 30,
    [string]$OutputDir = "$PSScriptRoot\..\build\android-soak"
)

$ErrorActionPreference = "Stop"
$package = "com.nashvpn.cudyagent"

function Resolve-Adb {
    $candidates = @(
        (Join-Path $SdkRoot "platform-tools\adb.exe"),
        "C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
        "adb.exe"
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    throw "adb.exe was not found."
}

$adb = Resolve-Adb
$deviceLines = & $adb devices
$online = @(
    $deviceLines |
        Where-Object { $_ -match "\tdevice$" } |
        ForEach-Object { ($_ -split "\s+")[0] }
)
if (-not $Serial) { $Serial = $online | Select-Object -First 1 }
if (-not $Serial -or $Serial -notin $online) {
    throw "No online Android device is available. Connect and unlock the phone, then retry."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$sessionDir = Join-Path $OutputDir "$stamp-$Serial"
New-Item -ItemType Directory -Force -Path $sessionDir | Out-Null
$samplesPath = Join-Path $sessionDir "samples.log"
$logcatPath = Join-Path $sessionDir "app-logcat.log"
$crashPath = Join-Path $sessionDir "crash-logcat.log"

function Invoke-Adb {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $adb -s $Serial @Arguments
}

function Add-Sample {
    $at = Get-Date -Format "yyyy-MM-ddTHH:mm:sszzz"
    "`n===== $at =====" | Add-Content -LiteralPath $samplesPath
    "pid=$(Invoke-Adb shell pidof $package)" | Add-Content -LiteralPath $samplesPath
    Invoke-Adb shell dumpsys activity services $package |
        Select-String -Pattern "CudyVpnService|isForeground|startRequested|createTime|lastActivity" |
        ForEach-Object Line |
        Add-Content -LiteralPath $samplesPath
    $jobLines = @(Invoke-Adb shell dumpsys jobscheduler)
    for ($index = 0; $index -lt $jobLines.Count; $index++) {
        if ($jobLines[$index] -match "JOB #.*2406[23].*CudyRecoveryJobService") {
            $end = [Math]::Min($jobLines.Count - 1, $index + 28)
            $jobLines[$index..$end] | Add-Content -LiteralPath $samplesPath
        }
    }
    $jobLines |
        Select-String -Pattern "2406[23].*com\.nashvpn\.cudyagent" |
        Select-Object -Last 8 |
        ForEach-Object Line |
        Add-Content -LiteralPath $samplesPath
    Invoke-Adb shell dumpsys package $package |
        Select-String -Pattern "stopped=|enabled=|BootReceiver|CudyRecoveryJobService" |
        Select-Object -First 12 |
        ForEach-Object Line |
        Add-Content -LiteralPath $samplesPath
    $connectivity = @(Invoke-Adb shell dumpsys connectivity)
    for ($index = 0; $index -lt $connectivity.Count; $index++) {
        if ($connectivity[$index] -match "NetworkAgentInfo.*type: VPN") {
            $end = [Math]::Min($connectivity.Count - 1, $index + 10)
            $connectivity[$index..$end] | Add-Content -LiteralPath $samplesPath
            break
        }
    }
}

Write-Host "Android soak monitor: serial=$Serial duration=${DurationMinutes}m sample=${SampleSeconds}s"
Write-Host "Output: $sessionDir"
Invoke-Adb logcat -c | Out-Null
$deadline = (Get-Date).AddMinutes([Math]::Max(1, $DurationMinutes))
try {
    while ((Get-Date) -lt $deadline) {
        Add-Sample
        Start-Sleep -Seconds ([Math]::Max(5, $SampleSeconds))
    }
}
finally {
    & $adb -s $Serial logcat -d -v threadtime "CudyAgent:I" "AndroidRuntime:E" "*:S" |
        Set-Content -LiteralPath $logcatPath
    & $adb -s $Serial logcat -b crash -d -v threadtime |
        Set-Content -LiteralPath $crashPath
    Add-Sample
    Write-Host "Soak diagnostics saved: $sessionDir"
}
