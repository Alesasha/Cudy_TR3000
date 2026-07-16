#!/usr/bin/env python3
"""Check the Cudy Go fallback service over SSH.

This verifies only loopback endpoints on the router. It does not expose or
download the secret fallback archive.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "192.168.8.1"
DEFAULT_USER = "root"
DEFAULT_PASSWORD_FILE = ROOT / "secrets" / "cudy_ssh_password.txt"


def load_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    value = os.environ.get("CUDY_SSH_PASSWORD")
    if value:
        return value
    if DEFAULT_PASSWORD_FILE.exists():
        value = DEFAULT_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return getpass.getpass("Cudy SSH password: ")


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


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> tuple[int, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, (out + err).strip()


def parse_json_line(output: str, prefix: str) -> dict[str, Any]:
    for line in output.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix) :])
    raise ValueError(f"missing {prefix!r} line in output")


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def observer_is_fresh(observer: dict[str, Any], *, max_age_seconds: int = 300) -> tuple[bool, int | None]:
    last_success = parse_timestamp(observer.get("last_success_at"))
    if last_success is None:
        return False, None
    age = max(0, int((datetime.now(timezone.utc) - last_success).total_seconds()))
    return age <= max_age_seconds, age


def check(args: argparse.Namespace) -> dict[str, Any]:
    password = load_password(args.ssh_password)
    client = connect(args.host, args.user, password, args.timeout)
    try:
        rc, output = ssh_exec(
            client,
            """
set -eu
printf 'service='
/etc/init.d/cudy-fallback status || true
printf '\\ntunnel_service='
/etc/init.d/cudy-control-tunnel status || true
printf '\\nready='
curl -fsS --max-time 5 http://127.0.0.1:8765/readyz
printf '\\nruntime='
curl -fsS --max-time 10 http://127.0.0.1:8765/api/cudy/runtime
printf '\\nagent_preview='
curl -fsS --max-time 25 http://127.0.0.1:8765/api/cudy/agent-preview
printf '\\nagent_observer='
curl -fsS --max-time 5 http://127.0.0.1:8765/api/cudy/agent-observer
printf '\\n'
""".strip(),
            args.timeout,
        )
    finally:
        client.close()
    if rc != 0:
        return {"ok": False, "host": args.host, "error": output, "checks": []}

    lines = output.splitlines()
    service = next((line.split("=", 1)[1] for line in lines if line.startswith("service=")), "")
    tunnel_service = next(
        (line.split("=", 1)[1] for line in lines if line.startswith("tunnel_service=")), ""
    )
    ready = parse_json_line(output, "ready=")
    runtime = parse_json_line(output, "runtime=")
    agent_preview = parse_json_line(output, "agent_preview=")
    agent_observer = parse_json_line(output, "agent_observer=")
    observer_fresh, observer_age = observer_is_fresh(agent_observer)
    preview_source = str(agent_preview.get("source") or "")
    preview_ok = (
        bool(agent_preview.get("ok"))
        and bool(agent_preview.get("configured"))
        and preview_source in {"live", "cache"}
        and bool(agent_preview.get("routes"))
    )

    checks = [
        {"name": "service", "ok": service == "running", "summary": service or "missing"},
        {
            "name": "control-tunnel",
            "ok": tunnel_service == "running",
            "summary": tunnel_service or "missing",
        },
        {"name": "readyz", "ok": bool(ready.get("ok")), "summary": f"warnings={len(ready.get('warnings') or [])}"},
        {
            "name": "runtime",
            "ok": bool(runtime.get("ok"))
            and runtime.get("architecture") == "aarch64"
            and bool(runtime.get("supported_interfaces")),
            "summary": (
                f"arch={runtime.get('architecture')}; "
                f"interfaces={len(runtime.get('supported_interfaces') or [])}; "
                f"links={len(runtime.get('links') or [])}"
            ),
        },
        {
            "name": "agent-preview",
            "ok": preview_ok,
            "summary": (
                f"configured={bool(agent_preview.get('configured'))}; "
                f"source={preview_source or 'none'}; "
                f"cache_age={int(agent_preview.get('cache_age_seconds') or 0)}s; "
                f"routes={len(agent_preview.get('routes') or [])}; "
                f"transports={len(agent_preview.get('transport_plan') or [])}"
            ),
        },
        {
            "name": "agent-observer",
            "ok": bool(agent_observer.get("enabled"))
            and observer_fresh
            and not bool(agent_observer.get("last_error")),
            "summary": (
                f"enabled={bool(agent_observer.get('enabled'))}; "
                f"last_success_age={observer_age if observer_age is not None else 'never'}s; "
                f"error={agent_observer.get('last_error') or 'none'}"
            ),
        },
    ]
    return {
        "ok": all(item["ok"] for item in checks),
        "host": args.host,
        "checks": checks,
        "readyz": ready,
        "runtime": {
            "architecture": runtime.get("architecture"),
            "openwrt_target": runtime.get("openwrt_target"),
            "supported_interfaces": len(runtime.get("supported_interfaces") or []),
            "links": len(runtime.get("links") or []),
            "warnings": runtime.get("warnings") or [],
        },
        "agent_preview": {
            "ok": bool(agent_preview.get("ok")),
            "configured": bool(agent_preview.get("configured")),
            "source": preview_source,
            "cache_age_seconds": int(agent_preview.get("cache_age_seconds") or 0),
            "routes": len(agent_preview.get("routes") or []),
            "transports": len(agent_preview.get("transport_plan") or []),
            "warnings": agent_preview.get("warnings") or [],
            "error": agent_preview.get("error") or "",
        },
        "agent_observer": {
            "enabled": bool(agent_observer.get("enabled")),
            "last_attempt_at": agent_observer.get("last_attempt_at") or "",
            "last_success_at": agent_observer.get("last_success_at") or "",
            "last_success_age_seconds": observer_age,
            "last_error": agent_observer.get("last_error") or "",
            "cache_updated_at": agent_observer.get("cache_updated_at") or "",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=40)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = check(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Cudy Go fallback: {'OK' if result.get('ok') else 'FAIL'} host={result.get('host')}")
        for item in result.get("checks") or []:
            print(f"  [{'OK' if item.get('ok') else 'FAIL'}] {item.get('name')}: {item.get('summary')}")
        runtime = result.get("runtime") or {}
        if runtime:
            print(
                "  runtime: "
                f"arch={runtime.get('architecture')} target={runtime.get('openwrt_target')} "
                f"interfaces={runtime.get('supported_interfaces')} links={runtime.get('links')}"
            )
    if args.strict and not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
