param(
  [Parameter(Mandatory = $true)]
  [string]$Path
)

if (-not (Test-Path -LiteralPath $Path)) {
  Write-Error "File not found: $Path"
  exit 1
}

$lines = Get-Content -LiteralPath $Path | Where-Object { $_.Trim() -ne "" }
$summary = [ordered]@{}
$vlessSecurity = [ordered]@{}
$vlessTransport = [ordered]@{}

foreach ($line in $lines) {
  $scheme = if ($line -match '^([A-Za-z][A-Za-z0-9+.-]*)://') { $matches[1].ToLowerInvariant() } else { 'unknown' }
  if (-not $summary.Contains($scheme)) { $summary[$scheme] = 0 }
  $summary[$scheme]++

  if ($scheme -eq 'vless') {
    $query = ''
    if ($line -match '\?([^#]*)') { $query = $matches[1] }
    $pairs = @{}
    foreach ($part in $query -split '&') {
      if ($part -match '^([^=]+)=(.*)$') {
        $pairs[$matches[1]] = [System.Uri]::UnescapeDataString($matches[2])
      }
    }
    $security = if ($pairs.ContainsKey('security') -and $pairs['security']) { $pairs['security'] } else { 'none' }
    $transport = if ($pairs.ContainsKey('type') -and $pairs['type']) { $pairs['type'] } else { 'tcp' }
    if (-not $vlessSecurity.Contains($security)) { $vlessSecurity[$security] = 0 }
    if (-not $vlessTransport.Contains($transport)) { $vlessTransport[$transport] = 0 }
    $vlessSecurity[$security]++
    $vlessTransport[$transport]++
  }
}

Write-Output "total=$($lines.Count)"
Write-Output "protocols:"
foreach ($key in $summary.Keys) {
  Write-Output "  $key=$($summary[$key])"
}

if ($vlessSecurity.Count -gt 0) {
  Write-Output "vless_security:"
  foreach ($key in $vlessSecurity.Keys) {
    Write-Output "  $key=$($vlessSecurity[$key])"
  }
}

if ($vlessTransport.Count -gt 0) {
  Write-Output "vless_transport:"
  foreach ($key in $vlessTransport.Keys) {
    Write-Output "  $key=$($vlessTransport[$key])"
  }
}
