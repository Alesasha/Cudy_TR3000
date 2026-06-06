param(
  [Parameter(Mandatory = $true)]
  [string]$InputPath,

  [Parameter(Mandatory = $true)]
  [string]$OutputPath,

  [string]$TagMapPath,

  [string]$InterfaceName = "vpntype",
  [string]$TunAddress = "172.19.0.1/30",
  [int]$Mtu = 1400,
  [int]$MaxOutbounds = 12
)

if (-not (Test-Path -LiteralPath $InputPath)) {
  Write-Error "Input file not found: $InputPath"
  exit 1
}

function Decode-Base64Url([string]$Value) {
  $s = $Value.Replace('-', '+').Replace('_', '/')
  $pad = (4 - ($s.Length % 4)) % 4
  $s = $s + ('=' * $pad)
  return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($s))
}

function Parse-Query([string]$Query) {
  $result = @{}
  if ([string]::IsNullOrWhiteSpace($Query)) { return $result }
  foreach ($part in $Query -split '&') {
    if ($part -match '^([^=]+)=(.*)$') {
      $result[$matches[1]] = [Uri]::UnescapeDataString($matches[2])
    } elseif ($part) {
      $result[$part] = ''
    }
  }
  return $result
}

function New-VlessOutbound([string]$Line, [int]$Index) {
  $uri = [Uri]$Line
  $query = Parse-Query $uri.Query.TrimStart('?')
  $security = if ($query.ContainsKey('security')) { $query['security'] } else { 'none' }
  $transport = if ($query.ContainsKey('type')) { $query['type'] } else { 'tcp' }

  if ($transport -ne 'tcp') { return $null }
  if ($security -notin @('reality', 'tls', 'none')) { return $null }

  $out = [ordered]@{
    type = 'vless'
    tag = "vless-$Index"
    server = $uri.Host
    server_port = $uri.Port
    uuid = $uri.UserInfo
    network = 'tcp'
  }

  if ($query.ContainsKey('flow') -and $query['flow']) {
    $out.flow = $query['flow']
  }

  if ($security -eq 'reality') {
    $tls = [ordered]@{
      enabled = $true
      server_name = $query['sni']
      utls = [ordered]@{
        enabled = $true
        fingerprint = $(if ($query['fp']) { $query['fp'] } else { 'chrome' })
      }
      reality = [ordered]@{
        enabled = $true
        public_key = $query['pbk']
        short_id = $query['sid']
      }
    }
    $out.tls = $tls
  } elseif ($security -eq 'tls') {
    $tls = [ordered]@{
      enabled = $true
    }
    if ($query.ContainsKey('sni') -and $query['sni']) {
      $tls.server_name = $query['sni']
    } elseif ($query.ContainsKey('host') -and $query['host']) {
      $tls.server_name = $query['host']
    }
    $out.tls = $tls
  }

  return $out
}

function New-ShadowsocksOutbound([string]$Line, [int]$Index) {
  $withoutScheme = $Line.Substring(5)
  $beforeFragment = ($withoutScheme -split '#', 2)[0]
  $at = $beforeFragment.LastIndexOf('@')
  if ($at -lt 0) { return $null }

  $userinfoEncoded = $beforeFragment.Substring(0, $at)
  $hostPort = $beforeFragment.Substring($at + 1)
  $colon = $hostPort.LastIndexOf(':')
  if ($colon -lt 0) { return $null }

  $userinfo = Decode-Base64Url $userinfoEncoded
  $methodPassword = $userinfo -split ':', 2
  if ($methodPassword.Count -ne 2) { return $null }

  return [ordered]@{
    type = 'shadowsocks'
    tag = "ss-$Index"
    server = $hostPort.Substring(0, $colon)
    server_port = [int]$hostPort.Substring($colon + 1)
    method = $methodPassword[0]
    password = $methodPassword[1]
  }
}

$lines = Get-Content -LiteralPath $InputPath -Encoding UTF8 | Where-Object { $_.Trim() -ne "" }
$outbounds = @()
$candidateTags = @()
$tagMap = @()
$i = 1

foreach ($line in $lines) {
  if ($outbounds.Count -ge $MaxOutbounds) { break }
  $line = $line.Trim()
  $name = ''
  try {
    $fragment = ([Uri]$line).Fragment
    if ($fragment) { $name = [System.Uri]::UnescapeDataString($fragment.TrimStart('#')) }
  } catch {
    $name = ''
  }
  $out = $null
  if ($line.StartsWith('vless://')) {
    $out = New-VlessOutbound $line $i
  } elseif ($line.StartsWith('ss://')) {
    $out = New-ShadowsocksOutbound $line $i
  }
  if ($null -ne $out) {
    $outbounds += $out
    $candidateTags += $out.tag
    $tagMap += [ordered]@{
      tag = $out.tag
      type = $out.type
      name = $name
    }
    $i++
  }
}

if ($outbounds.Count -eq 0) {
  Write-Error "No sing-box-compatible VLESS tcp or Shadowsocks nodes found."
  exit 2
}

$outbounds += [ordered]@{
  type = 'urltest'
  tag = "$InterfaceName-auto"
  outbounds = $candidateTags
  url = 'https://www.gstatic.com/generate_204'
  interval = '10m'
  tolerance = 100
}
$outbounds += [ordered]@{ type = 'direct'; tag = 'direct' }
$outbounds += [ordered]@{ type = 'block'; tag = 'block' }

$config = [ordered]@{
  log = [ordered]@{
    level = 'info'
    timestamp = $true
  }
  inbounds = @(
    [ordered]@{
      type = 'tun'
      tag = "$InterfaceName-tun"
      interface_name = $InterfaceName
      address = @($TunAddress)
      mtu = $Mtu
      auto_route = $false
      strict_route = $false
      stack = 'system'
    }
  )
  outbounds = $outbounds
  route = [ordered]@{
    auto_detect_interface = $true
    final = "$InterfaceName-auto"
  }
}

$dir = Split-Path -Parent $OutputPath
if ($dir) {
  New-Item -ItemType Directory -Force $dir | Out-Null
}

$json = ($config | ConvertTo-Json -Depth 20).TrimStart([char]0xFEFF)
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($OutputPath, $json, $utf8NoBom)

Write-Output "written=$OutputPath"
if (-not $TagMapPath) {
  $TagMapPath = [System.IO.Path]::ChangeExtension($OutputPath, ".tags.txt")
}
$tagMapLines = $tagMap | ForEach-Object { "$($_.tag)`t$($_.type)`t$($_.name)" }
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($TagMapPath, ($tagMapLines -join [Environment]::NewLine), $utf8NoBom)
Write-Output "tag_map=$TagMapPath"
Write-Output "interface=$InterfaceName"
Write-Output "outbounds=$($candidateTags.Count)"
Write-Output "tags=$($candidateTags -join ',')"
