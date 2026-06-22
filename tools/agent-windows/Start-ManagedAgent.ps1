param(
    [string]$ServerId = "",
    [string]$InterfaceAlias = "",
    [string[]]$DirectTransport = @("aktau=AmneziaVPN=aktau-awg.conf"),
    [string[]]$VpnTypeTransport = @(),
    [string[]]$LokVpnTransport = @(),
    [string[]]$SingBoxTransport = @(),
    [string[]]$ExtraInterfaceMap = @(),
    [string]$ControlHostName = "95.182.91.203",
    [string]$ControlSshUser = "cudy-tunnel-windows",
    [string]$ControlKeyPath = "$PSScriptRoot\uswest_control_tunnel_ed25519",
    [string]$ControlEndpointManifestUrls = $env:VPN_CONTROL_ENDPOINT_MANIFEST_URLS,
    [int]$PollSeconds = 60,
    [int]$LocalPort = 18765,
    [string]$LogPath = "$PSScriptRoot\managed-agent.log",
    [switch]$VerboseRoutes,
    [switch]$NoControlTransportPlan,
    [switch]$NoDirectTransports,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\agent.env.ps1"
if (-not $ControlEndpointManifestUrls -and $env:VPN_CONTROL_ENDPOINT_MANIFEST_URLS) {
    $ControlEndpointManifestUrls = $env:VPN_CONTROL_ENDPOINT_MANIFEST_URLS
}
if ($ControlHostName -eq "95.182.91.203" -and $env:VPN_CONTROL_PRIMARY_SSH_HOST) {
    $ControlHostName = $env:VPN_CONTROL_PRIMARY_SSH_HOST
}
if ($ControlSshUser -eq "cudy-tunnel-windows" -and $env:VPN_CONTROL_PRIMARY_SSH_USER) {
    $ControlSshUser = $env:VPN_CONTROL_PRIMARY_SSH_USER
}
if ($ControlKeyPath -eq "$PSScriptRoot\uswest_control_tunnel_ed25519" -and $env:VPN_CONTROL_PRIMARY_SSH_KEY) {
    $ControlKeyPath = $env:VPN_CONTROL_PRIMARY_SSH_KEY
}
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $OutputEncoding = [Console]::OutputEncoding
    $env:PYTHONUTF8 = "1"
} catch {
}

function Rotate-AgentLog {
    param([int64]$MaxBytes = 5242880)
    if (-not (Test-Path -LiteralPath $LogPath)) {
        return
    }
    $item = Get-Item -LiteralPath $LogPath -ErrorAction SilentlyContinue
    if ($null -eq $item -or $item.Length -lt $MaxBytes) {
        return
    }
    $archive = "$LogPath.1"
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $LogPath -Destination $archive -Force
}

function Write-AgentLine {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")]
        [string]$Level = "INFO"
    )
    $line = "[$((Get-Date).ToString('s'))] [$Level] $Message"
    if ($Level -eq "WARN") {
        Write-Warning $Message
    } elseif ($Level -eq "ERROR") {
        Write-Error $Message
    } else {
        Write-Host $Message
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
    Add-Content -LiteralPath $LogPath -Value $line
}

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Start-ManagedAgent.ps1 must be run as Administrator."
    }
}

