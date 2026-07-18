param(
    [string]$Code = "",
    [string]$DeviceId = "",
    [string]$DisplayName = "Windows PC",
    [string]$BootstrapHost = "95.182.91.203",
    [string]$BootstrapUser = "cudy-enroll",
    [int]$BootstrapPort = 8766,
    [int]$LocalPort = 18766
)

$ErrorActionPreference = "Stop"

function ConvertTo-PowerShellLiteral {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('`', '``').Replace('"', '`"').Replace('$', '`$') + '"'
}

function Wait-LocalPort {
    param([int]$Port, [int]$TimeoutSeconds = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $client = [Net.Sockets.TcpClient]::new()
            $task = $client.ConnectAsync("127.0.0.1", $Port)
            if ($task.Wait(500) -and $client.Connected) {
                $client.Dispose()
                return
            }
            $client.Dispose()
        } catch {
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Enrollment tunnel did not become ready on 127.0.0.1:$Port."
}

if (-not $Code) {
    $Code = Read-Host "One-time activation code"
}
$Code = $Code.Trim()
if (-not $Code) {
    throw "One-time activation code is required."
}

$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
if (-not $ssh) {
    throw "Windows OpenSSH Client is required. Install the optional Windows OpenSSH Client feature and retry."
}

$bootstrapKey = Join-Path $PSScriptRoot "enrollment_bootstrap_ed25519"
$hostPublicKey = Join-Path $PSScriptRoot "control_ssh_host_ed25519.pub"
foreach ($path in @($bootstrapKey, $hostPublicKey)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Enrollment package file is missing: $path"
    }
}

$hostKeyParts = (Get-Content -LiteralPath $hostPublicKey -Raw).Trim() -split '\s+'
if ($hostKeyParts.Count -lt 2) {
    throw "Invalid control-server host public key."
}
$bootstrapKnownHosts = Join-Path $env:TEMP ("cudy-enrollment-known-hosts-{0}" -f [guid]::NewGuid().ToString("N"))
$bootstrapLog = Join-Path $env:TEMP ("cudy-enrollment-ssh-{0}.log" -f [guid]::NewGuid().ToString("N"))
[IO.File]::WriteAllText(
    $bootstrapKnownHosts,
    "$BootstrapHost $($hostKeyParts[0]) $($hostKeyParts[1])`n",
    [Text.UTF8Encoding]::new($false)
)
$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls $bootstrapKey /inheritance:r | Out-Null
icacls $bootstrapKey /grant:r "${currentUser}:R" | Out-Null

$sshArgs = @(
    "-i", ('"' + $bootstrapKey + '"'),
    "-p", "22",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
    "-o", "StrictHostKeyChecking=yes",
    "-o", ('UserKnownHostsFile="' + $bootstrapKnownHosts + '"'),
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ConnectTimeout=12",
    "-o", "ConnectionAttempts=1",
    "-N", "-L", "127.0.0.1:${LocalPort}:127.0.0.1:${BootstrapPort}",
    "${BootstrapUser}@${BootstrapHost}"
)

$process = $null
try {
    $process = Start-Process -FilePath $ssh.Source -ArgumentList $sshArgs -WindowStyle Hidden -PassThru -RedirectStandardError $bootstrapLog
    Wait-LocalPort -Port $LocalPort

    $payload = @{
        code = $Code
        device_id = $DeviceId.Trim()
        display_name = $DisplayName.Trim()
        platform = "windows"
    } | ConvertTo-Json -Compress
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:${LocalPort}/api/agent/enroll" `
        -ContentType "application/json" `
        -Body $payload `
        -TimeoutSec 30

    if (-not $result.token -or -not $result.device_id -or -not $result.provisioning.ssh_private_key) {
        throw "Enrollment response is incomplete."
    }

    $deviceKey = Join-Path $PSScriptRoot "uswest_control_tunnel_ed25519"
    $knownHosts = Join-Path $PSScriptRoot "known_hosts"
    $envPath = Join-Path $PSScriptRoot "agent.env.ps1"
    [IO.File]::WriteAllText($deviceKey, ([string]$result.provisioning.ssh_private_key).TrimEnd() + "`n", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText(
        $knownHosts,
        "$($result.provisioning.ssh_host) $($hostKeyParts[0]) $($hostKeyParts[1])`n",
        [Text.UTF8Encoding]::new($false)
    )

    $versionCode = "1"
    $versionPath = Join-Path $PSScriptRoot "agent.version.json"
    if (Test-Path -LiteralPath $versionPath) {
        try { $versionCode = [string]((Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json).version_code) } catch { }
    }
    $lines = @(
        '$env:VPN_CONTROL_URL = "http://127.0.0.1:18765"',
        '$env:VPN_CONTROL_URLS = "http://10.77.0.1:8765,http://192.168.8.1:8765"',
        '$env:VPN_CONTROL_ENDPOINT_MANIFEST_URLS = "http://10.77.0.1/cudy-control/endpoints.json,http://192.168.8.1/cudy-control/endpoints.json"',
        '$env:VPN_CONTROL_PRIMARY_SSH_HOST = ' + (ConvertTo-PowerShellLiteral ([string]$result.provisioning.ssh_host)),
        '$env:VPN_CONTROL_PRIMARY_SSH_USER = ' + (ConvertTo-PowerShellLiteral ([string]$result.provisioning.ssh_user)),
        '$env:VPN_CONTROL_PRIMARY_SSH_HOST_KEY_SHA256 = ' + (ConvertTo-PowerShellLiteral ([string]$result.provisioning.ssh_host_key_sha256)),
        '$env:VPN_CONTROL_PRIMARY_SSH_KEY = "$PSScriptRoot\uswest_control_tunnel_ed25519"',
        '$env:VPN_AGENT_TOKEN = ' + (ConvertTo-PowerShellLiteral ([string]$result.token)),
        '$env:VPN_AGENT_DEVICE_ID = ' + (ConvertTo-PowerShellLiteral ([string]$result.device_id)),
        '$env:AGENT_VERSION_CODE = ' + (ConvertTo-PowerShellLiteral $versionCode),
        '$env:AGENT_AUTO_UPDATE = "1"'
    )
    [IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))

    icacls $deviceKey /inheritance:r | Out-Null
    icacls $deviceKey /grant:r "${currentUser}:R" | Out-Null
    Write-Host "Device activated: $($result.device_id)"
    Write-Host "Configuration saved. The one-time code cannot be reused."
} catch {
    $detail = ""
    if (Test-Path -LiteralPath $bootstrapLog) {
        $detail = ((Get-Content -LiteralPath $bootstrapLog -Tail 20 -ErrorAction SilentlyContinue) -join "`n").Trim()
    }
    if ($detail) {
        throw "$($_.Exception.Message)`nSSH details:`n$detail"
    }
    throw
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $bootstrapKnownHosts -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $bootstrapLog -Force -ErrorAction SilentlyContinue
}
