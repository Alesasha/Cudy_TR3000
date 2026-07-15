#!/usr/bin/env python3
"""Local regression checks for route_agent plan behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from argparse import Namespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import route_agent  # noqa: E402


def main() -> int:
    parsed = route_agent.build_parser().parse_args(["apply", "--can-manage-transports", "--yes"])
    if not parsed.can_manage_transports:
        raise AssertionError("managed platform wrappers must be able to declare transport capability")
    original_request_json_failover = route_agent.request_json_failover
    original_load_token = route_agent.load_token
    original_is_windows = route_agent.is_windows
    try:
        captured = {}
        route_agent.load_token = lambda args: "test-token"  # type: ignore[assignment]
        route_agent.is_windows = lambda: False  # type: ignore[assignment]
        route_agent.request_json_failover = lambda args, path, **kwargs: captured.update(kwargs["data"]) or {"ok": True}  # type: ignore[assignment]
        route_agent.post_status(Namespace(can_manage_transports=True, status_mode="apply"), {})
        if not captured["capabilities"]["can_manage_transports"]:
            raise AssertionError("explicit Linux wrapper capability was not posted")
    finally:
        route_agent.request_json_failover = original_request_json_failover  # type: ignore[assignment]
        route_agent.load_token = original_load_token  # type: ignore[assignment]
        route_agent.is_windows = original_is_windows  # type: ignore[assignment]

    action, command = route_agent.windows_route_command_for_ip(
        ip="203.0.113.1",
        server_id="auto",
        interface_map={},
        default_route={"interface_index": "14", "via": "192.168.8.1"},
    )
    if action != "direct" or "--interface-map" in command or "192.168.8.1" not in command:
        raise AssertionError(f"Windows unresolved auto should be direct, got {action=} {command=}")
    action, command = route_agent.route_command_for_ip(
        ip="203.0.113.1",
        server_id="auto",
        interface_map={},
        default_route={"dev": "eth0", "via": "192.168.1.1"},
    )
    if action != "direct" or "--interface-map" in command or "192.168.1.1" not in command:
        raise AssertionError(f"Linux unresolved auto should be direct, got {action=} {command=}")

    original_plan_commands = route_agent.plan_commands
    try:
        route_agent.plan_commands = lambda plan: ([], [""])  # type: ignore[assignment]
        result = route_agent.apply_plan({"platform": "windows"}, yes=True, direct_baseline=False)
        if result.get("apply_errors") != []:
            raise AssertionError(f"blank blockers should not produce apply errors: {result!r}")
    finally:
        route_agent.plan_commands = original_plan_commands  # type: ignore[assignment]

    original_run_powershell_file = route_agent.run_powershell_file
    try:
        route_agent.run_powershell_file = lambda script, timeout: (  # type: ignore[assignment]
            0,
            '[{"Index":0,"Ok":true,"Output":"ok"},{"Index":1,"Ok":false,"Output":"missing"}]',
        )
        applied = route_agent.run_windows_route_batch(
            ["powershell:Write-Output ok", "optional:powershell:throw 'missing'"]
        )
        if not applied[0]["ok"] or not applied[1]["ok"] or "ignored cleanup failure" not in applied[1]["output"]:
            raise AssertionError(f"Windows route batch result mapping failed: {applied!r}")
    finally:
        route_agent.run_powershell_file = original_run_powershell_file  # type: ignore[assignment]

    original_run_text = route_agent.run_text
    original_probe_bind_value = route_agent.probe_bind_value
    try:
        route_agent.probe_bind_value = lambda interface_name: interface_name  # type: ignore[assignment]
        route_agent.run_text = lambda command, timeout: (  # type: ignore[assignment]
            28,
            "curl: (28) Connection timed out after 5002 milliseconds\n"
            "http_code=301\n"
            "time_total=5.535081\n"
            "remote_ip=151.101.130.219\n"
            "size_download=0\n"
            "speed_download=0\n",
        )
        probe = route_agent.curl_probe(
            url="https://speedtest.net/",
            interface_name="proxyde",
            connect_timeout=5,
            max_time=12,
        )
        if not probe.get("ok") or probe.get("http_code_int") != 301:
            raise AssertionError(f"3xx HTTP probe should count as route-reachable: {probe!r}")
    finally:
        route_agent.run_text = original_run_text  # type: ignore[assignment]
        route_agent.probe_bind_value = original_probe_bind_value  # type: ignore[assignment]

    original_run_text = route_agent.run_text
    original_probe_bind_value = route_agent.probe_bind_value
    try:
        route_agent.probe_bind_value = lambda interface_name: ""  # type: ignore[assignment]

        def fake_geo_block_run(command: list[str], timeout: int) -> tuple[int, str]:
            body_path = Path(command[command.index("-o") + 1])
            body_path.write_text("Gemini isn't currently supported in your country. Stay tuned!", encoding="utf-8")
            return (
                0,
                "http_code=200\n"
                "time_total=0.200000\n"
                "remote_ip=142.250.1.1\n"
                "size_download=64\n"
                "speed_download=1024\n",
            )

        route_agent.run_text = fake_geo_block_run  # type: ignore[assignment]
        probe = route_agent.curl_probe(
            url="https://gemini.google.com/",
            interface_name="proxyfast",
            connect_timeout=5,
            max_time=12,
        )
        if probe.get("ok") or probe.get("semantic_status") != "geo_blocked":
            raise AssertionError(f"Gemini geo-block body must reject the candidate: {probe!r}")
    finally:
        route_agent.run_text = original_run_text  # type: ignore[assignment]
        route_agent.probe_bind_value = original_probe_bind_value  # type: ignore[assignment]

    semantic = {"ok": True}
    route_agent.apply_semantic_probe_check(
        semantic,
        url="https://example.com/",
        body_text="Access denied for this region",
        success_pattern=r"access|ready",
        failure_pattern=r"denied\s+for\s+this\s+region",
    )
    if semantic.get("ok") or semantic.get("semantic_status") != "failure_pattern":
        raise AssertionError(f"failure regex must reject a nominal HTTP response: {semantic!r}")

    semantic = {"ok": True}
    route_agent.apply_semantic_probe_check(
        semantic,
        url="https://example.com/",
        body_text="Service is warming up",
        success_pattern=r"service\s+ready",
    )
    if semantic.get("ok") or semantic.get("semantic_status") != "success_pattern_missing":
        raise AssertionError(f"missing success regex must reject a nominal HTTP response: {semantic!r}")

    print("Route agent plan regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
