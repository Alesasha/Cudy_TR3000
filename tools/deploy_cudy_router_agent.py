#!/usr/bin/env python3
"""Deploy cudy-router-agent to OpenWrt with an explicit persistent mode gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from deploy_cudy_go_fallback import ROOT, connect, load_password, ssh_exec, upload_via_cat


DEFAULT_BINARY = ROOT / "build" / "cudy" / "cudy-router-agent-linux-arm64"
DEFAULT_INIT = ROOT / "openwrt" / "cudy-router-agent.init"
DEFAULT_FAST_APPLY = ROOT / "openwrt" / "cudy-pbr-fast-apply"


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    binary = Path(args.binary).resolve()
    init = Path(args.init).resolve()
    fast_apply = Path(args.fast_apply).resolve()
    if not binary.exists():
        raise FileNotFoundError(binary)
    if not init.exists():
        raise FileNotFoundError(init)
    if not fast_apply.exists():
        raise FileNotFoundError(fast_apply)
    if args.dry_run:
        requested_mode = "apply" if args.enable_apply else "observe" if args.disable_apply else "preserve"
        return {"ok": True, "dry_run": True, "host": args.host, "mode": requested_mode}

    mode_command = ""
    if args.enable_apply:
        mode_command = "uci set cudy-router-agent.main.mode='apply'; uci set cudy-router-agent.main.allow_apply='1'"
    elif args.disable_apply:
        mode_command = "uci set cudy-router-agent.main.mode='observe'; uci set cudy-router-agent.main.allow_apply='0'"

    client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
    try:
        upload_via_cat(client, binary, "/tmp/cudy-router-agent")
        upload_via_cat(client, init, "/tmp/cudy-router-agent.init")
        upload_via_cat(client, fast_apply, "/tmp/cudy-pbr-fast-apply")
        rc, output = ssh_exec(
            client,
            f"""
set -eu
/etc/init.d/cudy-router-agent stop 2>/dev/null || true
cp /tmp/cudy-router-agent /usr/bin/cudy-router-agent
cp /tmp/cudy-router-agent.init /etc/init.d/cudy-router-agent
cp /tmp/cudy-pbr-fast-apply /usr/bin/cudy-pbr-fast-apply
chmod 0755 /usr/bin/cudy-router-agent /usr/bin/cudy-pbr-fast-apply /etc/init.d/cudy-router-agent
mkdir -p /var/lib/cudy-router-agent
chmod 0700 /var/lib/cudy-router-agent
touch /etc/config/cudy-router-agent
if ! uci -q get cudy-router-agent.main >/dev/null; then
  uci set cudy-router-agent.main='agent'
  uci set cudy-router-agent.main.mode='observe'
  uci set cudy-router-agent.main.allow_apply='0'
fi
{mode_command}
uci commit cudy-router-agent
rm -f /var/lib/cudy-router-agent/status.json
/etc/init.d/cudy-router-agent enable
/etc/init.d/cudy-router-agent start
i=0
while [ ! -s /var/lib/cudy-router-agent/status.json ] && [ "$i" -lt 90 ]; do
  sleep 1
  i=$((i + 1))
done
test -s /var/lib/cudy-router-agent/status.json
printf 'service='
/etc/init.d/cudy-router-agent status || true
printf '\nstatus='
tr -d '\n' < /var/lib/cudy-router-agent/status.json
printf '\n'
""".strip(),
            args.timeout,
        )
    finally:
        client.close()
    if rc != 0:
        return {"ok": False, "host": args.host, "error": output}
    service = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("service=")), "")
    status_line = next((line.split("=", 1)[1] for line in output.splitlines() if line.startswith("status=")), "{}")
    status = json.loads(status_line)
    return {
        "ok": service == "running" and status.get("mode") in {"observe", "apply"} and status.get("ok") is True,
        "host": args.host,
        "service": service,
        "status": status,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--binary", default=str(DEFAULT_BINARY))
    parser.add_argument("--init", default=str(DEFAULT_INIT))
    parser.add_argument("--fast-apply", default=str(DEFAULT_FAST_APPLY))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--enable-apply", action="store_true", help="Persistently enable apply mode with its safety gate.")
    mode.add_argument("--disable-apply", action="store_true", help="Persistently return the service to observe mode.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    result = deploy(build_parser().parse_args(argv))
    if result.get("dry_run"):
        print(f"Cudy router-agent deploy: DRY RUN mode={result.get('mode')}")
    else:
        print(f"Cudy router-agent deploy: {'OK' if result.get('ok') else 'FAIL'} host={result.get('host')}")
        if result.get("status"):
            status = result["status"]
            print(
                f"  service={result.get('service')} mode={status.get('mode')} "
                f"policy={status.get('policy_source')} routes={status.get('route_count')} "
                f"changed_files={status.get('changed_files')} blockers={bool(status.get('error'))}"
            )
        elif result.get("error"):
            print(f"  error={result['error']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
