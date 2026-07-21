#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "agent-linux" / "watch_agent_connectivity.py"


def load_module():
    spec = importlib.util.spec_from_file_location("linux_watchdog_under_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Linux watchdog module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_paths(module, root: Path) -> None:
    module.RUN_DIR = root / "run"
    module.LOG_DIR = root / "logs"
    module.STATE_PATH = module.RUN_DIR / "watchdog-state.json"
    module.PENDING_PATH = module.RUN_DIR / "watchdog-report-pending.json"
    module.TRIPPED_PATH = module.RUN_DIR / "watchdog-tripped.json"


def result(*, base_ok: bool, services_ok: bool) -> dict:
    return {
        "ok": base_ok and services_ok,
        "base_internet_ok": base_ok,
        "critical_services_ok": services_ok,
        "failed_services": [] if services_ok else ["Telegram"],
        "probes": [],
    }


def run_main(module, *args: str) -> int:
    with patch.object(sys, "argv", [str(MODULE_PATH), *args]):
        return module.main()


def main() -> int:
    module = load_module()
    with tempfile.TemporaryDirectory(prefix="cudy-linux-watchdog-") as temp:
        configure_paths(module, Path(temp))

        with (
            patch.object(module, "service_enabled", return_value=True),
            patch.object(module, "recover_enabled_service", return_value=True),
            patch.object(module, "check_connectivity", return_value=result(base_ok=True, services_ok=False)),
            patch.object(module, "send_report", return_value=True),
            patch.object(module, "load_env", return_value={}),
            patch.object(module, "log"),
            patch.object(module, "emergency_suspend") as suspend,
        ):
            assert run_main(module, "--failure-threshold", "1") == 0
            suspend.assert_not_called()
        state = json.loads(module.STATE_PATH.read_text(encoding="utf-8"))
        assert state["last_result"] == "critical_service_failed"
        assert state["consecutive_failures"] == 0

        module.STATE_PATH.unlink()
        with (
            patch.object(module, "service_enabled", return_value=True),
            patch.object(module, "recover_enabled_service", return_value=True),
            patch.object(module, "check_connectivity", return_value=result(base_ok=False, services_ok=False)),
            patch.object(module, "send_report", return_value=True),
            patch.object(module, "load_env", return_value={}),
            patch.object(module, "log"),
            patch.object(module, "emergency_suspend") as suspend,
        ):
            assert run_main(module, "--failure-threshold", "1", "--retry-seconds", "240") == 0
            suspend.assert_called_once_with("cudy-managed-agent.service", 240)

    print("Linux watchdog behavior regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
