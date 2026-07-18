#!/usr/bin/env python3
"""Verify a local control-server disaster-recovery archive."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = ROOT / "backups" / "control-server"
REQUIRED_FILES = (
    "backup-metadata.txt",
    "data/vpn_control.db",
    "tools/vpn_control_app.py",
    "deploy/uswest/vpn-control.service",
    "config/vpn_inventory.json",
    "requirements.txt",
)


def normalized_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def latest_backup(directory: Path) -> Path:
    candidates = sorted(directory.glob("cudy-control-*.tgz"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"No control backup archives under {directory}")
    return candidates[-1]


def verify_archive(path: Path, *, require_secrets: bool = True) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    with tarfile.open(path, "r:gz") as archive:
        members = {normalized_member_name(member.name): member for member in archive.getmembers()}
        unsafe_members = []
        for name, member in members.items():
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or not (member.isdir() or member.isfile()):
                unsafe_members.append(name)
        missing = [name for name in REQUIRED_FILES if name not in members]
        secret_files = [name for name in members if name.startswith("secrets/") and members[name].isfile()]
        if require_secrets and not secret_files:
            missing.append("secrets/<files>")

        metadata_text = ""
        metadata_member = members.get("backup-metadata.txt")
        if metadata_member is not None:
            extracted = archive.extractfile(metadata_member)
            if extracted is not None:
                metadata_text = extracted.read().decode("utf-8", errors="replace")

        sqlite_ok = False
        sqlite_result = "missing"
        db_member = members.get("data/vpn_control.db")
        if db_member is not None and db_member.isfile():
            source = archive.extractfile(db_member)
            if source is not None:
                with tempfile.TemporaryDirectory(prefix="cudy-control-backup-verify-") as temp_dir:
                    db_path = Path(temp_dir) / "vpn_control.db"
                    db_path.write_bytes(source.read())
                    conn = sqlite3.connect(db_path)
                    try:
                        row = conn.execute("PRAGMA integrity_check").fetchone()
                        sqlite_result = str(row[0] if row else "no result")
                        sqlite_ok = sqlite_result.lower() == "ok"
                    finally:
                        conn.close()

    metadata_ok = "sqlite_backup=online" in metadata_text
    if require_secrets:
        metadata_ok = metadata_ok and "include_secrets=True" in metadata_text
    ok = not missing and not unsafe_members and sqlite_ok and metadata_ok
    return {
        "ok": ok,
        "archive": str(path),
        "bytes": path.stat().st_size,
        "missing": missing,
        "unsafe_member_count": len(unsafe_members),
        "sqlite_integrity": sqlite_result,
        "metadata_ok": metadata_ok,
        "secret_file_count": len(secret_files),
        "member_count": len(members),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--allow-no-secrets", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = args.archive or latest_backup(args.backup_dir)
    result = verify_archive(path, require_secrets=not args.allow_no_secrets)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Control backup: {'OK' if result['ok'] else 'FAIL'} {result['archive']}")
        print(
            f"  bytes={result['bytes']} members={result['member_count']} "
            f"secrets={result['secret_file_count']} sqlite={result['sqlite_integrity']} "
            f"metadata={'ok' if result['metadata_ok'] else 'invalid'} "
            f"unsafe={result['unsafe_member_count']}"
        )
        for name in result["missing"]:
            print(f"  missing: {name}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
