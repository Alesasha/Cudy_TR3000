param(
    [string]$OutputDir = "$PSScriptRoot\..\build\universal-agents",
    [string]$WindowsVersionName = "1.25",
    [int]$WindowsVersionCode = 26,
    [string]$LinuxVersionName = "1.24",
    [int]$LinuxVersionCode = 25,
    [ValidateSet("windows", "linux")]
    [string[]]$Platforms = @("windows", "linux")
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$output = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
$bootstrapKey = Join-Path $root "secrets\android_enrollment_bootstrap_ed25519"
$hostKey = Join-Path $root "config\control_ssh_host_ed25519.pub"
foreach ($path in @($bootstrapKey, $hostKey)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required universal enrollment file is missing: $path"
    }
}

New-Item -ItemType Directory -Force -Path $output | Out-Null
& "$PSScriptRoot\Build-AgentUpdateArtifacts.ps1" `
    -OutputDir (Join-Path $root "build\universal-agent-updates") `
    -VersionName $WindowsVersionName `
    -VersionCode $WindowsVersionCode `
    -LinuxVersionName $LinuxVersionName `
    -LinuxVersionCode $LinuxVersionCode `
    -Platforms $Platforms

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function New-UniversalZip {
    param(
        [Parameter(Mandatory = $true)][string]$Platform,
        [Parameter(Mandatory = $true)][string[]]$AdditionalFiles
    )
    $stage = Join-Path $root "build\agent-updates-stage\$Platform"
    if (-not (Test-Path -LiteralPath $stage)) {
        throw "Agent update stage is missing: $stage"
    }
    foreach ($sourcePath in $AdditionalFiles) {
        Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $stage (Split-Path -Leaf $sourcePath)) -Force
    }
    Copy-Item -LiteralPath $bootstrapKey -Destination (Join-Path $stage "enrollment_bootstrap_ed25519") -Force
    Copy-Item -LiteralPath $hostKey -Destination (Join-Path $stage "control_ssh_host_ed25519.pub") -Force

    $zipPath = Join-Path $output "Cudy-Agent-$Platform-universal.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    $zip = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -LiteralPath $stage -Recurse -File | ForEach-Object {
            $relative = $_.FullName.Substring($stage.Length).TrimStart('\', '/')
            $entryName = $relative -replace '\\', '/'
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip,
                $_.FullName,
                $entryName,
                [IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    } finally {
        $zip.Dispose()
    }
    $item = Get-Item -LiteralPath $zipPath
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
    Write-Host "$($item.FullName) bytes=$($item.Length) sha256=$hash"
}

$selected = @($Platforms | ForEach-Object { $_.ToLowerInvariant() } | Select-Object -Unique)
if ($selected -contains "windows") {
    New-UniversalZip -Platform "windows" -AdditionalFiles @(
        (Join-Path $root "tools\agent-windows\Enroll-Agent.ps1"),
        (Join-Path $root "tools\agent-windows\Install-UniversalAgent.ps1")
    )
}
if ($selected -contains "linux") {
    New-UniversalZip -Platform "linux" -AdditionalFiles @(
        (Join-Path $root "tools\agent-linux\enroll_agent.sh"),
        (Join-Path $root "tools\agent-linux\install_universal.sh")
    )
}
