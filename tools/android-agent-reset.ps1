param(
    [string]$SdkRoot = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$Serial = "",
    [string]$PackageName = "com.nashvpn.cudyagent",
    [switch]$Status,
    [switch]$ForceStop,
    [switch]$ClearData,
    [switch]$Uninstall,
    [switch]$RevokePermissions,
    [switch]$ResetLogcat
)

$ErrorActionPreference = "Stop"

function Resolve-Adb {
    param([string]$Root)
    $candidates = @(
        (Join-Path $Root "platform-tools\adb.exe"),
        "C:\Program Files (x86)\Android\android-sdk\platform-tools\adb.exe",
        "adb.exe"
    )
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            return $cmd.Source
        }
    }
    throw "adb.exe was not found. Pass -SdkRoot or install Android platform-tools."
}

$adb = Resolve-Adb -Root $SdkRoot
Write-Host "Using adb: $adb"
$devicesOutput = & $adb devices
$devicesOutput
$onlineDevices = @(
    $devicesOutput |
        Where-Object { $_ -match "\tdevice$" } |
        ForEach-Object { ($_ -split "\s+")[0] }
)
if ($onlineDevices.Count -eq 0) {
    throw "No online Android devices detected."
}

if (-not $Serial) {
    $Serial = @($onlineDevices | Where-Object { $_ -notlike "emulator-*" } | Select-Object -First 1)
    if (-not $Serial) {
        $Serial = @($onlineDevices | Select-Object -First 1)
    }
}
Write-Host "Using device: $Serial"

function Invoke-Adb {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & $adb -s $Serial @Args
}

function Show-Status {
    Write-Host ""
    Write-Host "== package =="
    Invoke-Adb shell pm path $PackageName 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Package is not installed: $PackageName"
        return
    }

    Write-Host ""
    Write-Host "== process =="
    Invoke-Adb shell pidof $PackageName 2>$null

    Write-Host ""
    Write-Host "== service =="
    Invoke-Adb shell dumpsys activity services $PackageName |
        Select-String -Pattern "CudyVpnService|isForeground|startRequested|lastStartId" |
        Select-Object -First 30

    Write-Host ""
    Write-Host "== appops / permissions =="
    Invoke-Adb shell dumpsys package $PackageName |
        Select-String -Pattern "POST_NOTIFICATIONS|RECEIVE_BOOT_COMPLETED|REQUEST_IGNORE_BATTERY_OPTIMIZATIONS|android.permission.INTERNET|granted=" |
        Select-Object -First 80

    Write-Host ""
    Write-Host "== recent logcat =="
    Invoke-Adb logcat -d -t 180 |
        Select-String -Pattern "CudyAgent|CudyVpnService|CudyBoot|AndroidRuntime|FATAL|monodroid|nashvpn" |
        Select-Object -Last 80
}

$hasAction = $ForceStop -or $ClearData -or $Uninstall -or $RevokePermissions -or $ResetLogcat
if (-not $hasAction -or $Status) {
    Show-Status
}

if ($ResetLogcat) {
    Write-Host ""
    Write-Host "Clearing logcat..."
    Invoke-Adb logcat -c
}

if ($ForceStop -or $ClearData -or $Uninstall) {
    Write-Host ""
    Write-Host "Force-stopping $PackageName..."
    Invoke-Adb shell am force-stop $PackageName
}

if ($RevokePermissions) {
    Write-Host ""
    Write-Host "Revoking runtime permissions where Android allows it..."
    Invoke-Adb shell pm revoke $PackageName android.permission.POST_NOTIFICATIONS 2>$null
}

if ($ClearData) {
    Write-Host ""
    Write-Host "Clearing app data for $PackageName..."
    Invoke-Adb shell pm clear $PackageName
}

if ($Uninstall) {
    Write-Host ""
    Write-Host "Uninstalling $PackageName..."
    Invoke-Adb uninstall $PackageName
}

if ($hasAction) {
    Show-Status
}
