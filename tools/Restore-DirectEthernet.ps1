param(
    [string]$ComputerAddress = "192.168.1.200",
    [string]$Gateway = "192.168.1.1"
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $PSCommandPath),
        "-ComputerAddress", $ComputerAddress,
        "-Gateway", $Gateway
    )
    Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
    exit
}

Set-NetIPInterface -InterfaceAlias "Ethernet" -AddressFamily IPv4 -Dhcp Disabled
Get-NetIPAddress -InterfaceAlias "Ethernet" -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
Get-NetRoute -InterfaceAlias "Ethernet" -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object DestinationPrefix -eq "0.0.0.0/0" |
    Remove-NetRoute -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress $ComputerAddress -PrefixLength 24 -DefaultGateway $Gateway | Out-Null
Set-DnsClientServerAddress -InterfaceAlias "Ethernet" -ServerAddresses @($Gateway, "1.1.1.1")
Write-Host "Direct Ethernet restored: $ComputerAddress via $Gateway"
ping.exe -n 2 $Gateway
curl.exe -4 --connect-timeout 5 --max-time 15 https://ifconfig.me/ip
Read-Host "Press Enter to close"
