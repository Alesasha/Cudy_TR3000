#!/usr/bin/env python3
"""Run a guarded Cudy provider-transport bootstrap before the PBR route trial.

The command is a preview unless both ``--apply`` and ``--yes`` are supplied.
Transport files, service state and the PBR config are backed up on Cudy. An
on-router process restores them even if this workstation or SSH disappears.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from deploy_cudy_go_fallback import connect, load_password, ssh_exec, upload_via_cat
from trial_cudy_router_agent_apply import read_state


TRIAL_DIR = "/root/cudy-transport-trials"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def transport_paths(actions: list[dict[str, Any]]) -> list[str]:
    paths = {"/etc/config/pbr"}
    for action in actions:
        service = str(action.get("service") or "")
        config_path = str(action.get("config_path") or "")
        paths.add(config_path)
        paths.add(f"/etc/init.d/{service}")
    return sorted(paths)


def validate_preflight(state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = state.get("status") or {}
    desired = state.get("desired") or {}
    actions = desired.get("transport_actions") or []
    if state.get("service") != "running":
        errors.append("router-agent service is not running")
    if state.get("configured_mode") != "observe" or state.get("allow_apply") not in {"0", ""}:
        errors.append("persistent router-agent gate is not observe/disabled")
    if status.get("mode") != "observe" or status.get("ok") is not True:
        errors.append("latest observer cycle is not healthy")
    if desired.get("blockers"):
        errors.append("observer policy has blockers")
    if not actions:
        errors.append("observer plan has no transport actions")
    for action in actions:
        server_id = str(action.get("server_id") or "")
        interface = str(action.get("interface") or "")
        service = str(action.get("service") or "")
        config_path = str(action.get("config_path") or "")
        if not all(SAFE_NAME.fullmatch(item) for item in (server_id, interface, service)):
            errors.append(f"unsafe transport identity: {server_id}/{interface}/{service}")
        if service != f"sing-box-{interface}":
            errors.append(f"unexpected transport service: {service}")
        path = PurePosixPath(config_path)
        if not path.is_absolute() or path.parent != PurePosixPath("/etc/sing-box") or path.suffix != ".json":
            errors.append(f"unsafe transport config path: {config_path}")
        elif path.name != f"{interface}.json":
            errors.append(f"unexpected transport config name: {config_path}")
        if action.get("transport_type") not in {"http-proxy-tun", "vless-reality-tun", "sing-box-json"}:
            errors.append(f"unsupported managed transport type: {action.get('transport_type')}")
    return errors


def rollback_script(trial_path: str, actions: list[dict[str, Any]], trial_seconds: int) -> str:
    trial = shlex.quote(trial_path)
    services = " ".join(shlex.quote(str(item["service"])) for item in actions)
    return f"""#!/bin/sh
set -u
trial={trial}
delay="${{1:-{trial_seconds}}}"
touch "$trial/armed"
[ "$delay" = "0" ] || sleep "$delay"
rm -f "$trial/rollback.pid"
[ -f "$trial/commit" ] && exit 0
[ -f "$trial/rolled-back" ] && exit 0
/etc/init.d/cudy-router-agent stop 2>/dev/null || true
for service in {services}; do
  /etc/init.d/$service stop 2>/dev/null || true
  /etc/init.d/$service disable 2>/dev/null || true
done
while IFS='|' read -r existed index path; do
  [ -n "$path" ] || continue
  if [ "$existed" = "1" ]; then
    cp -p "$trial/files/$index" "$path"
  else
    rm -f "$path"
  fi
