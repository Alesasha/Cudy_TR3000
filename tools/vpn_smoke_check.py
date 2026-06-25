#!/usr/bin/env python3
"""Run repeatable smoke checks for the VPN control project.

The default mode is read-only and does not change Cudy. Use --online to include
SSH-based Cudy checks; those still avoid applying changes.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]
    online: bool = False
    required: bool = True
    timeout: int = 60


CHECKS = [
    Check("inventory validates", ["tools/vpn_inventory.py", "validate"]),
    Check("user-visible inventory lists", ["tools/vpn_inventory.py", "list"]),
    Check("admin inventory lists", ["tools/vpn_inventory.py", "admin-list", "--include-disabled"]),
    Check("database summary loads", ["tools/vpn_control_app.py", "summary"]),
    Check("system status alias loads", ["tools/vpn_control_app.py", "status"]),
    Check("control server HTTP smoke", ["tools/test_control_server_http.py"]),
    Check("effective route plan builds", ["tools/vpn_control_app.py", "route-plan"]),
    Check("Auto priority policy regression", ["tools/test_auto_policy_priority.py"]),
    Check("provider parsing regression", ["tools/test_provider_parsing.py"]),
    Check("Cudy runtime parsing regression", ["tools/test_vpn_inventory_runtime.py"]),
    Check("auto cache lists", ["tools/vpn_control_app.py", "auto-cache-list"]),
    Check("Auto priority policies list", ["tools/vpn_control_app.py", "auto-candidates-list"]),
    Check("Auto winners list", ["tools/vpn_control_app.py", "auto-winners", "ifconfig.me"]),
    Check("route lookup resolves service alias", ["tools/vpn_control_app.py", "route-lookup", "telegram", "--user-id", "isasha_X7Pro_Cudy"]),
    Check("route lookup reports direct", ["tools/vpn_control_app.py", "route-lookup", "216.239.36.21", "--user-id", "isasha_X7Pro_Cudy"]),
    Check("service alias CLI regression", ["tools/test_service_alias_cli.py"]),
    Check("Cudy client lifecycle regression", ["tools/test_cudy_client_lifecycle.py"]),
    Check("Cudy router migration generator regression", ["tools/test_generate_cudy_router_migration.py"]),
    Check("Cudy router preflight regression", ["tools/test_cudy_router_preflight.py"]),
    Check("domain discovery regression", ["tools/test_domain_discovery.py"]),
    Check("combined route deploy dry-run builds", ["tools/vpn_control_app.py", "deploy-routes"]),
    Check("provider refresh dry-run builds", ["tools/vpn_inventory.py", "refresh-provider", "all"]),
    Check("route agent help loads", ["tools/route_agent.py", "--help"]),
    Check("route agent plan regression", ["tools/test_route_agent_plan.py"]),
    Check("PowerShell scripts parse", ["tools/check_powershell_syntax.py"], timeout=120),
    Check("shell scripts parse", ["tools/check_shell_syntax.py"]),
    Check("control backup help loads", ["tools/backup_control_server.py", "--help"]),
    Check("control tunnel-user backup help loads", ["tools/backup_control_server_via_tunnel_user.py", "--help"]),
    Check("control clone help loads", ["tools/clone_control_server.py", "--help"]),
    Check("control VPS bootstrap help loads", ["tools/bootstrap_control_vps.py", "--help"]),
    Check("control backup/fallback artifact regression", ["tools/test_control_backup_artifacts.py"]),
    Check("Cudy fallback status checks", ["tools/check_cudy_fallback_status.py", "--strict"], timeout=30),
    Check("Production control-server checks", ["tools/check_control_server_prod.py", "--strict"], online=True, timeout=90),
    Check("Cudy runtime snapshot refreshes", ["tools/vpn_inventory.py", "refresh-cudy"], online=True, timeout=120),
    Check("Cudy provider refresh schedule checks", ["tools/vpn_inventory.py", "check-provider-schedule"], online=True),
    Check("Cudy user-route status reads", ["tools/vpn_control_app.py", "status-user-routes"], online=True, timeout=60),
    Check(
        "Auto selector probes candidates",
        ["tools/vpn_control_app.py", "auto-select", "example.com", "--candidates", "proxyde,uswest", "--max-time", "8"],
        online=True,
        timeout=60,
    ),
]


def run_check(check: Check) -> tuple[bool, str]:
    command = [sys.executable, *check.command]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=check.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return False, f"timeout after {check.timeout}s\n{output}".strip()

    return result.returncode == 0, result.stdout.strip()


def compact_output(output: str, max_lines: int) -> str:
    lines = output.splitlines()
    if len(lines) <= max_lines:
        return output
    kept = lines[:max_lines]
    kept.append(f"... ({len(lines) - max_lines} more lines)")
    return "\n".join(kept)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run VPN control project smoke checks.")
    parser.add_argument("--online", action="store_true", help="Include SSH checks against Cudy.")
    parser.add_argument("--verbose", action="store_true", help="Print full command output.")
    parser.add_argument("--max-lines", type=int, default=8, help="Output lines per check in compact mode.")
    args = parser.parse_args()

    failures: list[str] = []
    selected = [check for check in CHECKS if args.online or not check.online]

    for check in selected:
        ok, output = run_check(check)
        status = "PASS" if ok else "FAIL"
        scope = "online" if check.online else "local"
        print(f"[{status}] {check.name} ({scope})")
        if output:
            rendered = output if args.verbose else compact_output(output, args.max_lines)
            for line in rendered.splitlines():
                print(f"  {line}")
        if not ok and check.required:
            failures.append(check.name)

    skipped = [check.name for check in CHECKS if check.online and not args.online]
    if skipped:
        print()
        print("Skipped online checks. Re-run with --online to include:")
        for name in skipped:
            print(f"  - {name}")

    if failures:
        print()
        print("Smoke check failed:")
        for name in failures:
            print(f"  - {name}")
        return 1

    print()
    print("Smoke check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
