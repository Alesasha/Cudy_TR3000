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
    fresh_install = read_text(AGENT_DIR / "fresh_install_from_zip.sh")
    self_install_readme = read_text(AGENT_DIR / "SELF-INSTALL-README-RU.txt")
    installer = read_text(AGENT_DIR / "install_singbox_runtime.sh")
    managed = read_text(AGENT_DIR / "managed_agent.sh")
    start_tunnel = read_text(AGENT_DIR / "start_tunnel.sh")
    status = read_text(AGENT_DIR / "status.sh")
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
    assert_contains(one_click, "chmod +x ./runtime/sing-box", label="one_click_install.sh")
    assert_contains(fresh_install, 'find "$work_dir" -mindepth 1 -maxdepth 1 -type d', label="fresh_install_from_zip.sh")
    assert_contains(fresh_install, "sudo rm -rf --one-file-system", label="fresh_install_from_zip.sh")
    assert_contains(fresh_install, "sudo ./one_click_install.sh", label="fresh_install_from_zip.sh")
    assert_contains(fresh_install, "./test_prod_agent.sh", label="fresh_install_from_zip.sh")
    assert_contains(self_install_readme, "bash ./install.sh", label="SELF-INSTALL-README-RU.txt")
    assert_contains(installer, "from urllib.error import URLError", label="install_singbox_runtime.sh")
    assert_contains(installer, "cannot query GitHub release API", label="install_singbox_runtime.sh")
    assert_contains(start_tunnel, "-o BatchMode=yes", label="start_tunnel.sh")
    assert_contains(start_tunnel, "-o IdentitiesOnly=yes", label="start_tunnel.sh")
    assert_contains(start_tunnel, "-o StrictHostKeyChecking=accept-new", label="start_tunnel.sh")
    assert_contains(start_tunnel, "CONTROL_CONNECT_TIMEOUT", label="start_tunnel.sh")
    assert_contains(managed, "CONTROL_TUNNEL_WAIT_SECONDS", label="managed_agent.sh")
    assert_contains(managed, "stop_control_tunnel()", label="managed_agent.sh")
    assert_contains(managed, "dump_control_tunnel_logs()", label="managed_agent.sh")
    assert_contains(status, "control listeners/processes", label="status.sh")
    assert_contains(status, "ss -ltnp", label="status.sh")

    for label, script in {
        "managed_agent.sh": managed,
        "start_tunnel.sh": start_tunnel,
        "status.sh": status,
    }.items():
        assert_contains(script, "strip_cr()", label=label)
        assert_contains(script, "tr -d '\\r'", label=label)

    assert_contains(builder, "Copy-TextFileLf", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "New-ZipFromDirectoryUnix", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "$entryName = $relative -replace '\\\\', '/'", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "-replace \"`r`n\", \"`n\"", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "$AgentId-install.sh", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "$AgentId-self-install.sh", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "$AgentId-self-install.zip", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, '"install.sh"', label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "SELF-INSTALL-README-RU.txt", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "__CUDY_AGENT_ZIP_BASE64_BELOW__", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "production smoke test", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "unzip_rc=$?", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, 'if [ "$unzip_rc" -gt 1 ]; then', label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "IncludeRuntime", label="Build-LinuxAgentPackage.ps1")
    assert_contains(builder, "runtime", label="Build-LinuxAgentPackage.ps1")

    print("Linux agent packaging regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
