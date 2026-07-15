#!/usr/bin/env python3
"""Check the deployed Cudy router-agent observer without changing routing."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from deploy_cudy_go_fallback import connect, load_password, ssh_exec


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-age-seconds", type=int, default=300)
    parser.add_argument("--expected-mode", choices=("either", "observe", "apply"), default="either")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
    try:
        rc, output = ssh_exec(
            client,
            """
set -eu
printf 'service='
/etc/init.d/cudy-router-agent status || true
printf '\nstatus='
tr -d '\n' < /var/lib/cudy-router-agent/status.json
printf '\ndesired='
tr -d '\n' < /var/lib/cudy-router-agent/desired.json
printf '\n'
""".strip(),
            args.timeout,
        )
    finally:
        client.close()
    if rc != 0:
        print(f"Cudy router-agent: FAIL {output}")
        return 1
    lines = output.splitlines()
    service = next((line.split("=", 1)[1] for line in lines if line.startswith("service=")), "")
    status = json.loads(next(line.split("=", 1)[1] for line in lines if line.startswith("status=")))
    desired = json.loads(next(line.split("=", 1)[1] for line in lines if line.startswith("desired=")))
    updated_at = parse_timestamp(status.get("updated_at"))
    age_seconds = (
        max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))
        if updated_at
        else None
    )
    blockers = desired.get("blockers") or []
    actions = desired.get("transport_actions") or []
    critical_count = int(status.get("critical_service_count") or 0)
    critical_failures = status.get("critical_service_failures") or []
    mode_ok = status.get("mode") in {"observe", "apply"}
    if args.expected_mode != "either":
        mode_ok = status.get("mode") == args.expected_mode
    healthy = all(
        (
            service == "running",
            mode_ok,
            status.get("ok") is True,
            status.get("policy_source") in {"live", "cache"},
            bool(desired.get("groups")),
            not blockers,
            critical_count > 0,
            status.get("critical_services_ok") is True,
            age_seconds is not None and age_seconds <= args.max_age_seconds,
        )
    )
    print(f"Cudy router-agent: {'OK' if healthy else 'FAIL'} host={args.host}")
    print(
        f"  service={service} mode={status.get('mode')} source={status.get('policy_source')} "
        f"routes={status.get('route_count')} changed_files={status.get('changed_files')} "
        f"critical={critical_count} critical_ok={status.get('critical_services_ok')} "
        f"age={age_seconds if age_seconds is not None else 'invalid'}s"
    )
    print(
        f"  blockers={len(blockers)} warnings={len(desired.get('warnings') or [])} "
        f"transport_actions={len(actions)}"
    )
    for blocker in blockers[:10]:
        print(f"    - {blocker}")
    for failure in critical_failures[:10]:
        print(f"    critical: {failure}")
    for warning in (desired.get("warnings") or [])[:10]:
        print(f"    warning: {warning}")
    for action in actions[:10]:
        print(
            f"    transport: {action.get('server_id')} -> {action.get('interface')} "
            f"action={action.get('action')} service={action.get('service')}"
        )
    return 1 if args.strict and not healthy else 0


if __name__ == "__main__":
    raise SystemExit(main())
