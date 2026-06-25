#!/usr/bin/env python3
"""Regression checks for service alias CLI and route lookup integration."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "tools" / "vpn_control_app.py"
INVENTORY = ROOT / "config" / "vpn_inventory.json"


def run_cli(db_path: Path, *args: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            str(APP),
            "--db",
            str(db_path),
            "--inventory",
            str(INVENTORY),
            *args,
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(f"CLI failed rc={result.returncode}: {' '.join(args)}\n{result.stdout}")
    return result.stdout


def assert_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def assert_true(value: Any, context: str) -> None:
    if not value:
        raise AssertionError(context)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="service-alias-cli-", ignore_cleanup_errors=True) as tmp:
        db_path = Path(tmp) / "vpn_control.db"
        run_cli(
            db_path,
            "create-user",
            "alias-user",
            "--client-ip",
            "10.77.0.251",
            "--password",
            "alias-test-password",
        )
        created = json.loads(
            run_cli(
                db_path,
                "service-alias-set",
                "testtg",
                "149.154.160.0/20,telegram.org",
                "--label",
                "Test Telegram",
                "--json",
            )
        )
        assert_equal(created["alias"], "testtg", "created alias")
        assert_equal(created["label"], "Test Telegram", "created label")
        assert_equal(created["targets"], ["149.154.160.0/20", "telegram.org"], "created targets")

        aliases = json.loads(run_cli(db_path, "service-alias-list", "--json"))
        assert_true(any(item["alias"] == "testtg" for item in aliases), "created alias should be listed")

        lookup = json.loads(run_cli(db_path, "route-lookup", "testtg", "--user-id", "alias-user", "--json"))
        assert_equal(lookup["alias"]["label"], "Test Telegram", "lookup alias label")
        assert_equal(len(lookup["results"]), 2, "lookup should expand alias targets")
        assert_equal(lookup["results"][0]["target"], "149.154.160.0/20", "lookup first target")
        assert_equal(lookup["results"][1]["target"], "telegram.org", "lookup second target")

        deleted = json.loads(run_cli(db_path, "service-alias-delete", "testtg", "--json"))
        assert_equal(deleted["alias"], "testtg", "deleted alias")
        aliases = json.loads(run_cli(db_path, "service-alias-list", "--json"))
        assert_true(not any(item["alias"] == "testtg" for item in aliases), "deleted alias should not be listed")

    print("Service alias CLI regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
