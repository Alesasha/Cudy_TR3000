param(
    [string]$GoMsi = "C:\Users\Alexander\Downloads\go1.26.4.windows-amd64.msi",
    [string]$SdkManager = "C:\Program Files (x86)\Android\android-sdk\cmdline-tools\latest\bin\sdkmanager.bat",
    [string]$Java17Home = "C:\Users\Alexander\AppData\Local\Android\jdk-17",
    [string]$JavaSdkManagerHome = "C:\Program Files\Android\openjdk\jdk-21.0.8",
    [string]$AndroidPlatform = "platforms;android-36",
    [string]$AndroidBuildTools = "build-tools;36.0.0",
    [string]$NdkPackage = "ndk;28.0.13004108",
    [string]$BindPlatform = "android/arm64",
    [switch]$SkipCudyWanOverride,
    [switch]$SkipApkBuild
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildTools = Join-Path $Root "build\tools"
$GoRoot = Join-Path $BuildTools "go-msi-extract\Go"
$GoExe = Join-Path $GoRoot "bin\go.exe"
$AndroidHome = Join-Path $BuildTools "android-sdk"
$NdkHome = Join-Path $AndroidHome "ndk\28.0.13004108"
$GoPath = Join-Path $BuildTools "gopath"
$GoCache = Join-Path $BuildTools "gocache"
$SingBoxDir = Join-Path $Root "build\external\sing-box"
$LibsDir = Join-Path $Root "apps\CudyAndroidAgent\Libs"

function Add-CudyWanOverride {
    if ($SkipCudyWanOverride) {
        return
    }
    $domains = @(
        "dl.google.com",
        "github.com",
        "proxy.golang.org",
        "sum.golang.org",
        "go.dev",
        "storage.googleapis.com",
        "android.googlesource.com",
        "go.googlesource.com"
    )
    $ips = @()
    foreach ($domain in $domains) {
        $ips += Resolve-DnsName $domain -Type A -ErrorAction SilentlyContinue |
            Where-Object IPAddress |
            Select-Object -ExpandProperty IPAddress
    }
    $ips = @($ips | Sort-Object -Unique)
    if ($ips.Count -eq 0) {
        Write-Warning "No download IPs resolved for Cudy WAN override."
        return
    }

    $ipFile = Join-Path $Root "build\wan-override-ips.txt"
    New-Item -ItemType Directory -Force -Path (Split-Path $ipFile) | Out-Null
    $ips | Set-Content -Path $ipFile -Encoding ascii

    $python = @'
from pathlib import Path
import paramiko

root = Path.cwd()
password_file = root / "secrets" / "cudy_ssh_password.txt"
ip_file = root / "build" / "wan-override-ips.txt"
if not password_file.exists() or not ip_file.exists():
    raise SystemExit(0)

password = password_file.read_text(encoding="utf-8").strip()
ips = [line.strip() for line in ip_file.read_text().splitlines() if line.strip()]
if not ips:
    raise SystemExit(0)

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect("192.168.8.1", username="root", password=password, timeout=8, banner_timeout=8, auth_timeout=8)
elements = ", ".join(ips)
cmd = f"nft add element inet fw4 pbr_wan_4_dst_ip_user {{ {elements} }} 2>/dev/null || true"
stdin, stdout, stderr = client.exec_command(cmd, timeout=12)
stdout.read()
stderr.read()
client.close()
print(f"Added {len(ips)} current download IPs to Cudy pbr_wan_4_dst_ip_user.")
'@
    $python | python -
}

function Ensure-Go {
    if (Test-Path $GoExe) {
        return
    }
    if (-not (Test-Path $GoMsi)) {
        throw "Go MSI not found: $GoMsi"
    }
    $target = Join-Path $BuildTools "go-msi-extract"
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $log = Join-Path $Root "build\go-msi-extract.log"
    $process = Start-Process msiexec.exe -ArgumentList @("/a", $GoMsi, "/qn", "TARGETDIR=$target", "/L*v", $log) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Go MSI extract failed with exit code $($process.ExitCode). See $log"
    }
    if (-not (Test-Path $GoExe)) {
        throw "go.exe was not found after MSI extract: $GoExe"
    }
}

function Ensure-AndroidSdk {
    if (-not (Test-Path $SdkManager)) {
        throw "sdkmanager was not found: $SdkManager"
    }
    New-Item -ItemType Directory -Force -Path $AndroidHome | Out-Null
    $env:JAVA_HOME = $JavaSdkManagerHome
    $env:Path = "$env:JAVA_HOME\bin;$env:Path"

    $needsInstall = -not (Test-Path (Join-Path $NdkHome "source.properties")) -or
        -not (Test-Path (Join-Path $AndroidHome "platforms\android-36\source.properties")) -or
        -not (Test-Path (Join-Path $AndroidHome "build-tools\36.0.0\source.properties"))
    if ($needsInstall) {
        cmd /c "echo y| `"$SdkManager`" --sdk_root=`"$AndroidHome`" --install `"$NdkPackage`" `"$AndroidPlatform`" `"$AndroidBuildTools`""
    }

    $systemPlatform = "C:\Program Files (x86)\Android\android-sdk\platforms\android-36"
    $localPlatform = Join-Path $AndroidHome "platforms\android-36"
    if (-not (Test-Path (Join-Path $localPlatform "source.properties")) -and (Test-Path (Join-Path $systemPlatform "source.properties"))) {
        Remove-Item -LiteralPath $localPlatform -Recurse -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path (Split-Path $localPlatform) | Out-Null
        Copy-Item -LiteralPath $systemPlatform -Destination $localPlatform -Recurse -Force
    }
}

