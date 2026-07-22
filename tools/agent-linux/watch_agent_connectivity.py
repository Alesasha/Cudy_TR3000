#!/usr/bin/env python3
"""Independent Linux safety watchdog for the managed route agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "run"
LOG_DIR = ROOT / "logs"
STATE_PATH = RUN_DIR / "watchdog-state.json"
PENDING_PATH = RUN_DIR / "watchdog-report-pending.json"
TRIPPED_PATH = RUN_DIR / "watchdog-tripped.json"
CONFIG_PATH = RUN_DIR / "fresh-config.json"
LOCAL_SERVICES_PATH = ROOT / "watchdog-services.json"
ENV_PATH = ROOT / "agent.env"
BASE_URLS = (
    "https://connectivitycheck.gstatic.com/generate_204",
    "https://www.msftconnecttest.com/connecttest.txt",
    "https://ifconfig.me/ip",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(message: str, level: str = "INFO") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with (LOG_DIR / "watchdog.log").open("a", encoding="utf-8") as fh:
        fh.write(f"[{utc_now()}] [{level}] {message}\n")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def load_env() -> dict[str, str]:
    result = dict(os.environ)
    try:
        for raw in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip().rstrip("\r")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result.setdefault(key.strip(), value.strip().strip("'\""))
    except OSError:
        pass
    port = result.get("CONTROL_LOCAL_PORT", "18765").strip().rstrip("\r")
    result.setdefault("VPN_CONTROL_URL", f"http://127.0.0.1:{port}")
    return result


def tcp_probe(host: str = "1.1.1.1", port: int = 443) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3.5):
            return True
    except OSError:
        return False


def web_probe(url: str, success_pattern: str = "", failure_pattern: str = "") -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="cudy-watchdog-", delete=False) as temp:
        path = Path(temp.name)
    try:
        result = subprocess.run(
            [
                "curl", "-4", "--location", "--silent", "--show-error",
                "--connect-timeout", "4", "--max-time", "10", "--range", "0-262143",
                "--output", str(path), "--write-out", "%{http_code}", url,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        try:
            http_code = int((result.stdout or "0").strip())
        except ValueError:
            http_code = 0
        body = path.read_text(encoding="utf-8", errors="replace")[:262144] if path.exists() else ""
        try:
            success_matched = not success_pattern or re.search(success_pattern, body, re.IGNORECASE | re.MULTILINE) is not None
            failure_matched = bool(failure_pattern and re.search(failure_pattern, body, re.IGNORECASE | re.MULTILINE))
        except re.error:
            success_matched = False
            failure_matched = True
        return {
            "ok": result.returncode == 0 and http_code > 0 and success_matched and not failure_matched,
            "http_code": http_code,
            "success_matched": success_matched,
            "failure_matched": failure_matched,
        }
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "http_code": 0, "success_matched": False, "failure_matched": False}
    finally:
        path.unlink(missing_ok=True)


def service_targets(item: dict[str, Any], source: str) -> list[dict[str, Any]]:
    targets = item.get("targets") or ([item.get("url")] if item.get("url") else [])
    name = str(item.get("label") or item.get("name") or item.get("service_key") or "service")
    return [
        {
            "name": name,
            "url": str(url),
            "critical": item.get("critical") is not False,
            "success_pattern": str(item.get("success_pattern") or ""),
            "failure_pattern": str(item.get("failure_pattern") or ""),
            "source": source,
        }
        for url in targets
        if str(url or "")
    ]


def load_services() -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    managed = read_json(CONFIG_PATH, {})
    for item in managed.get("critical_services") or []:
        if isinstance(item, dict):
            services.extend(service_targets(item, "control-server"))
    local = read_json(LOCAL_SERVICES_PATH, {})
    for item in local.get("services") or []:
        if isinstance(item, dict):
            services.extend(service_targets(item, "local"))
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in services:
        deduped[(item["name"], item["url"])] = item
    return list(deduped.values())


def check_connectivity() -> dict[str, Any]:
    probes: list[dict[str, Any]] = [{"target": "tcp://1.1.1.1:443", "ok": tcp_probe()}]
    base_ok = False
    for url in BASE_URLS:
        probe = web_probe(url)
        probes.append({"target": url, **probe})
        if probe["ok"]:
            base_ok = True
            break
    grouped: dict[str, list[dict[str, Any]]] = {}
    for service in load_services():
        grouped.setdefault(service["name"], []).append(service)
    failed: list[str] = []
    for name, targets in grouped.items():
        service_ok = False
        critical = any(bool(item["critical"]) for item in targets)
        for item in targets:
            probe = web_probe(item["url"], item["success_pattern"], item["failure_pattern"])
            probes.append({"target": item["url"], "name": name, "critical": item["critical"], "source": item["source"], **probe})
            service_ok = service_ok or bool(probe["ok"])
        if critical and not service_ok:
            failed.append(name)
    return {
        "ok": base_ok and not failed,
        "base_internet_ok": base_ok,
        "critical_services_ok": not failed,
        "failed_services": failed,
        "probes": probes,
    }


def send_report(report: dict[str, Any], env: dict[str, str]) -> bool:
    token = env.get("VPN_AGENT_TOKEN", "")
    control = env.get("VPN_CONTROL_URL", "").rstrip("/")
    if not token or not control:
        return False
    body = json.dumps(
        {"summary": "Linux agent watchdog: critical connectivity failure", "report": json.dumps(report, ensure_ascii=False, indent=2)},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        control + "/api/agent/diagnostics",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


def service_enabled(name: str) -> bool:
    result = subprocess.run(["systemctl", "is-enabled", name], capture_output=True, text=True, check=False)
    return result.returncode == 0 and result.stdout.strip() == "enabled"


def service_active(name: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", "--quiet", name], check=False)
    return result.returncode == 0


def recover_enabled_service(name: str) -> bool:
    if service_active(name):
        return True
    log(f"Enabled agent service is inactive; attempting restart: {name}", "WARN")
    subprocess.run(["systemctl", "restart", name], check=False)
    for _ in range(15):
        if service_active(name):
            log(f"Agent service recovered: {name}")
            return True
        time.sleep(1)
    log(f"Agent service restart failed: {name}", "ERROR")
    return False


def emergency_suspend(service_name: str, retry_seconds: int) -> None:
    # Keep the unit enabled. A safety recovery must never silently turn off
    # autostart; the watchdog will retry the agent after a direct-routing
    # cooldown.
    subprocess.run(["systemctl", "stop", service_name], check=False)
    subprocess.run([str(ROOT / "restore_direct.sh")], cwd=ROOT, check=False)
    trip = read_json(TRIPPED_PATH, {})
    if not isinstance(trip, dict):
        trip = {}
    trip.update(
        {
            "tripped_at": str(trip.get("tripped_at") or utc_now()),
            "retry_after_epoch": int(time.time()) + max(60, retry_seconds),
            "reason": "base_connectivity_failed",
        }
    )
    write_json(TRIPPED_PATH, trip)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-service", default="cudy-managed-agent.service")
    parser.add_argument("--failure-threshold", type=int, default=3)
    parser.add_argument("--retry-seconds", type=int, default=60)
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if args.probe_only:
        result = check_connectivity()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    if not service_enabled(args.agent_service):
        write_json(STATE_PATH, {"consecutive_failures": 0, "last_result": "agent_disabled", "updated_at": utc_now()})
        return 0

    trip = read_json(TRIPPED_PATH, {})
    retry_after = int(trip.get("retry_after_epoch") or 0) if isinstance(trip, dict) else 0
    if retry_after > int(time.time()):
        write_json(
            STATE_PATH,
            {
                "consecutive_failures": 0,
                "last_result": "direct_recovery_cooldown",
                "retry_after_epoch": retry_after,
                "updated_at": utc_now(),
            },
        )
        return 0
    if trip:
        TRIPPED_PATH.unlink(missing_ok=True)
        log("Direct-routing cooldown completed; retrying the enabled agent.")

    if not recover_enabled_service(args.agent_service):
        result = {
            "ok": False,
            "base_internet_ok": False,
            "critical_services_ok": False,
            "failed_services": ["agent-service"],
            "probes": [],
        }
    else:
        result = check_connectivity()
    state = read_json(STATE_PATH, {})
    failures = int(state.get("consecutive_failures") or 0)
    env = load_env()
    if result["base_internet_ok"]:
        if failures:
            log("Base connectivity recovered before emergency action.")
        service_failures = 0 if result["critical_services_ok"] else int(state.get("consecutive_service_failures") or 0) + 1
        state = {
            "consecutive_failures": 0,
            "consecutive_service_failures": service_failures,
            "last_result": "healthy" if result["critical_services_ok"] else "critical_service_failed",
            "last_success_at": utc_now(),
            "failed_services": result["failed_services"],
            "probes": result["probes"],
        }
        if service_failures == max(1, args.failure_threshold):
            report = {
                "reported_at": utc_now(),
                "reason": "critical_service_failed",
                **state,
            }
            if not send_report(report, env):
                write_json(PENDING_PATH, report)
            log(
                "Critical service probe threshold reached; reporting without stopping the agent: "
                + ",".join(result["failed_services"]),
                "WARN",
            )
        if PENDING_PATH.exists():
            pending = read_json(PENDING_PATH, {})
            if pending and send_report(pending, env):
                PENDING_PATH.unlink(missing_ok=True)
                log("Queued watchdog report delivered to control-server.")
        write_json(STATE_PATH, state)
        return 0
    failures += 1
    state = {
        "consecutive_failures": failures,
        "last_result": "connectivity_failed",
        "last_failure_at": utc_now(),
        "failed_services": result["failed_services"],
        "probes": result["probes"],
    }
    write_json(STATE_PATH, state)
    log(f"Connectivity failure {failures}/{args.failure_threshold}; failed_services={','.join(result['failed_services'])}", "WARN")
    if failures < max(1, args.failure_threshold):
        # A transient probe failure is recorded in state/logs, but the
        # systemd oneshot itself remains healthy so the timer is not marked
        # failed on every temporary network interruption.
        return 0
    trip = {"tripped_at": utc_now(), "reason": "base_connectivity_failed", **state}
    write_json(TRIPPED_PATH, trip)
    if not send_report(trip, env):
        write_json(PENDING_PATH, trip)
    log("Base connectivity failure threshold reached; suspending agent and restoring direct routing.", "ERROR")
    emergency_suspend(args.agent_service, args.retry_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
