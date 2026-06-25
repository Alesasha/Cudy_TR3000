#!/usr/bin/env python3
"""Regression checks for Linux agent install/packaging scripts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "tools" / "agent-linux"
BUILD_SCRIPT = ROOT / "tools" / "Build-LinuxAgentPackage.ps1"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def assert_contains(text: str, needle: str, *, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} missing expected snippet: {needle}")


def assert_not_contains(text: str, needle: str, *, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} still contains forbidden snippet: {needle}")


def main() -> int:
    restore = read_text(AGENT_DIR / "restore_direct.sh")
    one_click = read_text(AGENT_DIR / "one_click_install.sh")
    installer = read_text(AGENT_DIR / "install_singbox_runtime.sh")
    builder = read_text(BUILD_SCRIPT)

    assert_contains(
        restore,
        'dns_value="${RESTORE_DNS_SERVERS:-${gw:-192.168.1.1} 1.1.1.1}"',
        label="restore_direct.sh",
    )
    assert_contains(restore, 'read -r -a dns_servers <<< "$dns_value"', label="restore_direct.sh")
    assert_contains(restore, 'resolvectl dns "$dev" "${dns_servers[@]}"', label="restore_direct.sh")
    assert_contains(restore, 'resolvectl default-route "$dev" yes', label="restore_direct.sh")
    assert_not_contains(
        restore,
        'resolvectl dns "$dev" "${RESTORE_DNS_SERVERS:-192.168.1.254 1.1.1.1}"',
        label="restore_direct.sh",
    )

    assert_contains(one_click, 'socket.getaddrinfo("api.github.com", 443)', label="one_click_install.sh")
    assert_contains(one_click, "DNS cannot resolve api.github.com", label="one_click_install.sh")
    assert_contains(installer, "from urllib.error import URLError", label="install_singbox_runtime.sh")
    assert_contains(installer, "cannot query GitHub release API", label="install_singbox_runtime.sh")

    assert_contains(builder, "IncludeRuntime", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "runtime", label="Build-LinuxAgentPackage.ps1")

    print("Linux agent packaging regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
