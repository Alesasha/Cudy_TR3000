#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path


def tun_address(name: str, base: int) -> str:
    value = hashlib.sha256(name.encode("utf-8")).digest()[0]
    octet = 2 + (value % 230)
    return f"172.{base}.{octet}.1/30"


def http_proxy_config(item: dict) -> dict:
    iface = item["interface_name"]
    cfg = item.get("config") or {}
    host = str(cfg["server"])
    port = int(cfg["server_port"])
    proxy_type = str(cfg.get("proxy_type") or "http")
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "tun",
                "tag": f"{iface}-tun",
                "interface_name": iface,
                "address": [tun_address(iface, 41)],
                "mtu": 1400,
                "auto_route": False,
                "strict_route": False,
                "stack": "gvisor",
            }
        ],
        "outbounds": [
            {"type": proxy_type, "tag": "proxy-out", "server": host, "server_port": port},
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "auto_detect_interface": True,
            "rules": [{"ip_cidr": [f"{host}/32"], "outbound": "direct"}],
            "final": "proxy-out",
        },
    }


def vless_reality_config(item: dict) -> dict:
    iface = item["interface_name"]
    cfg = item.get("config") or {}
    tls = cfg.get("tls") or {}
    reality = tls.get("reality") or {}
    host = str(cfg["server"])
    outbound = {
        "type": "vless",
        "tag": "proxy-out",
        "server": host,
        "server_port": int(cfg["server_port"]),
        "uuid": str(cfg["uuid"]),
        "tls": {
            "enabled": True,
            "server_name": str(tls["server_name"]),
            "utls": {"enabled": True, "fingerprint": "chrome"},
            "reality": {
                "enabled": True,
                "public_key": str(reality["public_key"]),
                "short_id": str(reality.get("short_id") or ""),
            },
        },
    }
    flow = str(cfg.get("flow") or "")
    if flow:
        outbound["flow"] = flow
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "tun",
                "tag": f"{iface}-tun",
                "interface_name": iface,
                "address": [tun_address(iface, 43)],
                "mtu": 1400,
                "auto_route": False,
                "strict_route": False,
                "stack": "gvisor",
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "auto_detect_interface": True,
            "rules": [{"ip_cidr": [f"{host}/32"], "outbound": "direct"}],
            "final": "proxy-out",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config_json")
    parser.add_argument("--output-dir", default="transports")
    args = parser.parse_args()

    config = json.loads(Path(args.config_json).read_text(encoding="utf-8"))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in config.get("transport_plan") or []:
        server_id = str(item.get("server_id") or "")
        iface = str(item.get("interface_name") or "")
        transport_type = str(item.get("transport_type") or "")
        if not server_id or not iface:
            continue
        if transport_type == "http-proxy-tun":
            singbox = http_proxy_config(item)
        elif transport_type == "vless-reality-tun":
            singbox = vless_reality_config(item)
        elif transport_type == "sing-box-json":
            singbox = item.get("config") or {}
        else:
            raise SystemExit(f"unsupported transport_type for {server_id}: {transport_type}")
        path = out_dir / f"{iface}.json"
        path.write_text(json.dumps(singbox, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append({"server_id": server_id, "interface_name": iface, "config_path": str(path)})
    print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
