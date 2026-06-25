#!/usr/bin/env python3
"""Unit checks for control endpoint manifest TTLs."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from sync_control_manifest_to_cudy import (  # noqa: E402
    CUDY_STATIC_MANIFEST_CACHE_SECONDS,
    CUDY_STATIC_MANIFEST_VALID_SECONDS,
)
from sync_control_state_to_cudy import cudy_static_control_endpoints_manifest  # noqa: E402
from vpn_control_app import control_endpoints_manifest  # noqa: E402


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def ttl_seconds(manifest: dict) -> int:
    return int((parse_utc(manifest["valid_until"]) - parse_utc(manifest["generated_at"])).total_seconds())


def test_default_manifest_has_short_live_api_ttl() -> None:
    manifest = control_endpoints_manifest()
    assert ttl_seconds(manifest) == 600
    assert manifest["cache_seconds"] == 300
    assert manifest["endpoints"]


def test_cudy_static_manifest_has_long_fallback_ttl() -> None:
    manifest = cudy_static_control_endpoints_manifest()
    assert ttl_seconds(manifest) == CUDY_STATIC_MANIFEST_VALID_SECONDS
    assert manifest["cache_seconds"] == CUDY_STATIC_MANIFEST_CACHE_SECONDS
    assert manifest["endpoints"]
