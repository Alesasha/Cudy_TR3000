#!/usr/bin/env python3
"""Regression checks for transport and semantic Auto probe results."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import vpn_control_app as app  # noqa: E402


def check(metrics: str, body: str = "") -> dict[str, object]:
    parsed = app.parse_curl_probe_output(metrics)
    app.apply_semantic_probe_check(parsed, body_text=body)
    return parsed


def main() -> int:
    success = check("rc=0\nhttp_code=200\ntime_total=0.25\n")
    assert success["ok"] is True
    assert success["semantic_status"] == "ok"

    timeout = check("rc=28\nhttp_code=000\ntime_total=5.0\n")
    assert timeout["ok"] is False
    assert timeout["semantic_status"] == "curl_error"

    geo_block = check(
        "rc=0\nhttp_code=200\ntime_total=0.4\n",
        "Gemini isn't currently supported in your country. Stay tuned!",
    )
    assert geo_block["ok"] is False
    assert geo_block["semantic_status"] == "geo_blocked"
    assert geo_block.get("semantic_evidence")

    print("Probe semantic regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
