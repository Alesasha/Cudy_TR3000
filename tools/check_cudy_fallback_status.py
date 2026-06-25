#!/usr/bin/env python3
"""Check Cudy fallback-control public status files.

This tool reads only static JSON files published under /www/cudy-control. It
does not download or inspect the secret backup archive stored under /root.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_ENDPOINTS_URL = "http://192.168.8.1/cudy-control/endpoints.json"
DEFAULT_STATE_URL = "http://192.168.8.1/cudy-control/state.json"


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_json(url: str, *, timeout: int) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "cudy-fallback-status/1"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object from {url}")
    return payload


def check_endpoint_manifest(manifest: dict[str, Any], *, now_utc: datetime) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    endpoints = manifest.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        checks.append({"name": "endpoint-list", "status": "ok", "message": f"{len(endpoints)} endpoint(s)"})
    else:
        checks.append({"name": "endpoint-list", "status": "fail", "message": "endpoint list is empty"})

    valid_until_raw = manifest.get("valid_until")
    if isinstance(valid_until_raw, str) and valid_until_raw:
        valid_until = parse_time(valid_until_raw)
        remaining_seconds = int((valid_until - now_utc).total_seconds())
        status = "ok" if remaining_seconds > 0 else "fail"
        checks.append(
            {
                "name": "endpoint-validity",
                "status": status,
                "message": f"valid_until={valid_until.isoformat()} remaining_seconds={remaining_seconds}",
            }
        )
    else:
        checks.append({"name": "endpoint-validity", "status": "fail", "message": "valid_until is missing"})

    cache_seconds = manifest.get("cache_seconds")
    if isinstance(cache_seconds, int) and cache_seconds > 0:
        checks.append({"name": "endpoint-cache", "status": "ok", "message": f"cache_seconds={cache_seconds}"})
    else:
        checks.append({"name": "endpoint-cache", "status": "warn", "message": "cache_seconds is missing"})
    return checks


def check_state(state: dict[str, Any], *, now_utc: datetime, max_age_minutes: int) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    created_at_raw = state.get("created_at")
    if isinstance(created_at_raw, str) and created_at_raw:
        created_at = parse_time(created_at_raw)
        age_minutes = int((now_utc - created_at).total_seconds() // 60)
        status = "ok" if age_minutes <= max_age_minutes else "warn"
        checks.append(
            {
                "name": "state-age",
                "status": status,
                "message": f"created_at={created_at.isoformat()} age_minutes={age_minutes}",
            }
        )
    else:
        checks.append({"name": "state-age", "status": "fail", "message": "created_at is missing"})

    archive_name = state.get("archive_name")
    digest = state.get("sha256")
    size = state.get("bytes")
    if isinstance(archive_name, str) and archive_name and isinstance(digest, str) and len(digest) == 64 and isinstance(size, int) and size > 0:
        checks.append(
            {
                "name": "state-archive",
                "status": "ok",
                "message": f"archive={archive_name} bytes={size} sha256={digest[:12]}...",
            }
        )
    else:
        checks.append({"name": "state-archive", "status": "fail", "message": "archive metadata is incomplete"})
    return checks


def summarize(checks: list[dict[str, str]]) -> str:
    lines = []
    for check in checks:
        lines.append(f"[{check['status'].upper()}] {check['name']}: {check['message']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoints-url", default=DEFAULT_ENDPOINTS_URL)
    parser.add_argument("--state-url", default=DEFAULT_STATE_URL)
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--max-state-age-minutes", type=int, default=180)
    parser.add_argument("--json", action="store_true", help="Print machine-readable status")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on fail or warn")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    result: dict[str, Any] = {
        "ok": False,
        "generated_at": now_utc.isoformat(),
        "endpoints_url": args.endpoints_url,
        "state_url": args.state_url,
        "checks": [],
    }
    checks: list[dict[str, str]] = []
    try:
        endpoints = fetch_json(args.endpoints_url, timeout=args.timeout)
        checks.extend(check_endpoint_manifest(endpoints, now_utc=now_utc))
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        checks.append({"name": "fetch-endpoints", "status": "fail", "message": str(exc)})

    try:
        state = fetch_json(args.state_url, timeout=args.timeout)
        checks.extend(check_state(state, now_utc=now_utc, max_age_minutes=args.max_state_age_minutes))
    except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        checks.append({"name": "fetch-state", "status": "fail", "message": str(exc)})

    result["checks"] = checks
    result["ok"] = all(check["status"] == "ok" for check in checks)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(summarize(checks))
    if args.strict and not result["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
