param(
    [string]$SdkRoot = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$AgentSecretDir = "C:\Users\Alexander\Cudy_TR3000\secrets\agents\isasha_X7Pro_Cudy-android",
    [string]$ApkPath = "C:\Users\Alexander\Cudy_TR3000\apps\CudyAndroidAgent\bin\Debug\net10.0-android\android-arm64\com.nashvpn.cudyagent-Signed.apk",
    [string]$ControlUrl = "http://127.0.0.1:18765",
    [string]$SshHost = "95.182.91.203",
    [string]$SshUser = "cudy-tunnel-windows",
    [string]$Serial = "",
    [int]$WaitSeconds = 25,
    [switch]$NoInstall,
    [switch]$NoStart,
    [switch]$StartEngine,
    [string]$DebugProbeUrl = "",
    [string]$DebugProbeCandidates = ""
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
$agent = Get-Content (Join-Path $AgentSecretDir "agent.json") -Raw | ConvertFrom-Json
$keyPath = Join-Path $AgentSecretDir "uswest_control_tunnel_ed25519"
$sshKey = Get-Content $keyPath -Raw
$sshKeyB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($sshKey))

Write-Host "Using adb: $adb"
$devicesOutput = & $adb devices
$devicesOutput
$onlineDevices = @(
    $devicesOutput |
        Where-Object { $_ -match "\tdevice$" } |
        ForEach-Object { ($_ -split "\s+")[0] }
)
if ($onlineDevices.Count -eq 0) {
    Write-Host "No online Android devices detected."
    if (-not $NoInstall -or -not $NoStart) {
        throw "Connect a device or start an emulator before running install/start steps."
    }
}

if (-not $Serial) {
    $Serial = @($onlineDevices | Where-Object { $_ -notlike "emulator-*" } | Select-Object -First 1)
    if (-not $Serial) {
        $Serial = @($onlineDevices | Select-Object -First 1)
    }
}
if ($Serial) {
    Write-Host "Using device: $Serial"
}

function Invoke-Adb {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    if ($Serial) {
        & $adb -s $Serial @Args
    }
    else {
        & $adb @Args
    }
}

if (-not $NoInstall) {
    if ($Serial) {
        Write-Host "Force-stopping existing app instance..."
        Invoke-Adb shell am force-stop com.nashvpn.cudyagent
    }
    if (-not (Test-Path $ApkPath)) {
        throw "APK not found: $ApkPath"
    }
    $installOutput = Invoke-Adb install --no-incremental -r -d $ApkPath 2>&1
    $installOutput
    if ($LASTEXITCODE -ne 0 -and ($installOutput -join "`n") -match "Unknown option|unknown option|Invalid option") {
        Write-Host "Retrying install without --no-incremental for older adb..."
        Invoke-Adb install -r -d $ApkPath
    }
    elseif ($LASTEXITCODE -ne 0) {
        throw "APK install failed."
    }
}

if (-not $NoStart) {
    $controlOnly = -not $StartEngine
    $controlOnlyArg = $controlOnly.ToString().ToLowerInvariant()
    Invoke-Adb logcat -c
    Invoke-Adb shell am start `
        -n "com.nashvpn.cudyagent/com.nashvpn.cudyagent.MainActivity" `
        --es control_url $ControlUrl `
        --es device_id $agent.id `
        --es token $agent.token `
        --es ssh_host $SshHost `
        --es ssh_user $SshUser `
        --es ssh_key_b64 $sshKeyB64 `
        --es debug_probe_url $DebugProbeUrl `
        --es debug_probe_candidates $DebugProbeCandidates `
        --ez fetch_policy false `
        --ez start_agent true `
        --ez control_only $controlOnlyArg
}

Write-Host ""
if ($onlineDevices.Count -gt 0 -and -not $NoStart) {
    if ($WaitSeconds -gt 0) {
        Write-Host "Waiting ${WaitSeconds}s for the first control loop..."
        Start-Sleep -Seconds $WaitSeconds
    }
    Write-Host "Process:"
    Invoke-Adb shell pidof com.nashvpn.cudyagent

    Write-Host ""
    Write-Host "Current Android window:"
    Invoke-Adb shell dumpsys window |
        Select-String -Pattern "mCurrentFocus|mFocusedApp|Keyguard|mDreamingLockscreen|mShowingLockscreen" |
        Select-Object -First 12

    Write-Host ""
    Write-Host "Service:"
    Invoke-Adb shell dumpsys activity services com.nashvpn.cudyagent |
        Select-String -Pattern "CudyVpnService|isForeground|startRequested" |
        Select-Object -First 20

    Write-Host ""
    Write-Host "Stored safe status:"
    try {
        Invoke-Adb shell run-as com.nashvpn.cudyagent cat /data/data/com.nashvpn.cudyagent/shared_prefs/cudy-agent.xml 2>$null |
            Select-String -Pattern "service_status|service_status_at|last_policy_at|last_policy_summary|debug_probe_at|debug_probe_result"
    }
    catch {
        Write-Host "run-as is unavailable for this build; using logcat/dumpsys diagnostics instead."
    }

    Write-Host ""
    Write-Host "Device network probe:"
    if ($Serial) {
        & $adb -s $Serial shell ping -c 2 -W 3 1.1.1.1
    }
    else {
        & $adb shell ping -c 2 -W 3 1.1.1.1
    }

    Write-Host ""
    Write-Host "Recent app logcat lines:"
    Invoke-Adb logcat -d -t 260 |
        Select-String -Pattern "CudyAgent|CudyVpnService|Control loop|Control error|SSH tunnel|AndroidRuntime|FATAL|monodroid|nashvpn" |
        Select-Object -Last 120
}
