param(
    [string]$AgentId = "DC_via_Cudy-linux",
    [string]$AgentSecretsDir = "$PSScriptRoot\..\secrets\agents",
    [string]$SourceDir = "$PSScriptRoot\agent-linux",
    [string]$OutputDir = "$PSScriptRoot\..\secrets\agents",
    [switch]$IncludeRuntime,
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$source = (Resolve-Path -LiteralPath $SourceDir).Path
$agentDir = Join-Path $AgentSecretsDir $AgentId
if (-not (Test-Path -LiteralPath $agentDir)) {
    throw "Agent secrets directory not found: $agentDir"
}

function Copy-TextFileLf {
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )
    $text = [System.IO.File]::ReadAllText($SourcePath)
    $text = $text -replace "`r`n", "`n"
    $text = $text -replace "`r", "`n"
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($DestinationPath, $text, $encoding)
}

function New-ZipFromDirectoryUnix {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDirectory,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $DestinationPath) {
        Remove-Item -LiteralPath $DestinationPath -Force
    }
    $sourceResolved = (Resolve-Path -LiteralPath $SourceDirectory).Path
    $destinationResolved = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DestinationPath)
    $zip = [System.IO.Compression.ZipFile]::Open($destinationResolved, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -LiteralPath $sourceResolved -Recurse -File | ForEach-Object {
            $relative = $_.FullName.Substring($sourceResolved.Length).TrimStart('\', '/')
            $entryName = $relative -replace '\\', '/'
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip,
                $_.FullName,
                $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    } finally {
        $zip.Dispose()
    }
}

$requiredSecretFiles = @(
    "agent.env",
    "uswest_control_tunnel_ed25519",
    "uswest_control_tunnel_ed25519.pub"
)
foreach ($file in $requiredSecretFiles) {
    $path = Join-Path $agentDir $file
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required agent file is missing: $path"
    }
}

