#!/usr/bin/env python3
"""Static regression checks for the Windows Cudy maintenance guard."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START = (ROOT / "tools" / "Start-CudyMaintenanceGuard.ps1").read_text(encoding="utf-8-sig")
STOP = (ROOT / "tools" / "Stop-CudyMaintenanceGuard.ps1").read_text(encoding="utf-8-sig")


def main() -> int:
    required_start = (
        "Enable-WifiPath",
        "Find-TunnelEndpoint",
        "Set-EndpointRoute",
        "Test-Guard",
        "Find-NetRoute -RemoteIPAddress $CudyAddress",
        "-WindowStyle Hidden",
        "maintenance-guard",
        "arm failed:",
    )
    required_stop = (
        "Remove-NetRoute",
        "Stop-Process",
        "maintenance-guard",
    )
    for needle in required_start:
        assert needle in START, f"start guard is missing {needle!r}"
    for needle in required_stop:
        assert needle in STOP, f"stop guard is missing {needle!r}"
    assert "Remove-NetRoute" not in START.split("function Set-EndpointRoute", 1)[0]
    assert "$routeAdded" in START
    print("Cudy maintenance guard regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
