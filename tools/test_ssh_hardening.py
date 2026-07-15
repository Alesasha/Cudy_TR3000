#!/usr/bin/env python3
"""Regression checks for control-server SSH hardening tooling."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "harden_control_ssh.py"


def assert_contains(text: str, needle: str, *, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} missing expected snippet: {needle}")


def assert_not_contains(text: str, needle: str, *, label: str) -> None:
    if needle in text:
        raise AssertionError(f"{label} contains forbidden snippet: {needle}")


def main() -> int:
    text = SCRIPT.read_text(encoding="utf-8")
    ast.parse(text)

    assert_contains(text, "cudy-sshd-watchdog.timer", label="harden_control_ssh.py")
    assert_contains(text, "cudy-sshd-watchdog.service", label="harden_control_ssh.py")
    assert_contains(text, "cudy-ssh-firewall-guard.service", label="harden_control_ssh.py")
    assert_contains(text, "CUDY-SSH-GUARD", label="harden_control_ssh.py")
    assert_contains(text, "--skip-firewall-guard", label="harden_control_ssh.py")
    assert_contains(text, "--connlimit-above", label="harden_control_ssh.py")
    assert_contains(text, "--hitcount", label="harden_control_ssh.py")
    assert_contains(text, 'CUDY_SSH_GUARD_HITCOUNT:-64', label="shared-NAT SSH guard")
    assert_contains(text, 'CUDY_SSH_GUARD_CONNLIMIT:-32', label="shared-NAT SSH guard")
    assert_contains(text, "OnUnitActiveSec={interval_seconds}s", label="harden_control_ssh.py")
    assert_contains(text, "--watchdog-stale-seconds", label="harden_control_ssh.py")
    assert_contains(text, "--watchdog-interval-seconds", label="harden_control_ssh.py")
    assert_contains(text, "--skip-watchdog", label="harden_control_ssh.py")
    assert_contains(text, "CUDY_SSHD_WATCHDOG_STALE_SECONDS", label="harden_control_ssh.py")
    assert_contains(text, "line ~ /\\\\[preauth\\\\]/", label="harden_control_ssh.py")
    assert_contains(text, "line ~ /\\\\[accepted\\\\]/", label="harden_control_ssh.py")
    assert_contains(text, "line ~ /sshd: unknown/", label="harden_control_ssh.py")
    assert_contains(text, "line ~ /sshd: invalid user/", label="harden_control_ssh.py")
    assert_contains(text, "force killing stale sshd preauth/banner", label="harden_control_ssh.py")
    assert_contains(text, "cudy-sshd-safe", label="harden_control_ssh.py")
    assert_contains(text, "Failed password for (?!({agent_user_regex})", label="harden_control_ssh.py")
    assert_contains(text, "cudy-tunnel-windows", label="harden_control_ssh.py")
    assert_contains(text, "cudy-tunnel-linux", label="harden_control_ssh.py")
    assert_contains(text, "--agent-user", label="harden_control_ssh.py")
    assert_contains(text, "Connection timed out during banner exchange", label="harden_control_ssh.py")
    assert_contains(text, "banaction = iptables-multiport", label="harden_control_ssh.py")
    assert_not_contains(text, "sshd: root [priv]", label="harden_control_ssh.py")
    assert_not_contains(text, "user@pts", label="harden_control_ssh.py")

    print("SSH hardening regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
