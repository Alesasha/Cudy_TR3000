#!/usr/bin/env python3
"""Install the serialized, fail-open PBR startup path on Cudy/OpenWrt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from deploy_cudy_go_fallback import ROOT, connect, load_password, ssh_exec, upload_via_cat


FILES = {
    ROOT / "openwrt" / "cudy-pbr-safe-restart": "/usr/bin/cudy-pbr-safe-restart",
    ROOT / "openwrt" / "cudy-pbr-watchdog": "/usr/bin/cudy-pbr-watchdog",
    ROOT / "openwrt" / "cudy-pbr-safe.init": "/etc/init.d/cudy-pbr-safe",
    ROOT / "openwrt" / "cudy-pbr-watchdog.init": "/etc/init.d/cudy-pbr-watchdog",
    ROOT / "openwrt" / "cudy-cidr-collapse": "/usr/bin/cudy-cidr-collapse",
    ROOT / "openwrt" / "pbr.user.opencck-merged-vpn": "/usr/share/pbr/pbr.user.opencck-merged-vpn",
    ROOT / "openwrt" / "cudy-pbr-fast-apply": "/usr/bin/cudy-pbr-fast-apply",
}


def deploy(args: argparse.Namespace) -> dict[str, Any]:
    for path in FILES:
        if not path.exists():
            raise FileNotFoundError(path)
    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "host": args.host,
            "start_pbr": args.start_pbr,
            "files": [str(path) for path in FILES],
        }

    client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
    try:
        for index, (source, _target) in enumerate(FILES.items()):
            upload_via_cat(client, source, f"/tmp/cudy-pbr-safety-{index}")
        start_command = ""
        if args.start_pbr:
            start_command = r"""
printf '\n== controlled PBR start ==\n'
if /usr/bin/cudy-pbr-safe-restart; then
  start_result=ok
else
  start_result=failed-open
fi
""".strip()
        installs = "\n".join(
            f"cp /tmp/cudy-pbr-safety-{index} {target}"
            for index, target in enumerate(FILES.values())
        )
        syntax_files = " ".join(f"/tmp/cudy-pbr-safety-{index}" for index in range(len(FILES)))
        command = f"""
set -u
for script in {syntax_files}; do
  ash -n "$script"
done
if [ -f /usr/share/pbr/pbr.user.opencck-merged-vpn ] && \
   ! cmp -s /tmp/cudy-pbr-safety-5 /usr/share/pbr/pbr.user.opencck-merged-vpn; then
  cp -p /usr/share/pbr/pbr.user.opencck-merged-vpn \
    /usr/share/pbr/pbr.user.opencck-merged-vpn.bak-cidr-collapse
fi
{installs}
chmod 0755 {' '.join(FILES.values())}

touch /etc/config/cudy-pbr-safety
if ! uci -q get cudy-pbr-safety.main >/dev/null; then
  uci set cudy-pbr-safety.main='safety'
fi
uci set cudy-pbr-safety.main.boot_delay='{args.boot_delay}'
uci set pbr.config.strict_enforcement='0'
uci set pbr.config.nft_set_auto_merge='0'
uci set pbr.config.procd_boot_trigger_delay='90000'
uci set pbr.config.procd_reload_delay='30'
uci commit cudy-pbr-safety
uci commit pbr

/etc/init.d/pbr disable 2>/dev/null || true
/etc/init.d/pbr stop >/tmp/cudy-pbr-deploy-stop.log 2>&1 || true
echo 1 > /proc/sys/net/ipv4/ip_forward
[ ! -e /proc/sys/net/ipv6/conf/all/forwarding ] || echo 1 > /proc/sys/net/ipv6/conf/all/forwarding

/etc/init.d/cudy-pbr-safe enable
/etc/init.d/cudy-pbr-watchdog enable
/etc/init.d/cudy-pbr-watchdog restart

cron_file=/tmp/cudy-pbr-root-cron
crontab -l > "$cron_file" 2>/dev/null || true
if grep -q '/etc/init.d/pbr restart' "$cron_file"; then
  sed 's#/etc/init.d/pbr restart#/usr/bin/cudy-pbr-safe-restart#g' "$cron_file" > "$cron_file.new"
  crontab "$cron_file.new"
fi
rm -f "$cron_file" "$cron_file.new"

{start_command}

printf 'watchdog='
/etc/init.d/cudy-pbr-watchdog status 2>/dev/null || true
printf '\npbr_enabled='
/etc/init.d/pbr enabled >/dev/null 2>&1 && printf yes || printf no
printf '\nsafe_enabled='
/etc/init.d/cudy-pbr-safe enabled >/dev/null 2>&1 && printf yes || printf no
printf '\nip_forward='
cat /proc/sys/net/ipv4/ip_forward
printf 'strict_enforcement='
uci -q get pbr.config.strict_enforcement
printf 'auto_merge='
uci -q get pbr.config.nft_set_auto_merge
printf 'reload_delay='
uci -q get pbr.config.procd_reload_delay
printf 'pbr_dataplane='
if ip -4 rule show 2>/dev/null | grep -Eq 'fwmark .* lookup pbr_' && \
   nft list chain inet fw4 pbr_prerouting 2>/dev/null | grep -q 'goto pbr_mark_'; then
  printf ready
else
  printf missing
fi
printf '\nfailed_state='
cat /var/run/cudy-pbr-safety/failed 2>/dev/null || printf none
printf '\nfw4_check='
fw4 check >/tmp/cudy-pbr-deploy-fw4.log 2>&1 && printf ok || printf failed
printf '\nwan_ping='
ping -c 2 -W 3 1.1.1.1 >/dev/null 2>&1 && printf ok || printf failed
printf '\nrecent_logs_begin\n'
logread -e cudy-pbr-safe -e cudy-pbr-watchdog -e pbr | tail -80
printf 'recent_logs_end\n'
""".strip()
        rc, output = ssh_exec(client, command, args.timeout)
    finally:
        client.close()

    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line and not line.startswith(" "):
            key, value = line.split("=", 1)
            if key in {
                "watchdog", "pbr_enabled", "safe_enabled", "ip_forward",
                "strict_enforcement", "reload_delay", "pbr_dataplane",
                "auto_merge", "failed_state", "fw4_check", "wan_ping",
            }:
                fields[key] = value
    ok = (
        rc == 0
        and fields.get("watchdog") == "running"
        and fields.get("pbr_enabled") == "no"
        and fields.get("safe_enabled") == "yes"
        and fields.get("ip_forward") == "1"
        and fields.get("strict_enforcement") == "0"
        and fields.get("auto_merge") == "0"
        and fields.get("fw4_check") == "ok"
        and fields.get("wan_ping") == "ok"
    )
    if args.start_pbr:
        ok = ok and fields.get("pbr_dataplane") == "ready" and fields.get("failed_state") == "none"
    return {"ok": ok, "host": args.host, "start_pbr": args.start_pbr, "fields": fields, "output": output}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=360)
    parser.add_argument("--boot-delay", type=int, default=45)
    parser.add_argument("--start-pbr", action="store_true", help="Run one controlled PBR start after installing safeguards.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    result = deploy(args)
    if result.get("dry_run") or args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Cudy PBR safety deploy: {'OK' if result.get('ok') else 'FAIL'} host={result.get('host')}")
        for key, value in (result.get("fields") or {}).items():
            print(f"  {key}={value}")
        if not result.get("ok"):
            print(result.get("output") or "")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