done < "$trial/paths"
for service in {services}; do
  if [ -f "$trial/service-$service.enabled" ]; then
    /etc/init.d/$service enable 2>/dev/null || true
  fi
  if [ -f "$trial/service-$service.running" ]; then
    /etc/init.d/$service restart 2>/dev/null || true
  else
    /etc/init.d/$service stop 2>/dev/null || true
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
    actions = state["desired"]["transport_actions"]
    paths = transport_paths(actions)
    trial_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    trial_path = f"{TRIAL_DIR}/{trial_id}"
    with tempfile.TemporaryDirectory(prefix="cudy-transport-trial-") as temp_dir:
        guard = Path(temp_dir) / "rollback.sh"
        guard.write_text(rollback_script(trial_path, actions, args.trial_seconds), encoding="utf-8", newline="\n")
        upload_via_cat(client, guard, "/tmp/cudy-transport-trial-rollback.sh")
    path_words = " ".join(shlex.quote(path) for path in paths)
    service_words = " ".join(shlex.quote(str(item["service"])) for item in actions)
    command = f"""
set -eu
trial={shlex.quote(trial_path)}
mkdir -p "$trial/files" "$trial/prepare-state"
chmod 0700 "$trial" "$trial/files" "$trial/prepare-state"
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
for service in {service_words}; do
  /etc/init.d/$service enabled 2>/dev/null && touch "$trial/service-$service.enabled" || true
  /etc/init.d/$service status 2>/dev/null | grep -q running && touch "$trial/service-$service.running" || true
done
if ip -4 rule show 2>/dev/null | grep -Eq 'fwmark .* lookup pbr_' && \
   nft list chain inet fw4 pbr_prerouting 2>/dev/null | grep -q 'goto pbr_mark_'; then
  touch "$trial/pbr.was-running"
fi
cp /tmp/cudy-transport-trial-rollback.sh "$trial/rollback.sh"
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
/etc/init.d/cudy-router-agent stop
if ! /usr/bin/cudy-router-agent \
  -mode prepare -allow-transport-prepare -once -probe-limit 0 \
  -preview-url http://127.0.0.1:8765/api/cudy/agent-preview \
  -control-url http://127.0.0.1:18765 \
  -token-file /etc/cudy-fallback/agent.token \
  -state-dir "$trial/prepare-state" \
  -override-dir /etc/pbr-overrides \
  -bootstrap-command /usr/bin/cudy-pbr-safe-restart; then
  "$trial/rollback.sh" 0
  exit 1
fi
/etc/init.d/cudy-router-agent start
printf '%s\n' "$trial"
""".strip()
    rc, output = ssh_exec(client, command, max(args.timeout, 240))
    if rc != 0:
        raise RuntimeError(output)
    return output.splitlines()[-1].strip()


def commit_trial(client: Any, trial_path: str, timeout: int) -> None:
    rc, output = ssh_exec(
        client,
        f"""
set -eu
trial={shlex.quote(trial_path)}
touch "$trial/commit"
/etc/init.d/cudy-router-agent start >/dev/null 2>&1 || true
date -u +%Y-%m-%dT%H:%M:%SZ > "$trial/committed"
""".strip(),
        timeout,
    )
    if rc != 0:
        raise RuntimeError(output)


def wait_for_rollback(client: Any, trial_path: str, timeout: int, trial_seconds: int) -> None:
    deadline = time.monotonic() + trial_seconds + 45
    while time.monotonic() < deadline:
        rc, output = ssh_exec(
            client,
            f"""
if [ -f {shlex.quote(trial_path)}/rolled-back ]; then
  printf 'rolled_back=yes\nservice='; /etc/init.d/cudy-router-agent status || true
  printf '\nmode='; uci -q get cudy-router-agent.main.mode || true
else
  printf 'rolled_back=no\n'
fi
""".strip(),
            timeout,
        )
        if rc == 0 and "rolled_back=yes" in output:
            if "service=running" not in output or "mode=observe" not in output:
                raise RuntimeError(f"rollback marker exists but observer was not restored: {output}")
            print("Automatic rollback verified on Cudy; observer is running in observe mode.")
            return
        time.sleep(10)
    raise RuntimeError("automatic transport rollback was not verified before the deadline")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.8.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--trial-seconds", type=int, default=300)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--commit", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.commit and not (args.apply and args.yes):
        raise SystemExit("--commit requires --apply --yes")
    if args.trial_seconds < 120:
        raise SystemExit("--trial-seconds must be at least 120")
    client = connect(args.host, args.user, load_password(args.ssh_password), args.timeout)
    try:
        state = read_state(client, args.timeout)
        errors = validate_preflight(state)
        actions = state.get("desired", {}).get("transport_actions") or []
        print(f"Cudy transport bootstrap preflight: {'OK' if not errors else 'BLOCKED'} actions={len(actions)}")
        for action in actions:
            print(f"  {action.get('server_id')} -> {action.get('interface')} action={action.get('action')}")
        for path in transport_paths(actions) if actions else []:
            print(f"  backup: {path}")
        for error in errors:
            print(f"  blocker: {error}")
        if errors:
            return 1
        if not (args.apply and args.yes):
            print("Preview only. Applying requires --apply --yes; retaining the result also requires --commit.")
            return 0
        trial_path = start_trial(client, state, args)
        print(f"Guarded transport bootstrap completed: {trial_path}")
        print(f"Independent on-router rollback remains armed for {args.trial_seconds}s.")
        if args.commit:
            commit_trial(client, trial_path, args.timeout)
            print("Transport bootstrap committed; rollback guard disarmed.")
        else:
            print("Trial was not committed and will roll back automatically.")
            wait_for_rollback(client, trial_path, args.timeout, args.trial_seconds)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
