from generate_cudy_router_migration import generate_commands, record_map


def test_generate_commands_keep_guardrails_and_skip_self_forwards():
    records = [
        {"inst": "static-1", "key": "settings.ip", "value": "195.170.35.108"},
        {"inst": "static-1", "key": "settings.mask", "value": "255.255.255.0"},
        {"inst": "static-1", "key": "settings.gateway", "value": "195.170.35.1"},
        {"inst": "static-1", "key": "settings.dns1", "value": "195.170.55.1"},
        {"inst": "static-1", "key": "settings.dns2", "value": "195.170.32.18"},
        {"inst": "wan_vlan-1", "key": "vlanid", "value": "2"},
        {"inst": "static-0", "key": "settings.ip", "value": "192.168.1.1"},
        {"inst": "static-0", "key": "settings.mask", "value": "255.255.255.0"},
        {"inst": "dhcps-0", "key": "startip", "value": "192.168.1.10"},
        {"inst": "dhcps-0", "key": "endip", "value": "192.168.1.249"},
        {"inst": "dhcps-0", "key": "leasetime", "value": "3600"},
        {"inst": "dhcps-0", "key": "mac0", "value": "aa:bb:cc:dd:ee:ff"},
        {"inst": "dhcps-0", "key": "ip0", "value": "192.168.1.20"},
        {"inst": "dhcps-0", "key": "hostname0", "value": "device-one"},
        {"inst": "dhcps-0", "key": "mac1", "value": "d4:0d:ab:52:82:ff"},
        {"inst": "dhcps-0", "key": "ip1", "value": "192.168.1.174"},
        {"inst": "dhcps-0", "key": "hostname1", "value": "OpenWrt"},
    ]
    nat_rows = [
        {
            "id": "1",
            "active": "1",
            "name": "Air4452RU",
            "client_ip": "192.168.1.1",
            "tcp_wan": "8000",
            "tcp_lan": "80",
            "udp_wan": "",
            "udp_lan": "",
        },
        {
            "id": "2",
            "active": "1",
            "name": "Cudy",
            "client_ip": "192.168.1.174",
            "tcp_wan": "",
            "tcp_lan": "",
            "udp_wan": "51830",
            "udp_lan": "51830",
        },
        {
            "id": "3",
            "active": "1",
            "name": "Home_Assistant",
            "client_ip": "192.168.1.252",
            "tcp_wan": "8123",
            "tcp_lan": "8123",
            "udp_wan": "",
            "udp_lan": "",
        },
    ]

    commands = "\n".join(
        generate_commands(record_map(records), nat_rows, include_disabled_forwards=False)
    )

    assert "exit 1" in commands
    assert "Review VLAN first" in commands
    assert "uci set dhcp.lan.limit='240'" in commands
    assert "skipped old AirTies-side reservation for Cudy/OpenWrt WAN" in commands
    assert "skipped AirTies router self-forward 1" in commands
    assert "skipped AirTies self-forward for Cudy UDP 51830" in commands
    assert "firewall.airties_redirect_3_home_assistant" in commands
