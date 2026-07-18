#!/usr/bin/env python3
"""Regression checks for control-server deployment payload selection."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import deploy_control_server as deploy
import deploy_control_server_via_tunnel_user as fallback_deploy


def main() -> int:
    code_only = deploy.selected_upload_dirs(include_agent_updates=False)
    with_updates = deploy.selected_upload_dirs(include_agent_updates=True)
    assert deploy.AGENT_UPDATE_DIR not in code_only
    assert deploy.AGENT_UPDATE_DIR in with_updates
    assert set(deploy.UPLOAD_DIRS).issubset(code_only)

    defaults = deploy.build_parser().parse_args([])
    assert not defaults.skip_agent_updates
    selected = deploy.build_parser().parse_args(["--skip-agent-updates"])
    assert selected.skip_agent_updates
    private = deploy.build_parser().parse_args(["--via-cudy"])
    assert private.via_cudy
    assert private.cudy_host == "192.168.8.1"
    assert private.private_host == "172.29.172.1"
    assert private.cudy_awg_interface == "awg2"
    assert not private.upload_db
    source = (TOOLS / "deploy_control_server.py").read_text(encoding="utf-8")
    assert '"direct-tcpip"' in source
    assert "Archive uploaded in" in source
    assert "Deployment completed in" in source
    assert "install_agent_provisioning_ssh.py" in source
    assert (ROOT / "config" / "android_enrollment_bootstrap.pub").is_file()
    provisioning_installer = (TOOLS / "install_agent_provisioning_ssh.py").read_text(encoding="utf-8")
    for marker in (
        'DEFAULT_BOOTSTRAP_USER = "cudy-enroll"',
        'PermitOpen 127.0.0.1:8766',
        'DEFAULT_BOOTSTRAP_KEY = Path("/opt/cudy-control/config/android_enrollment_bootstrap.pub")',
    ):
        assert marker in provisioning_installer

    fallback_code_only = [path.relative_to(ROOT).as_posix() for path in fallback_deploy.archive_paths(include_agent_updates=False)]
    fallback_with_updates = [path.relative_to(ROOT).as_posix() for path in fallback_deploy.archive_paths(include_agent_updates=True)]
    assert fallback_deploy.AGENT_UPDATE_DIR not in fallback_code_only
    assert fallback_deploy.AGENT_UPDATE_DIR in fallback_with_updates
    fallback_selected = fallback_deploy.build_parser().parse_args(["--skip-agent-updates"])
    assert fallback_selected.skip_agent_updates
    fallback_openssh = fallback_deploy.build_parser().parse_args(["--openssh"])
    assert fallback_openssh.openssh
    options = fallback_deploy.openssh_options(fallback_openssh)
    assert "BatchMode=yes" in options
    assert "ConnectionAttempts=1" in options
    script = fallback_deploy.promotion_script(
        fallback_openssh,
        remote_archive="/tmp/control.tar",
        remote_script="/tmp/promote.sh",
    )
    assert "systemctl restart vpn-control" in script
    assert "rm -f /tmp/control.tar /tmp/promote.sh" in script
    fallback_source = (TOOLS / "deploy_control_server_via_tunnel_user.py").read_text(encoding="utf-8")
    assert 'script -qec' in fallback_source
    assert '["ssh", "-tt"' not in fallback_source
    print("Control-server deploy payload regression passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
