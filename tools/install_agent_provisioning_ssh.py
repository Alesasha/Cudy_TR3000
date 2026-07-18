#!/usr/bin/env python3
"""Install the restricted SSH account used by per-device provisioning keys."""

from __future__ import annotations

import argparse
import os
import pwd
import subprocess
from pathlib import Path


DEFAULT_USER = "cudy-tunnel-agent"
DEFAULT_BOOTSTRAP_USER = "cudy-enroll"
DEFAULT_SERVICE_USER = "cudy-control"
DEFAULT_KEYS = Path("/opt/cudy-control/data/agent_authorized_keys")
DEFAULT_BOOTSTRAP_KEY = Path("/opt/cudy-control/config/android_enrollment_bootstrap.pub")
HELPER = Path("/usr/local/sbin/cudy-agent-authorized-keys")
SSHD_DROPIN = Path("/etc/ssh/sshd_config.d/65-cudy-agent-provisioning.conf")


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def write_root_file(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.chown(temporary, 0, 0)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def ensure_tunnel_user(name: str) -> None:
    try:
        pwd.getpwnam(name)
    except KeyError:
        run(
            "useradd",
            "--system",
            "--no-create-home",
            "--home-dir",
            f"/var/empty/{name}",
            "--shell",
            "/usr/sbin/nologin",
            name,
        )


def install(
    *,
    tunnel_user: str,
    bootstrap_user: str,
    service_user: str,
    keys_path: Path,
    bootstrap_key_path: Path,
) -> None:
    if os.geteuid() != 0:
        raise PermissionError("Run this installer as root")
    try:
        pwd.getpwnam(service_user)
    except KeyError as exc:
        raise RuntimeError(f"Missing service user: {service_user}") from exc
    ensure_tunnel_user(tunnel_user)
    ensure_tunnel_user(bootstrap_user)
    if not bootstrap_key_path.is_file() or not bootstrap_key_path.read_text(encoding="ascii").strip():
        raise RuntimeError(f"Missing Android enrollment bootstrap public key: {bootstrap_key_path}")

    service = pwd.getpwnam(service_user)
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    keys_path.touch(exist_ok=True)
    os.chown(keys_path, service.pw_uid, service.pw_gid)
    os.chmod(keys_path, 0o644)

    write_root_file(
        HELPER,
        "#!/bin/sh\n"
        "set -eu\n"
        "case \"${1:-}\" in\n"
        f"  {tunnel_user!r}) exec /bin/cat {str(keys_path)!r} ;;\n"
        f"  {bootstrap_user!r}) exec /bin/cat {str(bootstrap_key_path)!r} ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        0o755,
    )
    write_root_file(
        SSHD_DROPIN,
        f"""# Managed by install_agent_provisioning_ssh.py
Match User {tunnel_user}
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
    AuthorizedKeysFile none
    AuthorizedKeysCommand {HELPER} %u
    AuthorizedKeysCommandUser {service_user}
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8765
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
    MaxSessions 0

Match User {bootstrap_user}
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PubkeyAuthentication yes
    AuthorizedKeysFile none
    AuthorizedKeysCommand {HELPER} %u
    AuthorizedKeysCommandUser {service_user}
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8766
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitTTY no
    MaxSessions 0

Match all
""",
        0o644,
    )
    run("/usr/sbin/sshd", "-t")
    if subprocess.run(["systemctl", "reload", "ssh"], check=False).returncode != 0:
        run("systemctl", "reload", "sshd")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tunnel-user", default=DEFAULT_USER)
    parser.add_argument("--bootstrap-user", default=DEFAULT_BOOTSTRAP_USER)
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--keys-path", type=Path, default=DEFAULT_KEYS)
    parser.add_argument("--bootstrap-key-path", type=Path, default=DEFAULT_BOOTSTRAP_KEY)
    args = parser.parse_args()
    install(
        tunnel_user=args.tunnel_user,
        bootstrap_user=args.bootstrap_user,
        service_user=args.service_user,
        keys_path=args.keys_path,
        bootstrap_key_path=args.bootstrap_key_path,
    )
    print(
        f"Provisioning SSH installed: user={args.tunnel_user} keys={args.keys_path} "
        f"bootstrap_user={args.bootstrap_user} bootstrap_key={args.bootstrap_key_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
