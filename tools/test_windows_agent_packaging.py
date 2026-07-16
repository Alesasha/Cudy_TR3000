#!/usr/bin/env python3
"""Regression checks for Windows agent update/packaging scripts."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "tools" / "agent-windows"
BUILD_SCRIPT = ROOT / "tools" / "Build-WindowsAgentPackage.ps1"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def assert_contains(text: str, needle: str, *, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"{label} missing expected snippet: {needle}")


def main() -> int:
    starter = read_text(AGENT_DIR / "Start-ManagedAgent.ps1")
    updater = read_text(AGENT_DIR / "Update-AgentPackage.ps1")
    env_example = read_text(AGENT_DIR / "agent.env.ps1.example")
    watchdog = read_text(AGENT_DIR / "Watch-AgentConnectivity.ps1")
    watchdog_installer = read_text(AGENT_DIR / "Install-AgentWatchdogTask.ps1")
    watchdog_example = read_text(AGENT_DIR / "watchdog-services.json.example")
    maintenance_start = read_text(AGENT_DIR / "Start-OpenAIMaintenanceTunnel.ps1")
    maintenance_update = read_text(AGENT_DIR / "Update-OpenAIMaintenanceRoutes.ps1")
    maintenance_stop = read_text(AGENT_DIR / "Stop-OpenAIMaintenanceTunnel.ps1")
    maintenance_installer = read_text(AGENT_DIR / "Install-OpenAIMaintenanceRefreshTask.ps1")
    builder = read_text(BUILD_SCRIPT)

    assert_contains(starter, "Invoke-AgentSelfUpdate", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "Update-AgentPackage.ps1", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "-FromAgent", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "exit=$exitCode", label="Start-ManagedAgent.ps1")

    assert_contains(updater, "/api/agent/app-version?platform=", label="Update-AgentPackage.ps1")
    assert_contains(updater, "Expand-UpdateArchive", label="Update-AgentPackage.ps1")
    assert_contains(updater, "Copy-UpdateFiles", label="Update-AgentPackage.ps1")
    assert_contains(updater, "agent.env.ps1", label="Update-AgentPackage.ps1")
    assert_contains(updater, "uswest_control_tunnel_ed25519", label="Update-AgentPackage.ps1")
    assert_contains(updater, "exit 10", label="Update-AgentPackage.ps1")

    assert_contains(env_example, "AGENT_AUTO_UPDATE", label="agent.env.ps1.example")
    assert_contains(env_example, "AGENT_VERSION_CODE", label="agent.env.ps1.example")
    assert_contains(starter, "Write-AgentHeartbeat", label="Start-ManagedAgent.ps1")
    assert_contains(watchdog, "watchdog.armed", label="Watch-AgentConnectivity.ps1")
    assert_contains(watchdog, "watchdog.keepalive", label="Watch-AgentConnectivity.ps1")
    assert_contains(watchdog, "Emergency-Stop-Agent.ps1", label="Watch-AgentConnectivity.ps1")
    assert_contains(watchdog, "RequiredDevelopmentUrl", label="Watch-AgentConnectivity.ps1")
    assert_contains(watchdog, "Get-CriticalServices", label="Watch-AgentConnectivity.ps1")
    assert_contains(watchdog, "/api/agent/diagnostics", label="Watch-AgentConnectivity.ps1")
    assert_contains(watchdog_installer, '"SYSTEM"', label="Install-AgentWatchdogTask.ps1")
    assert_contains(watchdog_installer, "CriticalService", label="Install-AgentWatchdogTask.ps1")
    assert_contains(watchdog_example, '"services"', label="watchdog-services.json.example")
    assert_contains(
        maintenance_start,
        "Standalone AmneziaWG executable is required",
        label="Start-OpenAIMaintenanceTunnel.ps1",
    )
    assert_contains(
        maintenance_start,
        "endpoint_route_owned = $endpointRouteOwned",
        label="Start-OpenAIMaintenanceTunnel.ps1",
    )
    assert_contains(
        maintenance_start,
        "AWG endpoint pinned:",
        label="Start-OpenAIMaintenanceTunnel.ps1",
    )
    assert_contains(
        maintenance_start,
        "$managedRoutes",
        label="Start-OpenAIMaintenanceTunnel.ps1 transactional rollback",
    )
    assert_contains(
        maintenance_start,
        "$oldStateInvalidated",
        label="Start-OpenAIMaintenanceTunnel.ps1 transactional rollback",
    )
    if "Start-AwgTransport.ps1" in maintenance_start:
        raise AssertionError("OpenAI maintenance tunnel must not share the generic AWG backend")
    assert_contains(
        maintenance_update,
        "endpoint_interface_index",
        label="Update-OpenAIMaintenanceRoutes.ps1",
    )
    assert_contains(
        maintenance_stop,
        "endpoint_route_owned",
        label="Stop-OpenAIMaintenanceTunnel.ps1",
    )
    assert_contains(
        maintenance_installer,
        'Start-ScheduledTask -TaskName $taskName',
        label="Install-OpenAIMaintenanceRefreshTask.ps1",
    )
    assert_contains(
        maintenance_installer,
        "LastTaskResult -ne 0",
        label="Install-OpenAIMaintenanceRefreshTask.ps1",
    )
    assert_contains(builder, '"Watch-AgentConnectivity.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Install-AgentWatchdogTask.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Update-AgentPackage.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"agent.version.json"', label="Build-WindowsAgentPackage.ps1")

    print("Windows agent packaging regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
