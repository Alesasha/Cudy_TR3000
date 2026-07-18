#!/usr/bin/env python3
"""Rehearse a control-server restore locally without touching production."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.request import urlopen

from verify_control_backup import DEFAULT_BACKUP_DIR, latest_backup, normalized_member_name, verify_archive


def safe_extract(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            normalized = normalized_member_name(member.name)
            if not normalized:
                continue
            relative = PurePosixPath(normalized)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not (member.isdir() or member.isfile()):
                raise ValueError(f"Unsupported archive member type: {member.name}")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Could not read archive member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_json(url: str, timeout: float = 3.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected object from {url}")
    return value


def wait_for_health(base_url: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health = fetch_json(f"{base_url}/healthz")
            if health.get("ok") is True:
                return health
            last_error = RuntimeError(f"unhealthy response: {health!r}")
        except Exception as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"restored control-server did not become healthy: {last_error}")


def rehearse(archive_path: Path, *, timeout: int = 20) -> dict[str, Any]:
    started = time.monotonic()
    verification = verify_archive(archive_path)
    if not verification["ok"]:
        raise RuntimeError(f"backup verification failed: {verification}")

    with tempfile.TemporaryDirectory(prefix="cudy-control-restore-rehearsal-") as temp_dir:
        restored = Path(temp_dir) / "control"
        restored.mkdir()
        safe_extract(archive_path, restored)
        app = restored / "tools" / "vpn_control_app.py"
        db = restored / "data" / "vpn_control.db"
        inventory = restored / "config" / "vpn_inventory.json"

        summary_process = subprocess.run(
            [
                sys.executable,
                str(app),
                "--db",
                str(db),
                "--inventory",
                str(inventory),
                "summary",
            ],
            cwd=restored,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if summary_process.returncode != 0:
            raise RuntimeError(
                f"restored summary failed rc={summary_process.returncode}: "
                f"{summary_process.stderr.strip()}"
            )

        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        command = [
            sys.executable,
            str(app),
            "--db",
            str(db),
            "--inventory",
            str(inventory),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-enrollment-server",
            "--no-auto-worker",
            "--no-provider-refresh-worker",
        ]
        log_path = Path(temp_dir) / "restored-control.log"
        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=restored,
                text=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                health = wait_for_health(base_url, timeout)
                readiness = fetch_json(f"{base_url}/readyz")
                if readiness.get("ok") is not True:
                    raise RuntimeError(f"restored readiness failed: {readiness}")
            except Exception as exc:
                log_file.flush()
                log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                raise RuntimeError(f"{exc}\nRestored server log:\n{log_tail}") from exc
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

        return {
            "ok": True,
            "archive": str(archive_path),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "verification": verification,
            "summary": [line for line in summary_process.stdout.splitlines() if line.strip()],
            "healthz": health,
            "readyz": readiness,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", nargs="?", type=Path)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        archive = args.archive or latest_backup(args.backup_dir)
        result = rehearse(archive, timeout=args.timeout)
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"Control restore rehearsal: FAIL {exc}")
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"Control restore rehearsal: OK archive={result['archive']} "
            f"elapsed={result['elapsed_seconds']}s"
        )
        for line in result["summary"]:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
