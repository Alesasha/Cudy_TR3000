#!/usr/bin/env python3
"""Start the control server on a temporary DB and smoke-check public HTTP endpoints."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "config" / "vpn_inventory.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_json(url: str, *, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def fetch_text(opener: urllib.request.OpenerDirector, url: str, *, timeout: int = 5) -> str:
    with opener.open(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def fetch_json_with_opener(opener: urllib.request.OpenerDirector, url: str, *, timeout: int = 5) -> dict:
    return json.loads(fetch_text(opener, url, timeout=timeout))


def post_json(opener: urllib.request.OpenerDirector, url: str, payload: dict, *, timeout: int = 5) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_healthz(base_url: str, *, timeout_seconds: int = 20) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            payload = fetch_json(f"{base_url}/healthz", timeout=2)
            if payload.get("ok") is True:
                return
            last_error = RuntimeError(f"unexpected health payload: {payload!r}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
        time.sleep(0.3)
    raise RuntimeError(f"control server did not become healthy: {last_error}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-control-http-") as tmp:
        port = free_port()
        db_path = Path(tmp) / "vpn_control.db"
        base_url = f"http://127.0.0.1:{port}"
        command = [
            sys.executable,
            str(ROOT / "tools" / "vpn_control_app.py"),
            "--db",
            str(db_path),
            "--inventory",
            str(INVENTORY),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-auto-worker",
            "--no-provider-refresh-worker",
        ]
        subprocess.check_call(
            [
                sys.executable,
                str(ROOT / "tools" / "vpn_control_app.py"),
                "--db",
                str(db_path),
                "--inventory",
                str(INVENTORY),
                "create-user",
                "smoke-admin",
                "--role",
                "admin",
                "--password",
                "smoke-password",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
        )
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for_healthz(base_url)
            readiness = fetch_json(f"{base_url}/readyz")
            if readiness.get("ok") is not True:
                raise AssertionError(f"unexpected readiness payload: {readiness!r}")
            if not readiness.get("checks"):
                raise AssertionError(f"readiness checks are empty: {readiness!r}")
            manifest = fetch_json(f"{base_url}/api/control/endpoints")
            endpoints = manifest.get("endpoints") or []
            if not endpoints:
                raise AssertionError("endpoint manifest is empty")
            if not manifest.get("valid_until"):
                raise AssertionError(f"endpoint manifest has no valid_until: {manifest!r}")
            if manifest.get("cache_seconds") != 300:
                raise AssertionError(f"unexpected live manifest cache_seconds: {manifest!r}")
            if endpoints[0].get("role") != "primary":
                raise AssertionError(f"first endpoint is not primary: {endpoints[0]!r}")

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
            login_page = fetch_text(opener, f"{base_url}/login")
            for snippet in ("loginForm", "/api/login"):
                if snippet not in login_page:
                    raise AssertionError(f"login page is missing {snippet!r}")
            login = post_json(
                opener,
                f"{base_url}/api/login",
                {"username": "smoke-admin", "password": "smoke-password"},
            )
            if login.get("ok") is not True:
                raise AssertionError(f"login failed: {login!r}")
            admin_page = fetch_text(opener, f"{base_url}/admin")
            for snippet in (
                "globalDefaultPriorityForm",
                "globalRouteAutoText",
                "adminRouteAutoText",
                "adminLookupForm",
                "autoProbeJobsBody",
                "providerTransportsBody",
            ):
                if snippet not in admin_page:
                    raise AssertionError(f"admin page is missing {snippet!r}")
            for forbidden in ("Auto Candidate Lists", "autoCandidatesForm"):
                if forbidden in admin_page:
                    raise AssertionError(f"admin page exposes obsolete Auto candidate UI: {forbidden}")
            admin_payload = fetch_json_with_opener(opener, f"{base_url}/api/admin")
            for key in ("servers", "users", "routes", "auto_candidates", "service_aliases"):
                if key not in admin_payload:
                    raise AssertionError(f"/api/admin is missing {key!r}")
            winners = fetch_json_with_opener(opener, f"{base_url}/api/admin/auto-winners?target=telegram&limit=10")
            if "winners" not in winners:
                raise AssertionError(f"auto winners payload is malformed: {winners!r}")
            lookup = fetch_json_with_opener(opener, f"{base_url}/api/route-lookup?target=216.239.36.21")
            results = lookup.get("results") or []
            if not results or results[0].get("route_state") != "direct":
                raise AssertionError(f"route lookup should report direct for unmanaged IP: {lookup!r}")

            status_raw = subprocess.check_output(
                [
                    sys.executable,
                    str(ROOT / "tools" / "vpn_control_app.py"),
                    "--db",
                    str(db_path),
                    "--inventory",
                    str(INVENTORY),
                    "system-status",
                    "--json",
                    "--strict",
                ],
                cwd=ROOT,
                text=True,
            )
            status = json.loads(status_raw)
            workers = status.get("workers") or {}
            for name in ("auto_probe", "provider_refresh"):
                if name not in workers:
                    raise AssertionError(f"{name} worker status was not persisted")
                if workers[name].get("enabled") is not False:
                    raise AssertionError(f"{name} worker status should be disabled: {workers[name]!r}")
        finally:
            proc.terminate()
            try:
                output, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate(timeout=10)
            if proc.returncode not in (0, -15, 1):
                print(output or "")
                raise RuntimeError(f"control server exited unexpectedly: {proc.returncode}")

    print("Control server HTTP smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
