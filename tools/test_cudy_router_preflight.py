import json
import tempfile
from pathlib import Path

from cudy_router_preflight import run_preflight


def write_snapshot(path, records, nat_rows, cudy_network, cudy_dhcp, cudy_firewall, ip_route, cudy_wireless=""):
    airties = path / "airties"
    cudy = path / "cudy"
    airties.mkdir()
    cudy.mkdir()
    (airties / "records.json").write_text(json.dumps(records), encoding="utf-8")
    (airties / "nat_port_forwarding_table.json").write_text(json.dumps(nat_rows), encoding="utf-8")
    (cudy / "uci_network.txt").write_text(cudy_network, encoding="utf-8")
    (cudy / "uci_dhcp.txt").write_text(cudy_dhcp, encoding="utf-8")
    (cudy / "uci_firewall.txt").write_text(cudy_firewall, encoding="utf-8")
    (cudy / "uci_wireless.txt").write_text(cudy_wireless, encoding="utf-8")
    (cudy / "ip_route.txt").write_text(ip_route, encoding="utf-8")
    return airties, cudy


def test_preflight_reports_expected_cutover_risks(tmp_path):
    records = [
        {"inst": "wan_vlan-1", "key": "vlanid", "value": "2"},
        {"inst": "static-1", "key": "settings.ip", "value": "195.170.35.108"},
        {"inst": "static-0", "key": "settings.ip", "value": "192.168.1.1"},
        {"inst": "dhcps-0", "key": "startip", "value": "192.168.1.10"},
        {"inst": "dhcps-0", "key": "endip", "value": "192.168.1.249"},
        {"inst": "dhcps-0", "key": "mac0", "value": "aa:bb:cc:dd:ee:ff"},
        {"inst": "dhcps-0", "key": "ip0", "value": "192.168.1.20"},
        {"inst": "dhcps-0", "key": "hostname0", "value": "device-one"},
        {"inst": "dhcps-0", "key": "mac1", "value": "d4:0d:ab:52:82:ff"},
        {"inst": "dhcps-0", "key": "ip1", "value": "192.168.1.174"},
        {"inst": "dhcps-0", "key": "hostname1", "value": "OpenWrt"},
        {"inst": "security-0", "key": "REM.enabled", "value": "1"},
        {"inst": "upnp-0", "key": "enablePF", "value": "1"},
        {"inst": "tr069-0", "key": "tr069.enable", "value": "1"},
    ]
    nat_rows = [
        {"active": "1", "name": "Air4452RU", "client_ip": "192.168.1.1", "tcp_wan": "8000", "udp_wan": ""},
        {"active": "1", "name": "Cudy", "client_ip": "192.168.1.174", "tcp_wan": "", "udp_wan": "51830"},
        {"active": "1", "name": "Unreserved", "client_ip": "192.168.1.55", "tcp_wan": "9000", "udp_wan": ""},
    ]
    airties, cudy = write_snapshot(
        tmp_path,
        records,
        nat_rows,
        "\n".join(
            [
                "network.wan=interface",
                "network.wan.device='eth0'",
                "network.wan.proto='dhcp'",
                "network.lan.ipaddr='192.168.8.1'",
                "network.awg_in.listen_port='51830'",
            ]
        ),
        "dhcp.lan.start='100'\ndhcp.lan.limit='150'\n",
        "\n".join(
            [
                "firewall.@rule[11]=rule",
                "firewall.@rule[11].src='wan'",
                "firewall.@rule[11].dest_port='51830'",
                "firewall.@rule[11].target='ACCEPT'",
            ]
        ),
        "45.136.59.135 via 192.168.1.1 dev eth0 proto static\n",
        "wireless.default_radio0=wifi-iface\nwireless.default_radio0.ssid='OpenWrt'\nwireless.default_radio0.encryption='none'\nwireless.default_radio0.disabled='1'\n",
    )

    findings = run_preflight(airties, cudy)
    by_code = {finding.code: finding for finding in findings}

    assert by_code["wan-vlan-review"].severity == "WARN"
    assert by_code["lan-ip-cutover"].severity == "WARN"
    assert by_code["awg-listen-port"].severity == "PASS"
    assert by_code["awg-wan-allow"].severity == "PASS"
    assert by_code["airties-self-forwards"].severity == "INFO"
    assert by_code["cudy-old-forward"].severity == "INFO"
    assert by_code["old-cudy-reservation"].severity == "INFO"
    assert by_code["host-routes-via-old-airties"].severity == "WARN"
    assert by_code["forward-targets-without-reservations"].severity == "WARN"
    assert by_code["wifi-disabled"].severity == "WARN"


def main() -> int:
    with tempfile.TemporaryDirectory() as temp_dir:
        test_preflight_reports_expected_cutover_risks(Path(temp_dir))
    print("Cudy router preflight regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
