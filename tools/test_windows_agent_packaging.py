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
    ui = read_text(AGENT_DIR / "Cudy-Agent.ps1")
    state = read_text(AGENT_DIR / "Set-AgentState.ps1")
    ui_installer = read_text(AGENT_DIR / "Install-AgentUi.ps1")
    app_installer = read_text(AGENT_DIR / "Install-UniversalAgent.ps1")
    app_registration = read_text(AGENT_DIR / "Register-CudyAgentInstallation.ps1")
    app_uninstaller = read_text(AGENT_DIR / "Uninstall-CudyAgent.ps1")
    update_status = read_text(AGENT_DIR / "Get-AgentUpdateStatus.ps1")
    ui_status = read_text(AGENT_DIR / "Get-AgentUiStatus.ps1")
    builder = read_text(BUILD_SCRIPT)

    assert_contains(starter, "Invoke-AgentSelfUpdate", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "Update-AgentPackage.ps1", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "-FromAgent", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "exit=$exitCode", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "UpdateCheckSeconds", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "background self-update check started", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "Save-AuthenticatedControlEndpoint", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "run\\control-endpoint.json", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "ExpectedHostKeySha256", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "Desktop UI shortcut repair failed", label="Start-ManagedAgent.ps1")
    assert_contains(starter, "Install-AgentUi.ps1", label="Start-ManagedAgent.ps1")

    tunnel = read_text(AGENT_DIR / "Start-Tunnel.ps1")
    assert_contains(tunnel, "Confirm-ControlHostKey", label="Start-Tunnel.ps1")
    assert_contains(tunnel, "ssh-keyscan.exe", label="Start-Tunnel.ps1")
    assert_contains(tunnel, "Control-server SSH key mismatch", label="Start-Tunnel.ps1")

    assert_contains(updater, "/api/agent/app-version?platform=", label="Update-AgentPackage.ps1")
    assert_contains(updater, "Expand-UpdateArchive", label="Update-AgentPackage.ps1")
    assert_contains(updater, "Copy-UpdateFiles", label="Update-AgentPackage.ps1")
    assert_contains(updater, "agent.env.ps1", label="Update-AgentPackage.ps1")
    assert_contains(updater, "uswest_control_tunnel_ed25519", label="Update-AgentPackage.ps1")
    assert_contains(updater, "waiting for user approval", label="Update-AgentPackage.ps1")
    assert_contains(updater, "Get-FileHash", label="Update-AgentPackage.ps1")
    assert_contains(updater, "Install-AgentUi.ps1", label="Update-AgentPackage.ps1")
    assert_contains(updater, "migrated to", label="Update-AgentPackage.ps1")
    assert_contains(updater, "Remove-Item -LiteralPath $WorkDir", label="Update-AgentPackage.ps1")

    assert_contains(env_example, "AGENT_AUTO_UPDATE", label="agent.env.ps1.example")
    assert_contains(env_example, "AGENT_UPDATE_CHECK_SECONDS", label="agent.env.ps1.example")
    assert_contains(env_example, "AGENT_VERSION_CODE", label="agent.env.ps1.example")
    assert_contains(env_example, "VPN_CONTROL_PRIMARY_SSH_HOST_KEY_SHA256", label="agent.env.ps1.example")
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
        maintenance_update,
        "endpoint_wifi_profile",
        label="Update-OpenAIMaintenanceRoutes.ps1",
    )
    assert_contains(
        maintenance_update,
        "$currentEndpointIndex",
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
    assert_contains(
        maintenance_installer,
        '[string]$WiFiProfile',
        label="Install-OpenAIMaintenanceRefreshTask.ps1",
    )
    assert_contains(builder, '"Watch-AgentConnectivity.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Install-AgentWatchdogTask.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Update-AgentPackage.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"agent.version.json"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(ui, "Connection is healthy", label="Cudy-Agent.ps1")
    assert_contains(ui, "Closing this window does not stop the agent", label="Cudy-Agent.ps1")
    assert_contains(ui, "Start-StateChange", label="Cudy-Agent.ps1")
    assert_contains(ui, "Start-AgentUpdate", label="Cudy-Agent.ps1")
    assert_contains(ui, "Restart-AgentUi.ps1", label="Cudy-Agent.ps1")
    assert_contains(ui, "[switch]$SmokeTest", label="Cudy-Agent.ps1")
    assert_contains(state, "Emergency-Stop-Agent.ps1", label="Set-AgentState.ps1")
    assert_contains(state, "Enable-ScheduledTask", label="Set-AgentState.ps1")
    assert_contains(ui_installer, "Cudy Agent.lnk", label="Install-AgentUi.ps1")
    assert_contains(ui_installer, "-WindowStyle Hidden", label="Install-AgentUi.ps1")
    assert_contains(ui_installer, "CommonDesktopDirectory", label="Install-AgentUi.ps1")
    assert_contains(app_installer, 'Join-Path $env:ProgramFiles "Cudy Agent"', label="Install-UniversalAgent.ps1")
    assert_contains(app_installer, "Register-CudyAgentInstallation.ps1", label="Install-UniversalAgent.ps1")
    assert_contains(app_registration, "UninstallString", label="Register-CudyAgentInstallation.ps1")
    assert_contains(app_uninstaller, "Uninstall-ManagedAgentTask.ps1", label="Uninstall-CudyAgent.ps1")
    restarter = read_text(AGENT_DIR / "Restart-AgentUi.ps1")
    assert_contains(restarter, "-WindowStyle Hidden", label="Restart-AgentUi.ps1")
    assert_contains(update_status, "/api/agent/app-version?platform=windows", label="Get-AgentUpdateStatus.ps1")
    assert_contains(ui_status, "control_connected", label="Get-AgentUiStatus.ps1")
    assert_contains(ui, "Start-StatusRefresh", label="Cudy-Agent.ps1")
    assert_contains(builder, '"Cudy-Agent.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Enroll-Agent.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Install-UniversalAgent.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Register-CudyAgentInstallation.ps1"', label="Build-WindowsAgentPackage.ps1")
    assert_contains(builder, '"Uninstall-CudyAgent.ps1"', label="Build-WindowsAgentPackage.ps1")

    version_file_check = updater.index('if (Test-Path -LiteralPath $VersionFile)')
    env_version_check = updater.index('if ($env:AGENT_VERSION_CODE)')
    if version_file_check > env_version_check:
        raise AssertionError("Update-AgentPackage.ps1 must prefer agent.version.json over AGENT_VERSION_CODE")

    print("Windows agent packaging regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