function Test-ControlServer {
    try {
        $uri = "http://127.0.0.1:$LocalPort/healthz"
        $reply = Invoke-WebRequest -UseBasicParsing -Uri $uri -TimeoutSec 5
        return ($reply.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Stop-LocalTunnelListener {
    $listeners = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        if ($listener.OwningProcess -eq $PID) {
            continue
        }
        $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        if ($null -eq $proc) {
            continue
        }
        if ($proc.ProcessName -in @("ssh", "powershell", "pwsh")) {
            Write-AgentLine "Stopping stale local tunnel listener pid=$($proc.Id) name=$($proc.ProcessName) port=$LocalPort"
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        } else {
            throw "127.0.0.1:$LocalPort is used by pid=$($proc.Id) name=$($proc.ProcessName), not stopping it automatically."
        }
    }
}

function Get-ControlTunnelHostFromManifest {
    if (-not $ControlEndpointManifestUrls) {
        return $ControlHostName
    }
    foreach ($url in ($ControlEndpointManifestUrls -split "[,;]" | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        try {
            $manifest = Invoke-RestMethod -UseBasicParsing -Uri $url -TimeoutSec 5
            $endpoint = @($manifest.endpoints |
                Sort-Object @{ Expression = { [int]($_.priority) } } |
                Where-Object { $_.role -eq "primary" -and $_.ssh_tunnel -and $_.ssh_tunnel.host } |
                Select-Object -First 1)
            if ($endpoint.Count -gt 0) {
                $hostFromManifest = [string]$endpoint[0].ssh_tunnel.host
                if ($hostFromManifest) {
                    if ($hostFromManifest -ne $ControlHostName) {
                        Write-AgentLine "Control manifest selected SSH host $hostFromManifest from $url"
                    }
                    return $hostFromManifest
                }
            }
        } catch {
            Write-AgentLine "Control manifest unavailable: $url $($_.Exception.Message)" -Level WARN
        }
    }
    return $ControlHostName
}

function Ensure-ControlTunnel {
    if (Test-ControlServer) {
        return
    }

    Stop-LocalTunnelListener

    $script = Join-Path $PSScriptRoot "Start-Tunnel.ps1"
    $selectedHost = Get-ControlTunnelHostFromManifest
    Write-AgentLine "Starting SSH control tunnel on 127.0.0.1:$LocalPort via $selectedHost"
    Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$script`"",
        "-HostName", "$selectedHost",
        "-User", "$ControlSshUser",
        "-KeyPath", "`"$ControlKeyPath`"",
        "-LocalPort", "$LocalPort"
    ) | Out-Null

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Seconds 1
        if (Test-ControlServer) {
            return
        }
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Control tunnel did not become healthy on 127.0.0.1:$LocalPort."
}

function Parse-DirectTransport {
    param([string[]]$Items)
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($item in $Items) {
        if (-not $item) {
            continue
        }
        $parts = $item -split "=", 3
        if ($parts.Count -ne 3) {
            throw "DirectTransport must look like server_id=TunnelName=config.conf: $item"
        }
        $server = $parts[0].Trim()
        $tunnel = $parts[1].Trim()
        $config = $parts[2].Trim()
        if (-not $server -or -not $tunnel -or -not $config) {
            throw "DirectTransport must look like server_id=TunnelName=config.conf: $item"
        }
        $configPath = if ([System.IO.Path]::IsPathRooted($config)) {
            $config
        } else {
            Join-Path $PSScriptRoot $config
        }
        $result.Add([pscustomobject]@{
            ServerId = $server
            TunnelName = $tunnel
            ConfigPath = $configPath
        }) | Out-Null
    }
    return $result.ToArray()
}

function Parse-SingBoxTransport {
    param([string[]]$Items)
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($item in $Items) {
        if (-not $item) {
            continue
        }
        $parts = $item -split "=", 3
        if ($parts.Count -ne 3) {
            throw "SingBoxTransport must look like server_id=InterfaceName=config.json: $item"
        }
        $server = $parts[0].Trim()
        $iface = $parts[1].Trim()
        $config = $parts[2].Trim()
        if (-not $server -or -not $iface -or -not $config) {
            throw "SingBoxTransport must look like server_id=InterfaceName=config.json: $item"
        }
        $configPath = if ([System.IO.Path]::IsPathRooted($config)) {
            $config
        } else {
            Join-Path $PSScriptRoot $config
        }
        $result.Add([pscustomobject]@{
            ServerId = $server
            InterfaceName = $iface
            ConfigPath = $configPath
        }) | Out-Null
    }
    return $result.ToArray()
}

function Parse-VpnTypeTransport {
    param([string[]]$Items)
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($item in $Items) {
        if (-not $item) {
            continue
        }
        $parts = $item -split "=", 2
        if ($parts.Count -ne 2) {
            throw "VpnTypeTransport must look like provider=InterfaceName, for example proxyde=proxyde: $item"
        }
        $provider = $parts[0].Trim()
        $iface = $parts[1].Trim()
        if (-not $provider -or -not $iface) {
            throw "VpnTypeTransport must look like provider=InterfaceName, for example proxyde=proxyde: $item"
        }
        $result.Add([pscustomobject]@{
            Provider = $provider
            InterfaceName = $iface
            ConfigPath = (Join-Path $PSScriptRoot "transports\$iface.json")
        }) | Out-Null
    }
    return $result.ToArray()
}

function Parse-LokVpnTransport {
    param([string[]]$Items)
    $validProfiles = @("smart1", "de1", "ru1", "nl1", "fr1", "se1", "smart2", "de2", "ru2", "nl2", "fr2", "se2")
    $result = New-Object System.Collections.Generic.List[object]
    foreach ($item in $Items) {
        if (-not $item) {
            continue
        }
        $parts = $item -split "=", 2
        if ($parts.Count -ne 2) {
            throw "LokVpnTransport must look like profile=InterfaceName, for example de1=lokvpn-de1: $item"
        }
        $profile = $parts[0].Trim()
        $iface = $parts[1].Trim()
        if ($profile -notin $validProfiles) {
            throw "Unknown LokVPN profile '$profile'."
        }
        if (-not $iface) {
            throw "LokVpnTransport must look like profile=InterfaceName, for example de1=lokvpn-de1: $item"
        }
        $result.Add([pscustomobject]@{
            ServerId = "lokvpn-$profile"
            Profile = $profile
            InterfaceName = $iface
            ConfigPath = (Join-Path $PSScriptRoot "transports\$iface.json")
        }) | Out-Null
    }
    return $result.ToArray()
}

function Parse-InterfaceMap {
    param([string[]]$Items)
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($item in $Items) {
        if (-not $item) {
            continue
        }
        if ($item -notmatch "^[^=]+=[^=]+$") {
            throw "Interface map must look like server_id=InterfaceAlias: $item"
        }
        $result.Add($item.Trim()) | Out-Null
    }
    return $result.ToArray()
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Get-AgentConfig {
    $env:VPN_CONTROL_URL = "http://127.0.0.1:$LocalPort"
    $output = & python "$PSScriptRoot\route_agent.py" config --json 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String).Trim()
    if ($exitCode -ne 0) {
        throw "route_agent.py config failed with exit code $exitCode. $text"
    }
    return $text | ConvertFrom-Json
}

function Get-DeterministicTunAddress {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [int]$SecondOctet
    )
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Name))
    } finally {
        $sha.Dispose()
    }
    $value = ([int]$bytes[0] * 256) + [int]$bytes[1]
    $thirdOctet = 2 + ($value % 238)
    return "172.$SecondOctet.$thirdOctet.1/30"
}

