#!/usr/bin/env python3
"""Install the restricted SSH account used by per-device provisioning keys."""

from __future__ import annotations

import argparse
import os
import pwd
import subprocess
from pathlib import Path


DEFAULT_USER = "cudy-tunnel-agent"
DEFAULT_SERVICE_USER = "cudy-control"
DEFAULT_KEYS = Path("/opt/cudy-control/data/agent_authorized_keys")
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


def install(*, tunnel_user: str, service_user: str, keys_path: Path) -> None:
    if os.geteuid() != 0:
        raise PermissionError("Run this installer as root")
    try:
        pwd.getpwnam(service_user)
    except KeyError as exc:
        raise RuntimeError(f"Missing service user: {service_user}") from exc
    try:
        pwd.getpwnam(tunnel_user)
    except KeyError:
        run(
            "useradd",
            "--system",
            "--no-create-home",
            "--home-dir",
            f"/var/empty/{tunnel_user}",
            "--shell",
            "/usr/sbin/nologin",
            tunnel_user,
        )

    service = pwd.getpwnam(service_user)
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    keys_path.touch(exist_ok=True)
    os.chown(keys_path, service.pw_uid, service.pw_gid)
    os.chmod(keys_path, 0o644)

    write_root_file(
        HELPER,
        "#!/bin/sh\n"
        "set -eu\n"
        f"[ \"${{1:-}}\" = {tunnel_user!r} ] || exit 0\n"
        f"exec /bin/cat {str(keys_path)!r}\n",
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
    parser.add_argument("--service-user", default=DEFAULT_SERVICE_USER)
    parser.add_argument("--keys-path", type=Path, default=DEFAULT_KEYS)
    args = parser.parse_args()
    install(tunnel_user=args.tunnel_user, service_user=args.service_user, keys_path=args.keys_path)
    print(f"Provisioning SSH installed: user={args.tunnel_user} keys={args.keys_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
