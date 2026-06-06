param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("vpntype", "lokvpn")]
  [string]$Provider,

  [string]$Url,

  [string]$OutputDir = "$env:USERPROFILE\vpn-subscriptions"
)

New-Item -ItemType Directory -Force $OutputDir | Out-Null

if (-not $Url) {
  $clip = Get-Clipboard -Raw -ErrorAction SilentlyContinue
  if ($clip -and $clip.Trim() -match '^https?://') {
    $Url = $clip.Trim()
  }
}

if (-not $Url) {
  $Url = Read-Host "Paste $Provider subscription URL"
}

if ($Url -notmatch '^https?://') {
  Write-Error "Expected http(s) subscription URL"
  exit 1
}

$rawPath = Join-Path $OutputDir "$Provider.sub.txt"
$decodedPath = Join-Path $OutputDir "$Provider.decoded.txt"

curl.exe -L `
  -A "v2rayN/6.0" `
  -H "Accept: text/plain,*/*" `
  -o $rawPath `
  $Url

if ($LASTEXITCODE -ne 0) {
  exit $LASTEXITCODE
}

$raw = Get-Content -LiteralPath $rawPath -Raw -ErrorAction SilentlyContinue
if ($null -eq $raw -or (Get-Item -LiteralPath $rawPath).Length -eq 0) {
  Write-Warning "Downloaded file is empty."
  Write-Output "raw=$rawPath"
  Write-Output "decoded="
  Write-Output "bytes=0"
  exit 2
}
$trimmed = $raw.Trim()

if ($trimmed -match '^(vless|vmess|trojan|ss|hy2|hysteria2)://' -or $trimmed.StartsWith('{') -or $trimmed.StartsWith('[') -or $trimmed -match '^\s*[a-zA-Z0-9_-]+:') {
  Copy-Item -LiteralPath $rawPath -Destination $decodedPath -Force
} else {
  try {
    $compact = ($trimmed -replace '\s', '')
    $pad = (4 - ($compact.Length % 4)) % 4
    $compact = $compact + ('=' * $pad)
    $decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($compact))
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($decodedPath, $decoded, $utf8NoBom)
  } catch {
    Write-Warning "Downloaded file is not plain subscription and base64 decode failed. Keeping raw only."
  }
}

Write-Output "raw=$rawPath"
if (Test-Path -LiteralPath $decodedPath) {
  Write-Output "decoded=$decodedPath"
  powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\subscription-summary.ps1" -Path $decodedPath
} else {
  Write-Output "decoded="
  Write-Output "bytes=$((Get-Item -LiteralPath $rawPath).Length)"
}
