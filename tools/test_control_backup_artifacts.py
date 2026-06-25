#!/usr/bin/env python3
"""Local regression checks for control-server backup/fallback artifacts."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import backup_control_server as backup  # noqa: E402
import sync_control_state_to_cudy as sync_state  # noqa: E402


class FakeChannel:
    def __init__(self, rc: int = 0, *, out: bytes = b"", err: bytes = b"") -> None:
        self.rc = rc
        self.out = out
        self.err = err
        self.closed = False

    def recv_exit_status(self) -> int:
        return self.rc

    def settimeout(self, _value: float) -> None:
        return None

    def recv_ready(self) -> bool:
        return bool(self.out)

    def recv_stderr_ready(self) -> bool:
        return bool(self.err)

    def recv(self, _size: int) -> bytes:
        out = self.out
        self.out = b""
        return out

    def recv_stderr(self, _size: int) -> bytes:
        err = self.err
        self.err = b""
        return err

    def exit_status_ready(self) -> bool:
        return not self.out and not self.err

    def close(self) -> None:
        self.closed = True

    def sendall(self, _content: bytes) -> None:
        return None

    def shutdown_write(self) -> None:
        return None


class FakeStdin:
    def __init__(self) -> None:
        self.channel = FakeChannel()
        self.text = ""

    def write(self, content: str) -> None:
        self.text += content


class FakeStdout:
    def __init__(self, rc: int = 0, *, out: bytes = b"") -> None:
        self.channel = FakeChannel(rc, out=out)

    def read(self) -> bytes:
        return b""


class FakeStderr:
    def read(self) -> bytes:
        return b""


class FakeSshClient:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.writes: dict[str, bytes] = {}

    def exec_command(self, command: str, timeout: int) -> tuple[FakeStdin, FakeStdout, FakeStderr]:
        self.commands.append(command)
        stdin = FakeStdin()
        stdout = FakeStdout(out=b"ok\n")
        stderr = FakeStderr()
        if command.startswith("cat > "):
            raw_path = command.removeprefix("cat > ").strip()
            path = raw_path[1:-1] if raw_path.startswith("'") and raw_path.endswith("'") else raw_path

            def capture_text(content: str) -> None:
                stdin.text += content
                self.writes[path] = stdin.text.encode("utf-8")

            def capture_bytes(content: bytes) -> None:
                self.writes[path] = content

            stdin.write = capture_text  # type: ignore[method-assign]
            stdin.channel.sendall = capture_bytes  # type: ignore[method-assign]
        return stdin, stdout, stderr


def assert_true(value: Any, context: str) -> None:
    if not value:
        raise AssertionError(context)


def assert_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def run_prune_backup_check() -> None:
    with tempfile.TemporaryDirectory(prefix="cudy-backup-prune-") as tmp:
        base = Path(tmp)
        paths: list[Path] = []
        for idx in range(4):
            path = base / f"cudy-control-test-{idx}.tgz"
            path.write_bytes(f"archive-{idx}".encode("ascii"))
            ts = time.time() - (10 - idx)
            os.utime(path, (ts, ts))
            paths.append(path)

        removed = backup.prune_backups(base, keep=2)
        kept_names = sorted(path.name for path in base.glob("*.tgz"))
        removed_names = sorted(path.name for path in removed)

        assert_equal(kept_names, [paths[2].name, paths[3].name], "newest two archives should be kept")
        assert_equal(removed_names, [paths[0].name, paths[1].name], "oldest archives should be pruned")


def run_backup_timeout_contract_check() -> None:
    client = FakeSshClient()
    output = backup.ssh_exec(client, "true", timeout=3)  # type: ignore[arg-type]
    assert_equal(output, "ok\n", "bounded ssh_exec should collect stdout")

    direct_source = (ROOT / "tools" / "backup_control_server.py").read_text(encoding="utf-8")
    tunnel_source = (ROOT / "tools" / "backup_control_server_via_tunnel_user.py").read_text(encoding="utf-8")
    assert_true("remote command timed out" in direct_source, "direct backup should fail remote commands on timeout")
    assert_true("sftp.get_channel().settimeout(args.timeout)" in direct_source, "direct backup should bound SFTP operations")
    assert_true("sftp.get_channel().settimeout(args.timeout)" in tunnel_source, "tunnel backup should bound SFTP operations")
    assert_true("DEFAULT_PASSWORD_FILE" in direct_source, "direct backup should support unattended password file")
    assert_true("sys.stdin.isatty()" in direct_source, "direct backup should not prompt in non-interactive runs")
    assert_true("DEFAULT_PASSWORD_FILE" in tunnel_source, "tunnel backup should support unattended password file")
    assert_true("sys.stdin.isatty()" in tunnel_source, "tunnel backup should not prompt in non-interactive runs")


def run_cudy_publish_artifact_check() -> None:
    with tempfile.TemporaryDirectory(prefix="cudy-fallback-publish-") as tmp:
        archive_path = Path(tmp) / "cudy-control-95-182-91-203-20260625-000000.tgz"
        payload = b"fake-control-state"
        archive_path.write_bytes(payload)
        client = FakeSshClient()

        status = sync_state.publish_to_cudy(
            client,  # type: ignore[arg-type]
            archive_path=archive_path,
            source_host="95.182.91.203",
            state_dir="/root/cudy-control-fallback",
            web_dir="/www/cudy-control",
            include_secrets=True,
            timeout=10,
            keep_remote=3,
        )

        assert_equal(status["bytes"], len(payload), "published byte size")
        assert_equal(status["sha256"], sync_state.sha256_file(archive_path), "published sha256")
        assert_equal(
            client.writes["/root/cudy-control-fallback/" + archive_path.name],
            payload,
            "secret archive bytes should be uploaded under /root",
        )
        state = json.loads(client.writes["/www/cudy-control/state.json"].decode("utf-8"))
        endpoints = json.loads(client.writes["/www/cudy-control/endpoints.json"].decode("utf-8"))
        assert_equal(state["archive_name"], archive_path.name, "public state archive name")
        assert_equal(state["sha256"], status["sha256"], "public state sha256")
        assert_true(endpoints["endpoints"], "fallback endpoints manifest should not be empty")
        assert_true(
            any("control-state-current.tgz" in command for command in client.commands),
            "publish should update the current-state symlink",
        )


def main() -> int:
    run_prune_backup_check()
    run_backup_timeout_contract_check()
    run_cudy_publish_artifact_check()
    print("Control backup/fallback artifact regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
