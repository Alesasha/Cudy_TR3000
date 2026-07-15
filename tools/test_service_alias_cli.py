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
        assert_true(any(item["alias"] == "телеграм" for item in aliases), "telegram Cyrillic alias should be seeded")
        assert_true(any(item["alias"] == "ютуб" for item in aliases), "youtube Cyrillic alias should be seeded")
        assert_true(any(item["alias"] == "gemini" for item in aliases), "Gemini alias should be seeded")
        assert_true(any(item["alias"] == "chatgpt" for item in aliases), "ChatGPT alias should be seeded")
        assert_true(any(item["alias"] == "openai" for item in aliases), "OpenAI alias should be seeded")
        assert_true(any(item["alias"] == "mailru" for item in aliases), "Mail.ru alias should be seeded")
        assert_true(any(item["alias"] == "speedtest" for item in aliases), "Speedtest alias should be seeded")
        assert_true(any(item["alias"] == "linux-mirrors" for item in aliases), "Linux mirrors alias should be seeded")

        lookup = json.loads(run_cli(db_path, "route-lookup", "testtg", "--user-id", "alias-user", "--json"))
        assert_equal(lookup["alias"]["label"], "Test Telegram", "lookup alias label")
        assert_equal(len(lookup["results"]), 2, "lookup should expand alias targets")
        assert_equal(lookup["results"][0]["target"], "149.154.160.0/20", "lookup first target")
        assert_equal(lookup["results"][1]["target"], "telegram.org", "lookup second target")

        gemini_lookup = json.loads(run_cli(db_path, "route-lookup", "gemini", "--user-id", "alias-user", "--json"))
        assert_equal(gemini_lookup["alias"]["label"], "Gemini", "Gemini alias label")
        assert_true(
            any(item["target"] == "gemini.google.com" for item in gemini_lookup["results"]),
            "Gemini lookup should include gemini.google.com",
        )
        assert_true(
            all(item["route_state"] == "managed" and item["requested_server_id"] == "auto" for item in gemini_lookup["results"]),
            "Gemini lookup should be a managed Auto route",
        )

        chatgpt_lookup = json.loads(run_cli(db_path, "route-lookup", "chatgpt", "--user-id", "alias-user", "--json"))
        assert_equal(chatgpt_lookup["alias"]["label"], "ChatGPT / OpenAI", "ChatGPT alias label")
        assert_true(
            any(item["target"] == "chatgpt.com" for item in chatgpt_lookup["results"]),
            "ChatGPT lookup should include chatgpt.com",
        )
        assert_true(
            all(item["route_state"] == "managed" and item["requested_server_id"] == "auto" for item in chatgpt_lookup["results"]),
            "ChatGPT lookup should be a managed Auto route",
        )

        tunnel_list = db_path.parent / "domain-tunnel-list.txt"
        tunnel_list.write_text("needs-tunnel.example\n# comment\nalso-needs-tunnel.example\n", encoding="utf-8")
        imported_domains = json.loads(
            run_cli(db_path, "global-domain-route-import", "auto", str(tunnel_list), "--json")
        )
        assert_equal(imported_domains["count"], 2, "domain tunnel-list import count")
        imported_lookup = json.loads(
            run_cli(db_path, "route-lookup", "needs-tunnel.example", "--user-id", "alias-user", "--json")
        )
        assert_equal(imported_lookup["results"][0]["route_state"], "managed", "imported domain route state")
        assert_equal(imported_lookup["results"][0]["requested_server_id"], "auto", "imported domain requested server")
        assert_true(imported_lookup["results"][0]["server_id"] != "direct", "unresolved Auto route must use a tunnel fallback")

        deleted = json.loads(run_cli(db_path, "service-alias-delete", "testtg", "--json"))
        assert_equal(deleted["alias"], "testtg", "deleted alias")
        aliases = json.loads(run_cli(db_path, "service-alias-list", "--json"))
        assert_true(not any(item["alias"] == "testtg" for item in aliases), "deleted alias should not be listed")

    print("Service alias CLI regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