function Write-ControlTransportConfig {
    param(
        [Parameter(Mandatory = $true)]
        $Transport,
        [Parameter(Mandatory = $true)]
        [string]$ConfigPath
    )
    $type = [string]$Transport.transport_type
    $iface = [string]$Transport.interface_name
    $config = $Transport.config
    if (-not $iface) {
        throw "transport_plan item has no interface_name for $($Transport.server_id)"
    }

    if ($type -eq "http-proxy-tun") {
        $proxyType = [string]$config.proxy_type
        if (-not $proxyType) {
            $proxyType = "http"
        }
        & "$PSScriptRoot\New-SingBoxHttpProxyConfig.ps1" `
            -Name $iface `
            -InterfaceName $iface `
            -ProxyType $proxyType `
            -ProxyHost ([string]$config.server) `
            -ProxyPort ([int]$config.server_port) `
            -TunAddress (Get-DeterministicTunAddress -Name $iface -SecondOctet 41) `
            -OutputPath $ConfigPath | Out-Null
        return
    }

    if ($type -eq "vless-reality-tun") {
        $tls = $config.tls
        $reality = $tls.reality
        & "$PSScriptRoot\New-SingBoxVlessRealityConfig.ps1" `
            -Name $iface `
            -InterfaceName $iface `
            -Server ([string]$config.server) `
            -ServerPort ([int]$config.server_port) `
            -Uuid ([string]$config.uuid) `
            -Flow ([string]$config.flow) `
            -ServerName ([string]$tls.server_name) `
            -PublicKey ([string]$reality.public_key) `
            -ShortId ([string]$reality.short_id) `
            -TunAddress (Get-DeterministicTunAddress -Name $iface -SecondOctet 43) `
            -OutputPath $ConfigPath | Out-Null
        return
    }

    if ($type -eq "sing-box-json") {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ConfigPath) | Out-Null
        Write-Utf8NoBom -Path $ConfigPath -Value ($config | ConvertTo-Json -Depth 50)
        return
    }

    throw "Unsupported control transport type '$type' for $($Transport.server_id)."
}

