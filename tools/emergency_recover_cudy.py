#!/usr/bin/env python3
"""Put Cudy routing into a direct, non-managed recovery state over its LAN."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from deploy_cudy_go_fallback import connect, load_password, ssh_exec


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"


RECOVERY_COMMAND = r"""
set -u

echo '== before =='
uci -q show cudy-router-agent || true
/etc/init.d/cudy-router-agent status 2>/dev/null || true
/etc/init.d/pbr status 2>/dev/null || true
ip -4 route show
ip -4 rule show

echo '== force safe mode =='
touch /etc/config/cudy-router-agent
if ! uci -q get cudy-router-agent.main >/dev/null; then
  uci set cudy-router-agent.main='agent'
fi
uci set cudy-router-agent.main.mode='observe'
uci set cudy-router-agent.main.allow_apply='0'
uci commit cudy-router-agent
/etc/init.d/cudy-router-agent stop 2>/dev/null || true
/etc/init.d/cudy-router-agent disable 2>/dev/null || true

echo '== stop PBR =='
/etc/init.d/cudy-pbr-safe stop 2>/dev/null || true
/etc/init.d/cudy-pbr-safe disable 2>/dev/null || true
/etc/init.d/pbr stop 2>/dev/null || true
echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
sleep 3

echo '== first WAN probe =='
if ping -c 2 -W 3 1.1.1.1; then
  wan_ok=1
else
  wan_ok=0
fi

if [ "$wan_ok" -ne 1 ]; then
  echo '== restart WAN =='
  ifdown wan 2>/dev/null || ubus call network.interface.wan down 2>/dev/null || true
  sleep 3
  ifup wan 2>/dev/null || ubus call network.interface.wan up 2>/dev/null || true
  sleep 15
fi

echo '== after =='
uci -q show cudy-router-agent || true
/etc/init.d/cudy-router-agent status 2>/dev/null || true
/etc/init.d/pbr status 2>/dev/null || true
ubus call network.interface.wan status 2>/dev/null || true
ip -4 route show
ip -4 rule show
ping -c 3 -W 3 1.1.1.1 || true
nslookup ifconfig.me 1.1.1.1 2>&1 || true
wget -4 -qO- -T 10 https://ifconfig.me/ip 2>&1 || true
echo
echo '== recent logs =='
logread -e cudy-router-agent -e cudy-pbr-fast -e pbr | tail -120
""".strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"cudy-emergency-recovery-{stamp}.log"

    try:
        client = connect(args.host, args.user, load_password(args.ssh_password), 20)
        try:
            rc, output = ssh_exec(client, RECOVERY_COMMAND, args.timeout)
        finally:
            client.close()
    except Exception as exc:  # Always leave a local reason when remote recovery is unavailable.
        rc = 1
        output = f"ERROR: {type(exc).__name__}: {exc}"

    log_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    print(f"\nRecovery log: {log_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
