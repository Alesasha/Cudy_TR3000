#!/usr/bin/env python3
"""Run a guarded, reversible Cudy router-agent apply trial.

The command is a preview unless both ``--apply`` and ``--yes`` are supplied.
The first trial deliberately refuses plans that create provider transports. A
rollback process runs on Cudy itself, so losing the workstation or SSH session
does not leave apply mode enabled.
"""

from __future__ import annotations

import argparse
import json
import shlex
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from deploy_cudy_go_fallback import connect, load_password, ssh_exec, upload_via_cat


STATE_DIR = "/var/lib/cudy-router-agent"
TRIAL_DIR = "/root/cudy-router-trials"
ALLOWED_PREFIX = PurePosixPath("/etc/pbr-overrides")


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


def parse_state(output: str) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return {
        "service": values.get("service", ""),
        "configured_mode": values.get("configured_mode", ""),
        "allow_apply": values.get("allow_apply", ""),
        "status": json.loads(values.get("status", "{}")),
        "desired": json.loads(values.get("desired", "{}")),
        "diff": json.loads(values.get("diff", "[]")),
    }


def validate_preflight(state: dict[str, Any], *, max_age_seconds: int) -> list[str]:
    errors: list[str] = []
    status = state.get("status") or {}
    desired = state.get("desired") or {}
    diff = state.get("diff") or []
    if state.get("service") != "running":
        errors.append("router-agent service is not running")
    if state.get("configured_mode") != "observe" or state.get("allow_apply") not in {"0", ""}:
        errors.append("persistent router-agent gate is not observe/disabled")
    if status.get("mode") != "observe" or status.get("ok") is not True:
        errors.append("latest observer cycle is not healthy")
    if status.get("critical_services_ok") is not True or int(status.get("critical_service_count") or 0) < 1:
        errors.append("critical-service checks are not healthy")
    if int(status.get("route_count") or 0) < 1:
        errors.append("observer plan contains no routes")
    updated_at = parse_timestamp(status.get("updated_at"))
    if updated_at is None:
        errors.append("observer status timestamp is invalid")
    elif (datetime.now(timezone.utc) - updated_at).total_seconds() > max_age_seconds:
        errors.append("observer status is stale")
    blockers = desired.get("blockers") or []
    if blockers:
        errors.append(f"observer plan has {len(blockers)} blocker(s)")
    transports = desired.get("transport_actions") or []
    if transports:
        errors.append("first guarded trial refuses plans that create or restart transports")
    if not isinstance(diff, list) or not diff:
        errors.append("observer diff is empty or invalid")
    for item in diff if isinstance(diff, list) else []:
        raw_path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw_path, str):
            errors.append("observer diff contains an invalid path")
            continue
        path = PurePosixPath(raw_path)
        if not path.is_absolute() or path.parent != ALLOWED_PREFIX or ".." in path.parts:
            errors.append(f"unsafe trial path: {raw_path}")
    return errors


def read_state(client: Any, timeout: int) -> dict[str, Any]:
    rc, output = ssh_exec(
        client,
        f"""
set -eu
printf 'service='; /etc/init.d/cudy-router-agent status || true
printf '\nconfigured_mode='; uci -q get cudy-router-agent.main.mode || true
printf '\nallow_apply='; uci -q get cudy-router-agent.main.allow_apply || true
printf '\nstatus='; tr -d '\n' < {STATE_DIR}/status.json
printf '\ndesired='; tr -d '\n' < {STATE_DIR}/desired.json
printf '\ndiff='; tr -d '\n' < {STATE_DIR}/diff.json
printf '\n'
""".strip(),
        timeout,
    )
    if rc != 0:
        raise RuntimeError(output)
    return parse_state(output)


