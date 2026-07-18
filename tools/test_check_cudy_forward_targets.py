#!/usr/bin/env python3

import json
import tempfile
from pathlib import Path

from check_cudy_forward_targets import collect_targets, tcp_ports


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-forward-target-test-") as temp_dir:
        snapshot = Path(temp_dir)
        (snapshot / "records.json").write_text(
            json.dumps(
                [
                    {"inst": "static-0", "key": "settings.ip", "value": "192.168.1.1"},
                    {"inst": "dhcps-0", "key": "ip0", "value": "192.168.1.10"},
                    {"inst": "dhcps-0", "key": "mac0", "value": "AA:BB:CC:DD:EE:FF"},
                    {"inst": "dhcps-0", "key": "hostname0", "value": "reserved"},
                ]
            ),
            encoding="utf-8",
        )
        (snapshot / "nat_port_forwarding_table.json").write_text(
            json.dumps(
                [
                    {"active": "1", "name": "one", "client_ip": "192.168.1.10", "tcp_wan": "80", "tcp_lan": "80"},
                    {"active": "1", "name": "two", "client_ip": "192.168.1.10", "udp_wan": "81", "udp_lan": "81"},
                    {"active": "1", "name": "self", "client_ip": "192.168.1.1", "tcp_wan": "88", "tcp_lan": "88"},
                    {"active": "1", "name": "Cudy", "client_ip": "192.168.1.174", "udp_wan": "51830", "udp_lan": "51830"},
                    {"active": "0", "name": "off", "client_ip": "192.168.1.20", "tcp_wan": "90", "tcp_lan": "90"},
                ]
            ),
            encoding="utf-8",
        )
        targets = collect_targets(snapshot)
        assert len(targets) == 1, targets
        assert targets[0]["target"] == "192.168.1.10"
        assert targets[0]["reservation"]["name"] == "reserved"
        assert len(targets[0]["forwards"]) == 2
        assert tcp_ports(targets[0]) == [80]
    print("Cudy forward target parsing regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