function Ensure-ControlTransport {
    param($Transport)
    $iface = [string]$Transport.interface_name
    $configPath = Join-Path $PSScriptRoot "transports\$iface.json"
    $oldConfig = if (Test-Path -LiteralPath $configPath) { Get-Content -Raw -LiteralPath $configPath } else { "" }
    Write-ControlTransportConfig -Transport $Transport -ConfigPath $configPath
    $newConfig = Get-Content -Raw -LiteralPath $configPath
    $restart = ($oldConfig -ne "" -and $oldConfig -ne $newConfig)
    if ($restart) {
        Write-AgentLine "Transport config changed for $iface; restarting managed transport."
    }
    & "$PSScriptRoot\Start-SingBoxTransport.ps1" -Name $iface -ConfigPath $configPath -Restart:$restart
}

function Ensure-AwgTransport {
    param(
        [string]$TunnelName,
        [string]$ConfigPath
    )
    $serviceName = "AmneziaWGTunnel`$$TunnelName"
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($null -ne $service -and $service.Status -eq "Running") {
        return
    }

    Write-AgentLine "Starting managed AWG transport: $TunnelName"
    & "$PSScriptRoot\Start-AwgTransport.ps1" -TunnelName $TunnelName -ConfigPath $ConfigPath
}

function Ensure-SingBoxTransport {
    param(
        [string]$Name,
        [string]$ConfigPath
    )
    & "$PSScriptRoot\Start-SingBoxTransport.ps1" -Name $Name -ConfigPath $ConfigPath
}

function Ensure-VpnTypeTransport {
    param(
        [string]$Provider,
        [string]$Name,
        [string]$ConfigPath
    )
    & "$PSScriptRoot\Update-VpnTypeProxyConfig.ps1" -Provider $Provider -Name $Name -OutputPath $ConfigPath -RestartIfChanged | Out-Null
    & "$PSScriptRoot\Start-SingBoxTransport.ps1" -Name $Name -ConfigPath $ConfigPath
}

function Ensure-LokVpnTransport {
    param(
        [string]$Profile,
        [string]$Name,
        [string]$ConfigPath
    )
    & "$PSScriptRoot\Update-LokVpnConfig.ps1" -Profile $Profile -Name $Name -OutputPath $ConfigPath -RestartIfChanged | Out-Null
    & "$PSScriptRoot\Start-SingBoxTransport.ps1" -Name $Name -ConfigPath $ConfigPath
}

function Apply-PolicyRoutes {
    param([string[]]$InterfaceMaps)
    $env:VPN_CONTROL_URL = "http://127.0.0.1:$LocalPort"
    $args = @(
        "$PSScriptRoot\route_agent.py",
        "apply",
        "--direct-baseline"
    )
    foreach ($map in $InterfaceMaps) {
        $args += @("--interface-map", $map)
    }
    $args += @("--yes", "--post-status")
    if (-not $VerboseRoutes) {
        $args += "--json"
    }

    $output = & python @args 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String).Trim()

    if ($VerboseRoutes -and $text) {
        Write-Host $text
    }

    if ($exitCode -ne 0) {
        if ($text) {
            Add-Content -LiteralPath $LogPath -Value "[$((Get-Date).ToString('s'))] route_agent failed exit=$exitCode`n$text"
        }
        throw "route_agent.py failed with exit code $exitCode."
    }

    if ($VerboseRoutes) {
        return [pscustomobject]@{
            Applied = $true
            IpRoutes = "-"
            DomainRoutes = "-"
            Commands = "-"
            StatusPosted = "-"
        }
    }

    try {
        $plan = $text | ConvertFrom-Json
        $failedCommands = @($plan.applied_commands | Where-Object { -not $_.ok })
        if ($failedCommands.Count -gt 0) {
            Add-Content -LiteralPath $LogPath -Value "[$((Get-Date).ToString('s'))] failed route commands`n$text"
            throw "$($failedCommands.Count) route command(s) failed."
        }
        return [pscustomobject]@{
            Applied = $true
            IpRoutes = @($plan.ip_routes).Count
            DomainRoutes = @($plan.domain_routes).Count
            Commands = @($plan.applied_commands).Count
            StatusPosted = [bool]$plan.posted_status.ok
        }
    } catch {
        Add-Content -LiteralPath $LogPath -Value "[$((Get-Date).ToString('s'))] could not parse route_agent output`n$text"
        throw
    }
}

