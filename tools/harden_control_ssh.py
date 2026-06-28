#!/usr/bin/env python3
"""Harden public SSH on the control VPS against bot preauth floods."""

from __future__ import annotations

import argparse
import base64
import getpass
import os
import shlex
import sys
import textwrap
import time
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


def connect(host: str, user: str, password: str, timeout: int, *, attempts: int) -> paramiko.SSHClient:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
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
        except Exception as exc:
            last_error = exc
            client.close()
            if attempt < attempts:
                print(f"SSH connect attempt {attempt}/{attempts} failed: {exc}", file=sys.stderr)
                time.sleep(min(15, 2 * attempt))
    raise RuntimeError(f"SSH connect failed after {attempts} attempt(s): {last_error}")


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
    watchdog_body = textwrap.dedent(
        """\
        #!/usr/bin/env bash
        set -euo pipefail

        stale_seconds="${CUDY_SSHD_WATCHDOG_STALE_SECONDS:-120}"
        log_tag="cudy-sshd-watchdog"

        mapfile -t stale_rows < <(
          ps -eo pid=,etimes=,comm=,args= | awk -v stale="$stale_seconds" '
            $3 == "sshd" && $2 >= stale {
              line = $0
              if (line ~ /\\[preauth\\]/ || line ~ /\\[accepted\\]/ || line ~ /sshd: unknown/ || line ~ /sshd: invalid user/) {
                print $1 "\\t" $2 "\\t" substr(line, index(line, $4))
              }
            }
          '
        )

        [ "${#stale_rows[@]}" -gt 0 ] || exit 0

        for row in "${stale_rows[@]}"; do
          pid="${row%%$'\\t'*}"
          rest="${row#*$'\\t'}"
          age="${rest%%$'\\t'*}"
          cmd="${rest#*$'\\t'}"
          if kill -0 "$pid" 2>/dev/null; then
            logger -t "$log_tag" "terminating stale sshd preauth/banner pid=$pid age=${age}s cmd=$cmd"
            kill "$pid" 2>/dev/null || true
          fi
        done

        sleep 2

        for row in "${stale_rows[@]}"; do
          pid="${row%%$'\\t'*}"
          if kill -0 "$pid" 2>/dev/null; then
            logger -t "$log_tag" "force killing stale sshd preauth/banner pid=$pid"
            kill -9 "$pid" 2>/dev/null || true
          fi
        done
        """
    )
    watchdog_service = textwrap.dedent(
        """\
        [Unit]
        Description=Cudy SSH preauth/banner watchdog
        Documentation=man:sshd(8)

        [Service]
        Type=oneshot
        Environment=CUDY_SSHD_WATCHDOG_STALE_SECONDS={stale_seconds}
        ExecStart=/usr/local/sbin/cudy-sshd-watchdog
        """
    ).format(stale_seconds=int(args.watchdog_stale_seconds))
    watchdog_timer = textwrap.dedent(
        """\
        [Unit]
        Description=Run Cudy SSH preauth/banner watchdog periodically

        [Timer]
        OnBootSec=2min
        OnUnitActiveSec={interval_seconds}s
        AccuracySec=15s
        Persistent=true

        [Install]
        WantedBy=timers.target
        """
    ).format(interval_seconds=int(args.watchdog_interval_seconds))
    watchdog_body_b64 = base64.b64encode(watchdog_body.encode("utf-8")).decode("ascii")
    watchdog_service_b64 = base64.b64encode(watchdog_service.encode("utf-8")).decode("ascii")
    watchdog_timer_b64 = base64.b64encode(watchdog_timer.encode("utf-8")).decode("ascii")
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

        if [ "{int(not args.skip_watchdog)}" = "1" ]; then
          python3 - \
            /usr/local/sbin/cudy-sshd-watchdog {shlex.quote(watchdog_body_b64)} \
            /etc/systemd/system/cudy-sshd-watchdog.service {shlex.quote(watchdog_service_b64)} \
            /etc/systemd/system/cudy-sshd-watchdog.timer {shlex.quote(watchdog_timer_b64)} <<'PY'
        import base64
        import pathlib
        import sys

        pairs = sys.argv[1:]
        for path, payload in zip(pairs[0::2], pairs[1::2]):
            pathlib.Path(path).write_bytes(base64.b64decode(payload))
        PY
          chmod 0755 /usr/local/sbin/cudy-sshd-watchdog
          chmod 0644 /etc/systemd/system/cudy-sshd-watchdog.service /etc/systemd/system/cudy-sshd-watchdog.timer
          systemctl daemon-reload
          systemctl enable --now cudy-sshd-watchdog.timer
          systemctl start cudy-sshd-watchdog.service || true
        fi

        printf '== sshd effective ==\\n'
        sshd -T | grep -Ei '^(logingracetime|maxstartups|persourcemaxstartups|usedns|passwordauthentication|permitrootlogin)'
        printf '\\n== fail2ban ==\\n'
        fail2ban-client status sshd 2>/dev/null || true
        printf '\\n== cudy ssh watchdog ==\\n'
        systemctl --no-pager --full status cudy-sshd-watchdog.timer cudy-sshd-watchdog.service 2>/dev/null || true
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
    parser.add_argument("--connect-attempts", type=int, default=12)
    parser.add_argument("--login-grace-time", type=int, default=60)
    parser.add_argument("--per-source-max-startups", type=int, default=20)
    parser.add_argument("--max-startups", default="100:30:300")
    parser.add_argument("--ignore-ip", action="append", default=["195.170.35.108"])
    parser.add_argument("--skip-fail2ban", action="store_true")
    parser.add_argument("--fail2ban-maxretry", type=int, default=5)
    parser.add_argument("--fail2ban-findtime", default="10m")
    parser.add_argument("--fail2ban-bantime", default="1h")
    parser.add_argument("--skip-watchdog", action="store_true")
    parser.add_argument("--watchdog-stale-seconds", type=int, default=120)
    parser.add_argument("--watchdog-interval-seconds", type=int, default=60)
    args = parser.parse_args()

    password = ssh_password(args.ssh_password)
    client = connect(args.host, args.user, password, args.timeout, attempts=args.connect_attempts)
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
