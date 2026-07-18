import datetime as dt
import json
import tempfile
from pathlib import Path

from capture_cudy_preflight_snapshot import redact_output
from cudy_router_preflight import run_preflight
from test_cudy_router_preflight import write_snapshot


def test_redact_output_hides_sensitive_uci_assignments():
    raw = "\n".join(
        [
            "network.awg.private_key='private-value'",
            "network.peer.preshared_key='shared-value'",
            "wireless.default_radio0.key='wifi-value'",
            "service.main.token='token-value'",
            "network.wan.ipaddr='192.0.2.10'",
        ]
    )

    result = redact_output(raw)

    assert "private-value" not in result
    assert "shared-value" not in result
    assert "wifi-value" not in result
    assert "token-value" not in result
    assert "network.wan.ipaddr='192.0.2.10'" in result
    assert result.count("<redacted>") == 4


def test_preflight_reports_fresh_structured_snapshot(tmp_path):
    records = [
        {"inst": "wan_vlan-1", "key": "vlanid", "value": "2"},
        {"inst": "static-1", "key": "settings.ip", "value": "195.170.35.108"},
        {"inst": "static-0", "key": "settings.ip", "value": "192.168.1.1"},
        {"inst": "dhcps-0", "key": "startip", "value": "192.168.1.10"},
        {"inst": "dhcps-0", "key": "endip", "value": "192.168.1.249"},
    ]
    airties, cudy = write_snapshot(
        tmp_path,
        records,
        [],
        "network.wan.device='eth0'\nnetwork.wan.proto='dhcp'\nnetwork.lan.ipaddr='192.168.8.1'\nnetwork.awg.listen_port='51830'\n",
        "dhcp.lan.start='100'\ndhcp.lan.limit='150'\n",
        "firewall.awg=rule\nfirewall.awg.src='wan'\nfirewall.awg.dest_port='51830'\nfirewall.awg.target='ACCEPT'\n",
        "",
        "wireless.main=wifi-iface\nwireless.main.ssid='Cudy'\nwireless.main.encryption='sae-mixed'\n",
    )
    now = dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc)
    (cudy / "index.json").write_text(
        json.dumps({"generated_at": "2026-07-18T11:30:00+00:00"}),
        encoding="utf-8",
    )

    findings = run_preflight(airties, cudy, now=now)

    by_code = {finding.code: finding for finding in findings}
    assert by_code["cudy-snapshot-fresh"].severity == "PASS"
    assert by_code["wifi-ready"].severity == "PASS"


def main() -> int:
    test_redact_output_hides_sensitive_uci_assignments()
    with tempfile.TemporaryDirectory() as temp_dir:
        test_preflight_reports_fresh_structured_snapshot(Path(temp_dir))
    print("Cudy preflight snapshot regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