function Run-ProbeJobs {
    param([string[]]$InterfaceMaps)
    $env:VPN_CONTROL_URL = "http://127.0.0.1:$LocalPort"
    $args = @(
        "$PSScriptRoot\route_agent.py",
        "probe-jobs",
        "--limit", "2",
        "--json"
    )
    foreach ($map in $InterfaceMaps) {
        $args += @("--interface-map", $map)
    }

    $output = & python @args 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String).Trim()

    if ($exitCode -ne 0) {
        if ($text) {
            Add-Content -LiteralPath $LogPath -Value "[$((Get-Date).ToString('s'))] route_agent probe-jobs failed exit=$exitCode`n$text"
        }
        throw "route_agent.py probe-jobs failed with exit code $exitCode."
    }
    if (-not $text) {
        return [pscustomobject]@{
            Jobs = 0
            Completed = 0
            Failed = 0
        }
    }
    try {
        $result = $text | ConvertFrom-Json
        return [pscustomobject]@{
            Jobs = [int]$result.jobs
            Completed = @($result.completed).Count
            Failed = @($result.failed).Count
        }
    } catch {
        Add-Content -LiteralPath $LogPath -Value "[$((Get-Date).ToString('s'))] could not parse probe-jobs output`n$text"
        throw
    }
}

function Stop-UnusedSingBoxTransports {
    param([string[]]$DesiredNames)
    $desired = @{}
    foreach ($name in $DesiredNames) {
        if ($name) {
            $desired[$name] = $true
        }
    }

    $stateDir = Join-Path $PSScriptRoot "run"
    $managedNames = New-Object System.Collections.Generic.HashSet[string]
    if (Test-Path -LiteralPath $stateDir) {
        foreach ($pidFile in Get-ChildItem -LiteralPath $stateDir -Filter "*.pid" -File -ErrorAction SilentlyContinue) {
            [void]$managedNames.Add($pidFile.BaseName)
        }
    }
    foreach ($adapter in Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -eq "sing-tun Tunnel" }) {
        if ($adapter.Name -match "^(proxy|lokvpn)") {
            [void]$managedNames.Add($adapter.Name)
        }
    }

    foreach ($name in $managedNames) {
        if (-not $desired.ContainsKey($name)) {
            Write-AgentLine "Stopping unused sing-box transport: $name"
            & "$PSScriptRoot\Stop-SingBoxTransport.ps1" -Name $name | Out-Null
        }
    }
}

Assert-Admin
Rotate-AgentLog
Write-AgentLine "Managed agent process starting. pid=$PID script=$PSCommandPath"

$directSpecs = if ($NoDirectTransports) { @() } else { Parse-DirectTransport $DirectTransport }
if ($directSpecs.Count -gt 1) {
    throw "The installed AmneziaVPN Windows tunnel service supports only one active AWG tunnel. Pass exactly one -DirectTransport, for example aktau=AmneziaVPN=aktau-awg.conf."
}
$vpnTypeSpecs = Parse-VpnTypeTransport $VpnTypeTransport
$lokVpnSpecs = Parse-LokVpnTransport $LokVpnTransport
$singBoxSpecs = Parse-SingBoxTransport $SingBoxTransport
$interfaceMaps = New-Object System.Collections.Generic.List[string]
foreach ($spec in $directSpecs) {
    $interfaceMaps.Add("$($spec.ServerId)=$($spec.TunnelName)") | Out-Null
}
foreach ($spec in $vpnTypeSpecs) {
    $interfaceMaps.Add("$($spec.Provider)=$($spec.InterfaceName)") | Out-Null
}
foreach ($spec in $lokVpnSpecs) {
    $interfaceMaps.Add("$($spec.ServerId)=$($spec.InterfaceName)") | Out-Null
}
foreach ($spec in $singBoxSpecs) {
    $interfaceMaps.Add("$($spec.ServerId)=$($spec.InterfaceName)") | Out-Null
}
foreach ($map in (Parse-InterfaceMap $ExtraInterfaceMap)) {
    $interfaceMaps.Add($map) | Out-Null
}
if ($ServerId -or $InterfaceAlias) {
    if (-not $ServerId -or -not $InterfaceAlias) {
        throw "ServerId and InterfaceAlias must be passed together."
    }
    $interfaceMaps.Add("${ServerId}=${InterfaceAlias}") | Out-Null
}
if ($interfaceMaps.Count -eq 0 -and $NoControlTransportPlan) {
    throw "No server-to-interface mappings configured."
}
$baseInterfaceMaps = $interfaceMaps.ToArray()

