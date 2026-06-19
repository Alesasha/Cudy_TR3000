# Windows Managed Transport PoC

Status: passed on 2026-06-14.

This PoC bypasses the AmneziaVPN UI and starts the AmneziaWG tunnel daemon
directly. The transport is started with only the `[Interface]` section and
`Table = off`; the peer is added afterward through the AmneziaWG UAPI named
pipe. This avoids the Amnezia UI full-tunnel/killswitch behavior while still
using the installed AmneziaWG transport.

## Files

- `tools/agent-windows/Start-AwgTransport.ps1`
- `tools/agent-windows/Send-AwgUapi.ps1`
- `tools/agent-windows/Stop-AwgTransport.ps1`
- `tools/agent-windows/Restore-Direct.ps1`
- `tools/agent-windows/Apply-Test-Routes.ps1`
- `tools/agent-windows/Start-SingBoxTransport.ps1`
- `tools/agent-windows/Stop-SingBoxTransport.ps1`

The working per-device package is under:

```text
secrets/agents/isasha_R7_Cudy-windows/
```

## Verified Behavior

Before applying policy routes:

```text
1.1.1.1       -> Ethernet
95.182.91.203 -> Ethernet
149.154.160.1 -> Ethernet
104.17.147.22 -> Ethernet
```

After applying cached test policy:

```text
1.1.1.1       -> Ethernet
95.182.91.203 -> Ethernet
149.154.160.1 -> AmneziaVPN
104.17.147.22 -> AmneziaVPN
```

The test policy contained:

```text
104.17.147.22/32 -> aktau
149.154.160.0/20 -> aktau
```

`curl.exe -4 --resolve www.speedtest.net:443:104.17.147.22 https://www.speedtest.net/ -o NUL`
increased `AmneziaVPN` counters:

```text
ReceivedBytes: 832 -> 156845
SentBytes:     54024 -> 61743
```

## Conclusion

The viable Windows architecture is:

```text
AmneziaWG transport daemon, without Amnezia UI
  +
agent-managed routes from control-server policy
```

Do not use Amnezia UI profiles with `AllowedIPs = 0.0.0.0/0`; even with
`Table = off`, the UI-managed path can install full-tunnel routes and trigger
Windows networking blocks.

## Provider Transports

Provider exits from VPNtype and LokVPN are started as sing-box TUN transports
when they appear in `transport_plan`.

The Windows wrapper uses deterministic TUN addresses derived from interface
name for generated `http-proxy-tun` and `vless-reality-tun` configs. This is
important: random TUN addresses make the config appear changed on every polling
cycle and can leave Windows with stale Wintun state such as:

```text
configure tun interface: set ipv4 address: The object already exists.
```

For `sing-box-json` configs coming from control-server, the address is already
part of the saved JSON config.