def rollback_script(trial_path: str, trial_seconds: int) -> str:
    quoted = shlex.quote(trial_path)
    return f"""#!/bin/sh
set -u
trial={quoted}
delay="${{1:-{trial_seconds}}}"
touch "$trial/armed"
[ "$delay" = "0" ] || sleep "$delay"
rm -f "$trial/rollback.pid"
[ -f "$trial/commit" ] && exit 0
[ -f "$trial/rolled-back" ] && exit 0
/etc/init.d/cudy-router-agent stop 2>/dev/null || true
while IFS='|' read -r existed index path; do
  [ -n "$path" ] || continue
  if [ "$existed" = "1" ]; then
    cp -p "$trial/files/$index" "$path"
  else
    rm -f "$path"
  fi
done < "$trial/paths"
uci set cudy-router-agent.main.mode='observe'
uci set cudy-router-agent.main.allow_apply='0'
uci commit cudy-router-agent
for name in managed-paths.json managed-paths.next.json; do
  if [ -f "$trial/state/$name.existed" ]; then
    cp -p "$trial/state/$name" "{STATE_DIR}/$name"
  else
    rm -f "{STATE_DIR}/$name"
  fi
done
if [ -f "$trial/pbr.was-running" ]; then
  pbr_failed=0
  /usr/bin/cudy-pbr-fast-apply \
    || /usr/bin/cudy-pbr-safe-restart restart \
    || pbr_failed=1
  if [ "$pbr_failed" = "1" ]; then
    /etc/init.d/pbr stop >/dev/null 2>&1 || true
    date -u +%Y-%m-%dT%H:%M:%SZ > "$trial/rollback-pbr-failed"
  fi
else
  /etc/init.d/pbr stop >/dev/null 2>&1 || true
fi
/etc/init.d/cudy-router-agent start >/dev/null 2>&1 || true
date -u +%Y-%m-%dT%H:%M:%SZ > "$trial/rolled-back"
"""


def start_trial(client: Any, state: dict[str, Any], args: argparse.Namespace) -> str:
    trial_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    trial_path = f"{TRIAL_DIR}/{trial_id}"
    paths = [item["path"] for item in state["diff"]]
    with tempfile.TemporaryDirectory(prefix="cudy-router-trial-") as temp_dir:
        guard = Path(temp_dir) / "rollback.sh"
        guard.write_text(rollback_script(trial_path, args.trial_seconds), encoding="utf-8", newline="\n")
        upload_via_cat(client, guard, "/tmp/cudy-router-trial-rollback.sh")
    path_words = " ".join(shlex.quote(path) for path in paths)
    command = f"""
set -eu
trial={shlex.quote(trial_path)}
mkdir -p "$trial/files" "$trial/state"
chmod 0700 "$trial" "$trial/files" "$trial/state"
cp {STATE_DIR}/status.json "$trial/status.before.json"
cp {STATE_DIR}/desired.json "$trial/desired.before.json"
cp {STATE_DIR}/diff.json "$trial/diff.before.json"
for name in managed-paths.json managed-paths.next.json; do
  if [ -e "{STATE_DIR}/$name" ]; then
    cp -p "{STATE_DIR}/$name" "$trial/state/$name"
    touch "$trial/state/$name.existed"
  fi
done
if ip -4 rule show 2>/dev/null | grep -Eq 'fwmark .* lookup pbr_' && \
   nft list chain inet fw4 pbr_prerouting 2>/dev/null | grep -q 'goto pbr_mark_'; then
  touch "$trial/pbr.was-running"
fi
: > "$trial/paths"
index=0
for path in {path_words}; do
  index=$((index + 1))
  if [ -e "$path" ]; then
    cp -p "$path" "$trial/files/$index"
    printf '1|%s|%s\n' "$index" "$path" >> "$trial/paths"
  else
    printf '0|%s|%s\n' "$index" "$path" >> "$trial/paths"
  fi
done
cp /tmp/cudy-router-trial-rollback.sh "$trial/rollback.sh"
chmod 0700 "$trial/rollback.sh"
test -x /sbin/start-stop-daemon
/sbin/start-stop-daemon -S -b -m -p "$trial/rollback.pid" \
  -x "$trial/rollback.sh" -O "$trial/rollback.log" -- {args.trial_seconds}
i=0
while [ "$i" -lt 5 ]; do
  [ -f "$trial/armed" ] && break
  sleep 1
  i=$((i + 1))
done
test -f "$trial/armed"
uci set cudy-router-agent.main.mode='apply'
uci set cudy-router-agent.main.allow_apply='1'
uci commit cudy-router-agent
if ! /etc/init.d/cudy-router-agent restart; then
  "$trial/rollback.sh" 0
  exit 1
fi
printf '%s\n' "$trial"
""".strip()
    rc, output = ssh_exec(client, command, max(args.timeout, 120))
    if rc != 0:
        raise RuntimeError(output)
    return output.splitlines()[-1].strip()