Write-AgentLine "Managed agent started: maps=$($interfaceMaps -join ',') control_transport_plan=$(-not $NoControlTransportPlan) poll=${PollSeconds}s control=http://127.0.0.1:$LocalPort"

do {
    $startedAt = Get-Date
    try {
        Ensure-ControlTunnel
        $cycleInterfaceMaps = New-Object System.Collections.Generic.List[string]
        foreach ($map in $baseInterfaceMaps) {
            $cycleInterfaceMaps.Add($map) | Out-Null
        }
        if (-not $NoControlTransportPlan) {
            $agentConfig = Get-AgentConfig
            $desiredControlTransports = New-Object System.Collections.Generic.List[string]
            foreach ($transport in @($agentConfig.transport_plan)) {
                $desiredControlTransports.Add([string]$transport.interface_name) | Out-Null
            }
            foreach ($spec in $vpnTypeSpecs) {
                $desiredControlTransports.Add($spec.InterfaceName) | Out-Null
            }
            foreach ($spec in $lokVpnSpecs) {
                $desiredControlTransports.Add($spec.InterfaceName) | Out-Null
            }
            foreach ($spec in $singBoxSpecs) {
                $desiredControlTransports.Add($spec.InterfaceName) | Out-Null
            }
            foreach ($transport in @($agentConfig.transport_plan)) {
                Ensure-ControlTransport -Transport $transport
                $cycleInterfaceMaps.Add("$($transport.server_id)=$($transport.interface_name)") | Out-Null
            }
        }
        foreach ($spec in $directSpecs) {
            Ensure-AwgTransport -TunnelName $spec.TunnelName -ConfigPath $spec.ConfigPath
        }
        foreach ($spec in $vpnTypeSpecs) {
            Ensure-VpnTypeTransport -Provider $spec.Provider -Name $spec.InterfaceName -ConfigPath $spec.ConfigPath
        }
        foreach ($spec in $lokVpnSpecs) {
            Ensure-LokVpnTransport -Profile $spec.Profile -Name $spec.InterfaceName -ConfigPath $spec.ConfigPath
        }
        foreach ($spec in $singBoxSpecs) {
            Ensure-SingBoxTransport -Name $spec.InterfaceName -ConfigPath $spec.ConfigPath
        }
        if ($cycleInterfaceMaps.Count -eq 0) {
            throw "No server-to-interface mappings configured."
        }
        $result = Apply-PolicyRoutes -InterfaceMaps $cycleInterfaceMaps.ToArray()
        Write-AgentLine "routes applied: ip_routes=$($result.IpRoutes) domain_routes=$($result.DomainRoutes) commands=$($result.Commands) status_posted=$($result.StatusPosted)"
        $probeResult = Run-ProbeJobs -InterfaceMaps $cycleInterfaceMaps.ToArray()
        if ($probeResult.Jobs -gt 0) {
            Write-AgentLine "probe jobs processed: jobs=$($probeResult.Jobs) completed=$($probeResult.Completed) failed=$($probeResult.Failed)"
        }
        if (-not $NoControlTransportPlan) {
            Stop-UnusedSingBoxTransports -DesiredNames $desiredControlTransports.ToArray()
        }
    } catch {
        Write-AgentLine "agent cycle failed: $($_.Exception.Message)" -Level WARN
    }

    if ($Once) {
        break
    }

    $elapsed = [int]((Get-Date) - $startedAt).TotalSeconds
    $sleep = [Math]::Max(5, $PollSeconds - $elapsed)
    Start-Sleep -Seconds $sleep
} while ($true)
