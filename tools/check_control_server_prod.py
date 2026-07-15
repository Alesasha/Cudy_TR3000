#!/usr/bin/env python3
"""Check the production uswest control-server over SSH.

The check prints only service and health summaries. It does not print provider
secrets, agent tokens, or the remote database contents.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import paramiko


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "95.182.91.203"
DEFAULT_USER = "root"
DEFAULT_PASSWORD_FILE = ROOT / "secrets" / "control_backup_ssh_password.txt"
DEFAULT_REMOTE_DIR = "/opt/cudy-control"
DEFAULT_SERVICE = "vpn-control.service"
DEFAULT_HTTP_FALLBACK_URL = "http://127.0.0.1:18765"


logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)


def load_password(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("CONTROL_BACKUP_SSH_PASSWORD", "USWEST_SSH_PASSWORD", "AWG_SSH_PASSWORD_HOSTVDS_USWEST"):
        value = os.environ.get(name)
        if value:
            return value
    if DEFAULT_PASSWORD_FILE.exists():
        value = DEFAULT_PASSWORD_FILE.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    return getpass.getpass("SSH password for production control-server: ")


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
            if attempt < max(1, attempts):
                time.sleep(min(2 * attempt, 5))
    raise RuntimeError(f"SSH connect failed after {max(1, attempts)} attempt(s): {last_error}") from last_error


def ssh_exec(client: paramiko.SSHClient, command: str, timeout: int) -> tuple[int, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, (out + err).strip()


def read_remote_json(client: paramiko.SSHClient, command: str, timeout: int) -> dict[str, Any]:
    rc, output = ssh_exec(client, command, timeout)
    if rc != 0:
        raise RuntimeError(output)
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise ValueError("remote command did not return a JSON object")
    return payload


def read_http_json(base_url: str, path: str, timeout: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    try:
        with urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        except json.JSONDecodeError as decode_exc:
            raise RuntimeError(f"HTTP fallback failed for {url}: {exc}") from decode_exc
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"HTTP fallback failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"HTTP fallback did not return a JSON object: {url}")
    return payload


def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    workers = status.get("workers") or {}
    agents = status.get("agents") or {}
    probe_jobs = status.get("probe_jobs") or {}
    transports = status.get("transports") or {}
    control = status.get("control") or {}
    return {
        "ok": bool(status.get("ok")),
        "warnings": status.get("warnings") or [],
        "advisories": status.get("advisories") or [],
        "agents": {
            "online": agents.get("online"),
            "enabled": agents.get("enabled"),
            "offline_enabled": agents.get("offline_enabled"),
        },
        "workers": {
            name: {
                "enabled": item.get("enabled"),
                "last_error": item.get("last_error"),
                "last_finished_age_seconds": item.get("last_finished_age_seconds"),
            }
            for name, item in workers.items()
            if isinstance(item, dict)
        },
        "probe_jobs": {
            "pending": probe_jobs.get("pending"),
            "failed_recent": probe_jobs.get("failed_recent"),
        },
        "transports": {
            "enabled": transports.get("enabled"),
            "total": transports.get("total"),
            "stale_enabled_count": transports.get("stale_enabled_count"),
        },
        "cudy_fallback_reachable_from_prod": bool((control.get("cudy_fallback_state") or {}).get("reachable")),
    }


def summarize_readyz(ready: dict[str, Any]) -> dict[str, Any]:
    checks = ready.get("checks") or []
    by_name = {item.get("name"): item for item in checks if isinstance(item, dict)}
    agents_summary = str((by_name.get("agents") or {}).get("summary") or "")
    transports_summary = str((by_name.get("transports") or {}).get("summary") or "")
    probe_summary = str((by_name.get("probe_jobs") or {}).get("summary") or "")
    return {
        "ok": bool(ready.get("ok")),
        "warnings": ready.get("warnings") or [],
        "advisories": ready.get("advisories") or [],
        "agents": {"summary": agents_summary},
        "workers": {},
        "probe_jobs": {"summary": probe_summary},
        "transports": {"summary": transports_summary},
        "cudy_fallback_reachable_from_prod": None,
    }


def check_via_http_fallback(args: argparse.Namespace, ssh_error: Exception) -> dict[str, Any]:
    health = read_http_json(args.http_fallback_url, "/healthz", args.timeout)
    ready = read_http_json(args.http_fallback_url, "/readyz", args.timeout)
    status_summary = summarize_readyz(ready)
    ok = bool(health.get("ok") is True and ready.get("ok") is True)
    if args.require_ssh:
        ok = False
    return {
        "ok": ok,
        "host": args.host,
        "mode": "http_fallback",
        "ssh_error": str(ssh_error),
        "service": {
            "ok": None,
            "lines": [f"SSH audit unavailable: {ssh_error}"],
        },
        "healthz": health,
        "readyz": {
            "ok": ready.get("ok"),
            "warnings": ready.get("warnings") or [],
            "advisories": ready.get("advisories") or [],
            "checks": ready.get("checks") or [],
        },
        "status": status_summary,
    }


def check(args: argparse.Namespace) -> dict[str, Any]:
    password = load_password(args.ssh_password)
    try:
        client = connect(args.host, args.user, password, args.timeout, attempts=args.connect_attempts)
    except Exception as exc:
        if args.http_fallback_url and not args.no_http_fallback:
            return check_via_http_fallback(args, exc)
        raise
    try:
        service_rc, service_output = ssh_exec(
            client,
            (
                f"systemctl is-enabled {args.service}; "
                f"systemctl is-active {args.service}; "
                f"systemctl show {args.service} -p NRestarts -p Restart -p RestartUSec --no-pager"
            ),
            args.timeout,
        )
        health = read_remote_json(client, "curl -fsS --max-time 5 http://127.0.0.1:8765/healthz", args.timeout)
        ready = read_remote_json(client, "curl -sS --max-time 5 http://127.0.0.1:8765/readyz", args.timeout)
        status = read_remote_json(
            client,
            (
                f"cd {args.remote_dir} && python3 tools/vpn_control_app.py "
                f"--db {args.remote_dir}/data/vpn_control.db "
                f"--inventory {args.remote_dir}/config/vpn_inventory.json "
                "system-status --json"
            ),
            args.timeout,
        )
    finally:
        client.close()

    service_lines = [line.strip() for line in service_output.splitlines() if line.strip()]
    service_ok = service_rc == 0 and len(service_lines) >= 2 and service_lines[0] == "enabled" and service_lines[1] == "active"
    status_summary = summarize_status(status)
    ok = bool(service_ok and health.get("ok") is True and ready.get("ok") is True)
    return {
        "ok": ok,
        "host": args.host,
        "mode": "ssh",
        "ssh_error": "",
        "service": {
            "ok": service_ok,
            "lines": service_lines,
        },
        "healthz": health,
        "readyz": {
            "ok": ready.get("ok"),
            "warnings": ready.get("warnings") or [],
            "advisories": ready.get("advisories") or [],
            "checks": ready.get("checks") or [],
        },
        "status": status_summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--ssh-password")
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="SSH and remote audit timeout; system-status may include slow fallback reachability checks.",
    )
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--http-fallback-url", default=os.environ.get("CONTROL_HTTP_FALLBACK_URL", DEFAULT_HTTP_FALLBACK_URL))
    parser.add_argument("--no-http-fallback", action="store_true", help="Do not use local HTTP tunnel fallback when SSH audit is unavailable.")
    parser.add_argument("--require-ssh", action="store_true", help="Fail strict mode if SSH audit is unavailable even when HTTP readiness is OK.")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when the summary is not ok")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = check(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Production control-server: {'OK' if result['ok'] else 'WARN'} host={result['host']} mode={result.get('mode') or 'ssh'}")
        if result.get("ssh_error"):
            print(f"ssh: WARN {result['ssh_error']}")
        print(f"service: {'OK' if result['service']['ok'] else 'WARN'} {'; '.join(result['service']['lines'])}")
        print(f"healthz: {result['healthz'].get('ok')}")
        print(f"readyz: {result['readyz'].get('ok')} checks={len(result['readyz'].get('checks') or [])}")
        status = result["status"]
        print(f"workers: {status.get('workers')}")
        print(f"agents: {status.get('agents')}")
        print(f"transports: {status.get('transports')}")
        if status.get("warnings"):
            print(f"warnings: {status['warnings']}")
        if status.get("advisories"):
            print(f"advisories: {status['advisories']}")
    if args.strict and not result["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
