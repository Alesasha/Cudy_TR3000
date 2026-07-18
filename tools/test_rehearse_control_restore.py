#!/usr/bin/env python3

import sqlite3
import tarfile
import tempfile
from pathlib import Path

from rehearse_control_restore import rehearse, safe_extract
from verify_control_backup import REQUIRED_FILES


ROOT = Path(__file__).resolve().parents[1]


def build_fixture(root: Path) -> Path:
    stage = root / "stage"
    stage.mkdir()
    for name in REQUIRED_FILES:
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name == "tools/vpn_control_app.py":
            target.write_bytes((ROOT / name).read_bytes())
        elif name == "config/vpn_inventory.json":
            target.write_bytes((ROOT / name).read_bytes())
        elif name == "deploy/uswest/vpn-control.service":
            target.write_bytes((ROOT / name).read_bytes())
        elif name == "requirements.txt":
            target.write_bytes((ROOT / name).read_bytes())
        elif name == "backup-metadata.txt":
            target.write_text("sqlite_backup=online\ninclude_secrets=True\n", encoding="utf-8")
        elif name == "data/vpn_control.db":
            conn = sqlite3.connect(target)
            conn.execute("CREATE TABLE smoke (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
    secret = stage / "secrets" / "rehearsal.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("fixture\n", encoding="utf-8")

    app = stage / "tools" / "vpn_control_app.py"
    db = stage / "data" / "vpn_control.db"
    inventory = stage / "config" / "vpn_inventory.json"
    import subprocess
    import sys

    subprocess.check_call(
        [sys.executable, str(app), "--db", str(db), "--inventory", str(inventory), "init-db"],
        cwd=stage,
        stdout=subprocess.DEVNULL,
    )
    archive_path = root / "fixture.tgz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in stage.rglob("*"):
            archive.add(path, arcname=f"./{path.relative_to(stage).as_posix()}")
    return archive_path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-control-rehearsal-test-") as temp_dir:
        root = Path(temp_dir)
        archive_path = build_fixture(root)
        result = rehearse(archive_path, timeout=20)
        assert result["ok"] is True, result
        assert result["healthz"]["ok"] is True
        assert result["readyz"]["ok"] is True

        unsafe = root / "unsafe.tgz"
        payload = root / "payload"
        payload.write_text("bad\n", encoding="utf-8")
        with tarfile.open(unsafe, "w:gz") as archive:
            archive.add(payload, arcname="../outside")
        try:
            safe_extract(unsafe, root / "unsafe-out")
        except ValueError:
            pass
        else:
            raise AssertionError("path traversal member was accepted")

    print("Control restore rehearsal regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
