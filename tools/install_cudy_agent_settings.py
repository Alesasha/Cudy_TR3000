#!/usr/bin/env python3
"""Install root-only Cudy agent settings for read-only Go preview.

This helper does not create tokens and does not enable route apply mode. It
only places `/etc/cudy-fallback/agent.json` and `agent.token` on Cudy so the Go
fallback service can fetch `/api/agent/config` for `/api/cudy/agent-preview`.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from deploy_cudy_go_fallback import connect, load_password, ssh_exec, upload_via_cat


DEFAULT_HOST = "192.168.8.1"
DEFAULT_USER = "root"
DEFAULT_REMOTE_DIR = "/etc/cudy-fallback"


def load_agent_token(args: argparse.Namespace) -> str:
    if args.local_token_file:
        value = Path(args.local_token_file).read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    value = os.environ.get(args.token_env, "").strip()
    if value:
        return value
    raise SystemExit(
        f"Missing Cudy agent token. Set {args.token_env} or pass --local-token-file."
    )


def write_temp_file(directory: Path, name: str, data: bytes) -> Path:
    path = directory / name
    path.write_bytes(data)
    return path


def install(args: argparse.Namespace) -> dict[str, Any]:
    token = "" if args.dry_run else load_agent_token(args)
    remote_dir = args.remote_dir.rstrip("/")
    agent_json_path = f"{remote_dir}/agent.json"
    token_path = f"{remote_dir}/agent.token"
    settings = {
        "control_url": args.control_url,
        "agent_config_path": args.agent_config_path,
        "device_id": args.device_id,
        "token_file": token_path,
    }

    if args.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "host": args.host,
            "install": {
                "agent_json": agent_json_path,
                "agent_token": token_path,
                "control_url": args.control_url,
                "device_id": args.device_id,
            },
        }

    password = load_password(args.ssh_password)
    client = connect(args.host, args.user, password, args.timeout)
    try:
        with tempfile.TemporaryDirectory(prefix="cudy-agent-settings-") as tmp_raw:
            tmp = Path(tmp_raw)
            local_agent_json = write_temp_file(
                tmp,
                "agent.json",
                json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            local_token = write_temp_file(tmp, "agent.token", (token + "\n").encode("utf-8"))
            upload_via_cat(client, local_agent_json, "/tmp/cudy-agent.json")
            upload_via_cat(client, local_token, "/tmp/cudy-agent.token")

        rc, output = ssh_exec(
            client,
            f"""
set -eu
umask 077
mkdir -p {shell_quote(remote_dir)}
chmod 0700 {shell_quote(remote_dir)}
mv /tmp/cudy-agent.json {shell_quote(agent_json_path)}
mv /tmp/cudy-agent.token {shell_quote(token_path)}
chmod 0600 {shell_quote(agent_json_path)} {shell_quote(token_path)}
chown root:root {shell_quote(agent_json_path)} {shell_quote(token_path)}
printf 'installed=1\\n'
if [ -x /etc/init.d/cudy-fallback ]; then /etc/init.d/cudy-fallback restart >/dev/null 2>&1 || true; fi
if command -v curl >/dev/null 2>&1; then
  printf 'agent_preview='
  curl -fsS --max-time 15 http://127.0.0.1:8765/api/cudy/agent-preview || true
  printf '\\n'
fi
""".strip(),
            args.timeout,
        )
    finally:
        client.close()

    result: dict[str, Any] = {
        "ok": rc == 0,
        "host": args.host,
        "agent_json": agent_json_path,
        "agent_token": token_path,
        "output": output,
    }
    preview = parse_prefixed_json(output, "agent_preview=")
    if preview is not None:
        result["agent_preview"] = {
            "configured": bool(preview.get("configured")),
            "ok": bool(preview.get("ok")),
            "routes": len(preview.get("routes") or []),
            "transports": len(preview.get("transport_plan") or []),
            "error": preview.get("error") or "",
        }
    return result


def parse_prefixed_json(output: str, prefix: str) -> dict[str, Any] | None:
    for line in output.splitlines():
        if not line.startswith(prefix):
            continue
        try:
            payload = json.loads(line[len(prefix) :])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--control-url", default="http://127.0.0.1:18765")
    parser.add_argument("--agent-config-path", default="/api/agent/config")
    parser.add_argument("--device-id", default="cudy-home")
    parser.add_argument("--token-env", default="CUDY_AGENT_TOKEN")
    parser.add_argument("--local-token-file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = install(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Cudy agent settings install: {'OK' if result.get('ok') else 'FAIL'} host={result.get('host')}")
        if result.get("dry_run"):
            install_plan = result.get("install") or {}
            print(f"  agent_json={install_plan.get('agent_json')}")
            print(f"  agent_token={install_plan.get('agent_token')}")
            print(f"  control_url={install_plan.get('control_url')}")
            print(f"  device_id={install_plan.get('device_id')}")
        else:
            print(f"  agent_json={result.get('agent_json')}")
            print(f"  agent_token={result.get('agent_token')}")
            preview = result.get("agent_preview") or {}
            if preview:
                print(
                    "  agent_preview: "
                    f"configured={preview.get('configured')} ok={preview.get('ok')} "
                    f"routes={preview.get('routes')} transports={preview.get('transports')}"
                )
                if preview.get("error"):
                    print(f"  preview_error={preview.get('error')}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
