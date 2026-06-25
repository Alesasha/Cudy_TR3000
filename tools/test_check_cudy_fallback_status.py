#!/usr/bin/env python3
"""Unit checks for Cudy fallback status evaluation."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from check_cudy_fallback_status import check_endpoint_manifest, check_state  # noqa: E402


def statuses(checks: list[dict[str, str]]) -> dict[str, str]:
    return {check["name"]: check["status"] for check in checks}


def test_endpoint_manifest_validity_checks() -> None:
    now_utc = datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc)
    manifest = {
        "valid_until": (now_utc + timedelta(hours=2)).isoformat(),
        "cache_seconds": 1800,
        "endpoints": [{"id": "primary", "role": "primary", "url": "http://127.0.0.1:8765"}],
    }
    assert statuses(check_endpoint_manifest(manifest, now_utc=now_utc)) == {
        "endpoint-list": "ok",
        "endpoint-validity": "ok",
        "endpoint-cache": "ok",
    }

    manifest["valid_until"] = (now_utc - timedelta(seconds=1)).isoformat()
    assert statuses(check_endpoint_manifest(manifest, now_utc=now_utc))["endpoint-validity"] == "fail"


def test_state_freshness_and_archive_metadata_checks() -> None:
    now_utc = datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc)
    state = {
        "created_at": (now_utc - timedelta(minutes=30)).isoformat(),
        "archive_name": "cudy-control-uswest.tgz",
        "sha256": "a" * 64,
        "bytes": 12345,
    }
    assert statuses(check_state(state, now_utc=now_utc, max_age_minutes=180)) == {
        "state-age": "ok",
        "state-archive": "ok",
    }

    state["created_at"] = (now_utc - timedelta(minutes=181)).isoformat()
    assert statuses(check_state(state, now_utc=now_utc, max_age_minutes=180))["state-age"] == "warn"