def wait_for_apply(client: Any, timeout: int, settle_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + settle_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            last = read_state(client, timeout)
        except Exception:
            continue
        status = last.get("status") or {}
        if status.get("mode") == "apply" and status.get("applied") is True:
            return last
        if status.get("rolled_back") is True or status.get("error"):
            break
    raise RuntimeError(f"apply did not become healthy within {settle_seconds}s: {last.get('status') or {}}")


def commit_trial(client: Any, trial_path: str, timeout: int) -> None:
    rc, output = ssh_exec(
        client,
        f"""
set -eu
trial={shlex.quote(trial_path)}
touch "$trial/commit"
pid="$(cat "$trial/rollback.pid" 2>/dev/null || true)"
[ -z "$pid" ] || kill "$pid" 2>/dev/null || true
uci set cudy-router-agent.main.mode='observe'
uci set cudy-router-agent.main.allow_apply='0'
uci commit cudy-router-agent
/etc/init.d/cudy-router-agent restart
date -u +%Y-%m-%dT%H:%M:%SZ > "$trial/committed"
""".strip(),
        timeout,
    )
    if rc != 0:
        raise RuntimeError(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--max-age-seconds", type=int, default=300)
    parser.add_argument("--trial-seconds", type=int, default=300)
    parser.add_argument("--settle-seconds", type=int, default=150)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--commit", action="store_true", help="Keep a healthy apply result, then return the service to observe mode.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.commit and not (args.apply and args.yes):
        raise SystemExit("--commit requires --apply --yes")
    if args.trial_seconds < 120:
        raise SystemExit("--trial-seconds must be at least 120")
    if args.trial_seconds <= args.settle_seconds + 30:
        raise SystemExit("--trial-seconds must exceed --settle-seconds by at least 30 seconds")
    client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
    try:
        state = read_state(client, args.timeout)
        errors = validate_preflight(state, max_age_seconds=args.max_age_seconds)
        status = state["status"]
        print(
            f"Cudy apply trial preflight: {'OK' if not errors else 'BLOCKED'} "
            f"mode={status.get('mode')} routes={status.get('route_count')} "
            f"files={len(state.get('diff') or [])} critical={status.get('critical_service_count')}"
        )
        for item in state.get("diff") or []:
            print(f"  {item.get('path')}")
        for error in errors:
            print(f"  blocker: {error}")
        if errors:
            return 1
        if not (args.apply and args.yes):
            print("Preview only. Applying requires --apply --yes; keeping the result also requires --commit.")
            return 0
        trial_path = start_trial(client, state, args)
        print(f"Guarded trial started: {trial_path}; automatic rollback in {args.trial_seconds}s")
        applied = wait_for_apply(client, args.timeout, args.settle_seconds)
        applied_status = applied["status"]
        if applied_status.get("critical_services_ok") is not True or applied_status.get("ok") is not True:
            raise RuntimeError("post-apply status is not healthy; local Cudy guard remains armed")
        print("Apply reached a healthy state; local Cudy rollback guard is still armed.")
        if args.commit:
            commit_trial(client, trial_path, args.timeout)
            print("Trial committed; router-agent returned to observe mode with the applied files retained.")
        else:
            print("Trial was not committed and will roll back automatically.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
