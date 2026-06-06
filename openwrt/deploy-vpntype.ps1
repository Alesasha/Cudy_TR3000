param(
  [string]$Router = "root@192.168.8.1",
  [string]$ConfigPath = "$env:USERPROFILE\vpn-subscriptions\vpntype.json",
  [string]$TagMapPath = "$env:USERPROFILE\vpn-subscriptions\vpntype.tags.txt"
)

if (-not (Test-Path -LiteralPath $ConfigPath)) {
  Write-Error "Config not found: $ConfigPath"
  exit 1
}

$scriptPath = Join-Path $PSScriptRoot "install-singbox-provider.sh"
if (-not (Test-Path -LiteralPath $scriptPath)) {
  Write-Error "Installer not found: $scriptPath"
  exit 1
}

Write-Host "Deploying vpntype to $Router"
Write-Host "Config: $ConfigPath"
Write-Host "You may be prompted for the router password by ssh/scp."

ssh $Router "mkdir -p /root/vpn-subscriptions /root/install"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

scp -O $ConfigPath "${Router}:/root/vpn-subscriptions/vpntype.json"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Test-Path -LiteralPath $TagMapPath) {
  scp -O $TagMapPath "${Router}:/root/vpn-subscriptions/vpntype.tags.txt"
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

scp -O $scriptPath "${Router}:/root/install/install-singbox-provider.sh"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

ssh $Router "chmod 600 /root/vpn-subscriptions/vpntype.json && chmod +x /root/install/install-singbox-provider.sh && /root/install/install-singbox-provider.sh vpntype /root/vpn-subscriptions/vpntype.json"
exit $LASTEXITCODE
