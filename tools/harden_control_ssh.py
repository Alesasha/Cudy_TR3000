#!/usr/bin/env python3
"""Harden public SSH on the control VPS against bot preauth floods."""

from __future__ import annotations

import argparse
import getpass
import os
import shlex
import sys
import textwrap
from pathlib import Path

import paramiko


DEFAULT_HOST = "95.182.91.203"
DEFAULT_USER = "root"


def ssh_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("USWEST_SSH_PASSWORD", "CONTROL_BACKUP_SSH_PASSWORD", "AWG_SSH_PASSWORD_HOSTVDS_USWEST"):
        value = os.environ.get(name)
        if value:
            return value
    return getpass.getpass("SSH password for control VPS: ")


def connect(host: str, user: str, password: str, timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        username=user,
        password=password,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> str:
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return out + err


def remote_script(args: argparse.Namespace) -> str:
    ignore_ips = " ".join(args.ignore_ip)
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        stamp="$(date -u +%Y%m%d-%H%M%S)"
        cp -a /etc/ssh/sshd_config "/root/sshd_config.backup.${{stamp}}"
        mkdir -p /etc/ssh/sshd_config.d
        cat >/etc/ssh/sshd_config.d/99-cudy-anti-bruteforce.conf <<'EOF'
        # Managed by Cudy_TR3000 harden_control_ssh.py.
        # Keep public SSH usable for roaming agents, but reduce pre-auth stalls from bot floods.
        LoginGraceTime {int(args.login_grace_time)}
        PerSourceMaxStartups {int(args.per_source_max_startups)}
        MaxStartups {shlex.quote(args.max_startups)}
        UseDNS no
        EOF
        if [ -f /etc/ssh/sshd_config.d/98-cudy-stability.conf ]; then
          cp -a /etc/ssh/sshd_config.d/98-cudy-stability.conf "/root/98-cudy-stability.conf.backup.${{stamp}}"
          sed -i 's/^LoginGraceTime .*/LoginGraceTime {int(args.login_grace_time)}/' /etc/ssh/sshd_config.d/98-cudy-stability.conf || true
        fi
        sshd -t
        systemctl reload ssh

        if [ "{int(not args.skip_fail2ban)}" = "1" ]; then
          if ! command -v fail2ban-client >/dev/null 2>&1; then
            apt-get update -y
            apt-get install -y fail2ban
          fi
          mkdir -p /etc/fail2ban/jail.d
          cat >/etc/fail2ban/jail.d/cudy-sshd.conf <<'EOF'
        [sshd]
        enabled = true
        port = ssh
        filter = sshd
        backend = systemd
        maxretry = {int(args.fail2ban_maxretry)}
        findtime = {shlex.quote(args.fail2ban_findtime)}
        bantime = {shlex.quote(args.fail2ban_bantime)}
        ignoreip = 127.0.0.1/8 ::1 {ignore_ips}
        EOF
          systemctl enable fail2ban
          systemctl restart fail2ban
        fi

        printf '== sshd effective ==\\n'
        sshd -T | grep -Ei '^(logingracetime|maxstartups|persourcemaxstartups|usedns|passwordauthentication|permitrootlogin)'
        printf '\\n== fail2ban ==\\n'
        fail2ban-client status sshd 2>/dev/null || true
        printf '\\n== top SSH source IPs, last 6h ==\\n'
        journalctl -u ssh -S '6 hours ago' --no-pager 2>/dev/null \\
          | grep -E 'Failed password|Invalid user|Timeout before authentication|Connection reset by|Connection closed by' \\
          | grep -Eo 'from ([0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}|rhost=([0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}|for ([0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}' \\
          | sed -E 's/^(from|rhost=|for) //' \\
          | sort | uniq -c | sort -nr | head -30 || true
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password", default="")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--login-grace-time", type=int, default=15)
    parser.add_argument("--per-source-max-startups", type=int, default=20)
    parser.add_argument("--max-startups", default="100:30:300")
    parser.add_argument("--ignore-ip", action="append", default=["195.170.35.108"])
    parser.add_argument("--skip-fail2ban", action="store_true")
    parser.add_argument("--fail2ban-maxretry", type=int, default=5)
    parser.add_argument("--fail2ban-findtime", default="10m")
    parser.add_argument("--fail2ban-bantime", default="1h")
    args = parser.parse_args()

    password = ssh_password(args.ssh_password)
    client = connect(args.host, args.user, password, args.timeout)
    try:
        sftp = client.open_sftp()
        remote = "/root/cudy-harden-control-ssh.sh"
        with sftp.file(remote, "w") as handle:
            handle.write(remote_script(args))
        sftp.chmod(remote, 0o700)
        sftp.close()
        print(ssh_exec(client, remote, timeout=max(args.timeout, 240)), end="")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
