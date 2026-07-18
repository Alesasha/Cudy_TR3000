#!/usr/bin/env python3

import sqlite3
import tarfile
import tempfile
from pathlib import Path

from verify_control_backup import REQUIRED_FILES, verify_archive


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cudy-control-backup-test-") as temp_dir:
        root = Path(temp_dir) / "stage"
        root.mkdir()
        for name in REQUIRED_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == "data/vpn_control.db":
                conn = sqlite3.connect(path)
                conn.execute("CREATE TABLE smoke (id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO smoke(value) VALUES ('ok')")
                conn.commit()
                conn.close()
            elif name == "backup-metadata.txt":
                path.write_text("sqlite_backup=online\ninclude_secrets=True\n", encoding="utf-8")
            else:
                path.write_text("test\n", encoding="utf-8")
        secret = root / "secrets" / "provider.json"
        secret.parent.mkdir(parents=True)
        secret.write_text("{}\n", encoding="utf-8")
        archive_path = Path(temp_dir) / "backup.tgz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for path in root.rglob("*"):
                archive.add(path, arcname=f"./{path.relative_to(root).as_posix()}")

        result = verify_archive(archive_path)
        assert result["ok"] is True, result
        assert result["sqlite_integrity"] == "ok"
        assert result["secret_file_count"] == 1
        assert result["unsafe_member_count"] == 0

        no_secret_result = verify_archive(archive_path, require_secrets=False)
        assert no_secret_result["ok"] is True, no_secret_result

    print("Control backup verification regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
