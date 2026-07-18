#!/usr/bin/env python3
"""Static and artifact checks for universal desktop agent enrollment."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "tools" / "agent-windows"
LINUX = ROOT / "tools" / "agent-linux"
BUILD_SCRIPT = ROOT / "tools" / "Build-UniversalAgentPackages.ps1"
OUTPUT = ROOT / "build" / "universal-agents"
EXPECTED_HOST_FINGERPRINT = "SHA256:iyONyymHdd2Fwun5GIxKFo7eh4sooHpK1hdtLZOmGTM"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def require(source: str, *needles: str, label: str) -> None:
    for needle in needles:
        if needle not in source:
            raise AssertionError(f"{label} missing expected snippet: {needle}")


def host_fingerprint() -> str:
    parts = text(ROOT / "config" / "control_ssh_host_ed25519.pub").strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise AssertionError("invalid pinned control-server Ed25519 public key")
    digest = hashlib.sha256(base64.b64decode(parts[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def check_built_zip(platform: str, required: set[str]) -> None:
    path = OUTPUT / f"Cudy-Agent-{platform}-universal.zip"
    if not path.exists():
        return
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        missing = required - names
        if missing:
            raise AssertionError(f"{platform} universal zip missing: {sorted(missing)}")
        personalized = {
            "agent.env",
            "agent.env.ps1",
            "uswest_control_tunnel_ed25519",
            "uswest_control_tunnel_ed25519.pub",
        } & names
        if personalized:
            raise AssertionError(f"{platform} universal zip contains personalized files: {sorted(personalized)}")
        if len(archive.read("enrollment_bootstrap_ed25519")) < 100:
            raise AssertionError(f"{platform} universal zip has an invalid bootstrap key")


def main() -> int:
    if host_fingerprint() != EXPECTED_HOST_FINGERPRINT:
        raise AssertionError("pinned control-server key fingerprint changed")

    windows_enroll = text(WINDOWS / "Enroll-Agent.ps1")
    require(
        windows_enroll,
        'BootstrapUser = "cudy-enroll"',
        'BootstrapPort = 8766',
        '/api/agent/enroll',
        'platform = "windows"',
        'StrictHostKeyChecking=yes',
        'icacls $bootstrapKey /inheritance:r',
        'SSH details:',
        'agent.env.ps1',
        'uswest_control_tunnel_ed25519',
        label="Enroll-Agent.ps1",
    )
    windows_tunnel = text(WINDOWS / "Start-Tunnel.ps1")
    require(
        windows_tunnel,
        'StrictHostKeyChecking=$strictHostMode',
        'UserKnownHostsFile=$KnownHostsPath',
        'BatchMode=yes',
        'Confirm-ControlHostKey',
        'ExpectedHostKeySha256',
        label="Start-Tunnel.ps1",
    )

    linux_enroll = text(LINUX / "enroll_agent.sh")
    require(
        linux_enroll,
        'bootstrap_user="${CUDY_ENROLLMENT_USER:-cudy-enroll}"',
        'bootstrap_remote_port="${CUDY_ENROLLMENT_REMOTE_PORT:-8766}"',
        '/api/agent/enroll',
        '"platform": "linux"',
        'StrictHostKeyChecking=yes',
        'tail -20 "$tmp_dir/ssh.err"',
        'agent.env.tmp',
        'tmp.replace("agent.env")',
        label="enroll_agent.sh",
    )
    linux_tunnel = text(LINUX / "start_tunnel.sh")
    require(
        linux_tunnel,
        'STRICT_HOST_KEY_CHECKING=yes',
        'STRICT_HOST_KEY_CHECKING=accept-new',
        'StrictHostKeyChecking="$STRICT_HOST_KEY_CHECKING"',
        'prepare_known_host',
        'CONTROL_HOST_KEY_SHA256',
        label="start_tunnel.sh",
    )

    builder = text(BUILD_SCRIPT)
    require(
        builder,
        "android_enrollment_bootstrap_ed25519",
        "control_ssh_host_ed25519.pub",
        "Cudy-Agent-$Platform-universal.zip",
        label="Build-UniversalAgentPackages.ps1",
    )

    check_built_zip(
        "windows",
        {
            "Enroll-Agent.ps1",
            "Install-UniversalAgent.ps1",
            "enrollment_bootstrap_ed25519",
            "control_ssh_host_ed25519.pub",
            "agent.env.ps1.example",
        },
    )
    check_built_zip(
        "linux",
        {
            "enroll_agent.sh",
            "install_universal.sh",
            "enrollment_bootstrap_ed25519",
            "control_ssh_host_ed25519.pub",
            "agent.env.example",
        },
    )
    print("Universal desktop agent packaging regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