function Set-BuildEnvironment {
    $env:GOROOT = $GoRoot
    $env:GOPATH = $GoPath
    $env:GOBIN = Join-Path $GoPath "bin"
    $env:GOCACHE = $GoCache
    $env:JAVA_HOME = $Java17Home
    $env:ANDROID_HOME = $AndroidHome
    $env:ANDROID_SDK_HOME = $AndroidHome
    $env:ANDROID_NDK_HOME = $NdkHome
    $env:NDK = $NdkHome
    New-Item -ItemType Directory -Force -Path $env:GOBIN, $env:GOCACHE | Out-Null
    $env:Path = "$GoRoot\bin;$env:GOBIN;$Java17Home\bin;$NdkHome\toolchains\llvm\prebuilt\windows-x86_64\bin;$env:Path"
}

function Invoke-GoCleanEnv {
    param(
        [string]$WorkingDirectory,
        [string]$Arguments,
        [string]$LogPath
    )
    $cleanPath = "$GoRoot\bin;$env:GOBIN;$Java17Home\bin;$NdkHome\toolchains\llvm\prebuilt\windows-x86_64\bin;C:\Windows\System32;C:\Windows;C:\Windows\System32\WindowsPowerShell\v1.0;C:\Program Files\Git\cmd"
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $GoExe
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.Arguments = $Arguments
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.Environment.Clear()
    $envs = @{
        "GOROOT" = $GoRoot
        "GOPATH" = $env:GOPATH
        "GOBIN" = $env:GOBIN
        "GOCACHE" = $env:GOCACHE
        "GO111MODULE" = "on"
        "JAVA_HOME" = $Java17Home
        "ANDROID_HOME" = $AndroidHome
        "ANDROID_SDK_HOME" = $AndroidHome
        "ANDROID_NDK_HOME" = $NdkHome
        "NDK" = $NdkHome
        "PATH" = $cleanPath
        "TEMP" = $env:TEMP
        "TMP" = $env:TMP
        "LOCALAPPDATA" = $env:LOCALAPPDATA
        "APPDATA" = $env:APPDATA
        "USERPROFILE" = $env:USERPROFILE
        "HOMEDRIVE" = $env:HOMEDRIVE
        "HOMEPATH" = $env:HOMEPATH
        "USERNAME" = $env:USERNAME
        "SystemRoot" = $env:SystemRoot
        "ComSpec" = $env:ComSpec
        "PROCESSOR_ARCHITECTURE" = $env:PROCESSOR_ARCHITECTURE
        "NUMBER_OF_PROCESSORS" = $env:NUMBER_OF_PROCESSORS
    }
    foreach ($entry in $envs.GetEnumerator()) {
        if ($entry.Value) {
            $psi.Environment[$entry.Key] = $entry.Value
        }
    }
    $process = [System.Diagnostics.Process]::Start($psi)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $output = $stdoutTask.Result + "`n--- STDERR ---`n" + $stderrTask.Result
    Set-Content -Path $LogPath -Value $output -Encoding utf8
    if ($process.ExitCode -ne 0) {
        Get-Content -Path $LogPath -Tail 100
        throw "go $Arguments failed with exit code $($process.ExitCode). See $LogPath"
    }
}

Add-CudyWanOverride
Ensure-Go
Ensure-AndroidSdk
Set-BuildEnvironment

& $GoExe install -v github.com/sagernet/gomobile/cmd/gomobile@v0.1.13
& $GoExe install -v github.com/sagernet/gomobile/cmd/gobind@v0.1.13

if (-not (Test-Path (Join-Path $SingBoxDir ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $SingBoxDir) | Out-Null
    git clone --depth 1 https://github.com/SagerNet/sing-box.git $SingBoxDir
}

Remove-Item -LiteralPath (Join-Path $SingBoxDir "libbox.aar"), (Join-Path $SingBoxDir "libbox-legacy.aar") -Force -ErrorAction SilentlyContinue
Invoke-GoCleanEnv -WorkingDirectory $SingBoxDir -Arguments "run ./cmd/internal/build_libbox -target android -platform $BindPlatform" -LogPath (Join-Path $Root "build\libbox-arm64-build.log")

New-Item -ItemType Directory -Force -Path $LibsDir | Out-Null
Copy-Item -LiteralPath (Join-Path $SingBoxDir "libbox.aar") -Destination (Join-Path $LibsDir "libbox.aar") -Force

if (-not $SkipApkBuild) {
    dotnet build (Join-Path $Root "apps\CudyAndroidAgent\CudyAndroidAgent.csproj") -v:minimal -p:UseSharedCompilation=false -p:BuildInParallel=false -p:RuntimeIdentifier=android-arm64
}

Get-Item (Join-Path $LibsDir "libbox.aar") | Select-Object FullName, Length, LastWriteTime