$stageRoot = Join-Path $root "build\agent-packages"
$stageDir = Join-Path $stageRoot $AgentId
if (Test-Path -LiteralPath $stageDir) {
    Remove-Item -LiteralPath $stageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

$sourceFiles = @(
    "QUICKSTART-RU.md",
    "README.md",
    "agent.env.example",
    "fresh_install_from_zip.sh",
    "install_singbox_runtime.sh",
    "install_systemd.sh",
    "managed_agent.sh",
    "one_click_install.sh",
    "restore_direct.sh",
    "start_singbox_transport.sh",
    "start_tunnel.sh",
    "status.sh",
    "stop_singbox_transport.sh",
    "test_prod_agent.sh",
    "uninstall_systemd.sh",
    "write_transport_plan.py"
)

foreach ($file in $sourceFiles) {
    $src = Join-Path $source $file
    if (-not (Test-Path -LiteralPath $src)) {
        throw "Source file is missing: $src"
    }
    Copy-TextFileLf -SourcePath $src -DestinationPath (Join-Path $stageDir $file)
}

Copy-TextFileLf -SourcePath (Join-Path $root "tools\route_agent.py") -DestinationPath (Join-Path $stageDir "route_agent.py")
Copy-TextFileLf -SourcePath (Join-Path $agentDir "agent.env") -DestinationPath (Join-Path $stageDir "agent.env")
Copy-Item -LiteralPath (Join-Path $agentDir "uswest_control_tunnel_ed25519") -Destination (Join-Path $stageDir "uswest_control_tunnel_ed25519") -Force
Copy-Item -LiteralPath (Join-Path $agentDir "uswest_control_tunnel_ed25519.pub") -Destination (Join-Path $stageDir "uswest_control_tunnel_ed25519.pub") -Force

if ($IncludeRuntime) {
    $runtime = Join-Path $agentDir "runtime"
    if (-not (Test-Path -LiteralPath $runtime)) {
        throw "Runtime directory requested but not found: $runtime"
    }
    Copy-Item -LiteralPath $runtime -Destination (Join-Path $stageDir "runtime") -Recurse -Force
}

$fileCount = (Get-ChildItem -LiteralPath $stageDir -Recurse -File | Measure-Object).Count
Write-Host "Linux agent package staged: $stageDir"
Write-Host "files=$fileCount"

if (-not $SkipZip) {
    $resolvedOutputDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
    New-Item -ItemType Directory -Force -Path $resolvedOutputDir | Out-Null
    $zipPath = Join-Path $resolvedOutputDir "$AgentId-prod.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    $stageItems = Get-ChildItem -LiteralPath $stageDir -Recurse -File -Force
    if (-not $stageItems) {
        throw "Stage directory is empty: $stageDir"
    }
    New-ZipFromDirectoryUnix -SourceDirectory $stageDir -DestinationPath $zipPath
    $freshInstallPath = Join-Path $resolvedOutputDir "$AgentId-install.sh"
    Copy-TextFileLf -SourcePath (Join-Path $source "fresh_install_from_zip.sh") -DestinationPath $freshInstallPath
    $selfInstallPath = Join-Path $resolvedOutputDir "$AgentId-self-install.sh"
    $zipBytes = [System.IO.File]::ReadAllBytes($zipPath)
    $zipBase64 = [Convert]::ToBase64String($zipBytes, [Base64FormattingOptions]::InsertLineBreaks)
    $selfHeader = @'
#!/usr/bin/env bash
set -euo pipefail

work_dir="$(pwd)"
script_path="${BASH_SOURCE[0]:-$0}"
script_name="$(basename "$script_path")"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/cudy-agent-self-install.XXXXXX")"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

extract_payload() {
  local output="$1"
  local marker="__CUDY_AGENT_ZIP_BASE64_BELOW__"
  if command -v awk >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1; then
    awk "found {print} /^$marker$/ {found=1}" "$script_path" | base64 -d > "$output"
    return 0
  fi
  python3 - "$script_path" "$output" <<'PY'
import base64
import sys
script, output = sys.argv[1:3]
marker = "__CUDY_AGENT_ZIP_BASE64_BELOW__\n"
data = open(script, "rb").read().split(marker.encode(), 1)[1]
open(output, "wb").write(base64.b64decode(data))
PY
}

echo "== extract embedded package =="
extract_payload "$tmp_dir/package.zip"

echo "== stop previous service if present =="
if command -v systemctl >/dev/null 2>&1; then
  sudo systemctl stop cudy-managed-agent.service 2>/dev/null || true
fi

echo "== remove old files and directories in $work_dir =="
find "$work_dir" -mindepth 1 -maxdepth 1 -print0 | while IFS= read -r -d '' item; do
  if [ "$(basename "$item")" = "$script_name" ]; then
    continue
  fi
  echo "remove: $item"
  sudo chmod -R u+rwX "$item" 2>/dev/null || true
  sudo rm -rf --one-file-system "$item"
done

echo "== unpack fresh package =="
if command -v unzip >/dev/null 2>&1; then
  set +e
  unzip -o "$tmp_dir/package.zip" -d "$work_dir"
  unzip_rc=$?
  set -e
  if [ "$unzip_rc" -gt 1 ]; then
    echo "ERROR: unzip failed with exit code $unzip_rc" >&2
    exit "$unzip_rc"
  fi
else
  python3 - "$tmp_dir/package.zip" "$work_dir" <<'PY'
import sys
import zipfile
archive, target = sys.argv[1:3]
with zipfile.ZipFile(archive) as zf:
    zf.extractall(target)
PY
fi

echo "== make scripts executable =="
chmod +x "$work_dir"/*.sh
if [ -f "$work_dir/runtime/sing-box" ]; then
  chmod +x "$work_dir/runtime/sing-box"
fi

echo "== install and start agent =="
cd "$work_dir"
sudo ./one_click_install.sh

echo
echo "== production smoke test =="
./test_prod_agent.sh

echo
echo "== final status =="
./status.sh || true

exit 0
__CUDY_AGENT_ZIP_BASE64_BELOW__
'@
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $selfInstallText = ($selfHeader.TrimEnd("`r", "`n") + "`n" + $zipBase64 + "`n") -replace "`r`n", "`n"
    $selfInstallText = $selfInstallText -replace "`r", "`n"
    [System.IO.File]::WriteAllText($selfInstallPath, $selfInstallText, $encoding)
    $selfInstallZipPath = Join-Path $resolvedOutputDir "$AgentId-self-install.zip"
    if (Test-Path -LiteralPath $selfInstallZipPath) {
        Remove-Item -LiteralPath $selfInstallZipPath -Force
    }
    $readmeSource = Join-Path $source "SELF-INSTALL-README-RU.txt"
    $readmeTemp = Join-Path ([System.IO.Path]::GetTempPath()) "$AgentId-SELF-INSTALL-README-RU.txt"
    if (Test-Path -LiteralPath $readmeSource) {
        Copy-TextFileLf -SourcePath $readmeSource -DestinationPath $readmeTemp
        Compress-Archive -LiteralPath @($selfInstallPath, $readmeTemp) -DestinationPath $selfInstallZipPath -Force
        Remove-Item -LiteralPath $readmeTemp -Force
    } else {
        Compress-Archive -LiteralPath $selfInstallPath -DestinationPath $selfInstallZipPath -Force
    }
    $zip = Get-Item -LiteralPath $zipPath
    Write-Host "Linux agent package zip: $($zip.FullName)"
    Write-Host "bytes=$($zip.Length)"
    Write-Host "modified=$($zip.LastWriteTime.ToString('s'))"
    Write-Host "Linux agent one-file installer: $freshInstallPath"
    Write-Host "Linux agent self-contained installer: $selfInstallPath"
    Write-Host "Linux agent self-contained zip: $selfInstallZipPath"
}
