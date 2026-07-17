from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CudyPBRSafetyTests(unittest.TestCase):
    def test_watchdog_recovers_once_then_fails_open(self) -> None:
        text = (ROOT / "openwrt" / "cudy-pbr-watchdog").read_text(encoding="utf-8")
        self.assertIn("echo 1 > /proc/sys/net/ipv4/ip_forward", text)
        self.assertIn("/etc/init.d/pbr stop", text)
        self.assertIn("/usr/bin/cudy-pbr-safe-restart restart", text)
        self.assertIn("pbr_dataplane_ready", text)
        self.assertNotIn("/var/run/pbr.lock", text)

    def test_safe_start_serializes_and_validates(self) -> None:
        text = (ROOT / "openwrt" / "cudy-pbr-safe-restart").read_text(encoding="utf-8")
        self.assertIn('mkdir "$LOCK_DIR"', text)
        self.assertIn('printf \'%s\\n\' "$$" > "$LOCK_DIR/pid"', text)
        self.assertIn('kill -0 "$owner_pid"', text)
        self.assertIn('log "removing stale PBR transaction lock"', text)
        self.assertIn("# BusyBox ash normally runs the EXIT trap", text)
        self.assertIn("cleanup\nlog \"PBR rebuild completed", text)
        self.assertIn("fw4 check", text)
        self.assertIn("pbr_dataplane_ready", text)
        self.assertIn("wait_for_dataplane", text)
        self.assertIn("rm -f /var/run/pbr.lock", text)
        self.assertIn("fail_open", text)
        self.assertIn("fw4 validation failed after PBR stop", text)
        self.assertNotIn("fw4 validation failed before PBR start", text)

    def test_watchdog_removes_only_dead_transaction_locks(self) -> None:
        text = (ROOT / "openwrt" / "cudy-pbr-watchdog").read_text(encoding="utf-8")
        self.assertIn("/tmp/cudy-pbr-safe.lock/pid", text)
        self.assertIn('kill -0 "$owner_pid"', text)
        self.assertIn("rmdir /tmp/cudy-pbr-safe.lock", text)
        self.assertNotIn("/var/run/pbr.lock", text)

    def test_router_agent_uses_safe_bootstrap(self) -> None:
        main = (ROOT / "cmd" / "cudy-router-agent" / "main.go").read_text(encoding="utf-8")
        init = (ROOT / "openwrt" / "cudy-router-agent.init").read_text(encoding="utf-8")
        self.assertIn('/usr/bin/cudy-pbr-safe-restart", "command used', main)
        self.assertIn("-bootstrap-command /usr/bin/cudy-pbr-safe-restart", init)
        self.assertIn("!a.pbrDataplaneReady(ctx, groups)", main)
        self.assertIn('"pbr_" + iface + "_4_dst_ip_user"', main)
        self.assertIn("rollbackTimeout = 210 * time.Second", main)

    def test_pbr_paths_collapse_cidrs(self) -> None:
        full = (ROOT / "openwrt" / "pbr.user.opencck-merged-vpn").read_text(encoding="utf-8")
        fast = (ROOT / "openwrt" / "cudy-pbr-fast-apply").read_text(encoding="utf-8")
        collapse = (ROOT / "openwrt" / "cudy-cidr-collapse").read_text(encoding="utf-8")
        self.assertIn("collapse_file", full)
        self.assertIn("collapse_file", fast)
        self.assertIn('printf "%010.0f %010.0f', collapse)
        self.assertIn("sort -k1,1 -k2,2r", collapse)

    def test_fast_apply_rebuilds_interval_sets_from_complete_inputs(self) -> None:
        fast = (ROOT / "openwrt" / "cudy-pbr-fast-apply").read_text(encoding="utf-8")
        self.assertIn('register_set wan "$full_wan"', fast)
        self.assertIn('register_set "$target_interface" "$full_target"', fast)
        self.assertNotIn("register_managed_delta wan", fast)
        self.assertNotIn("append_delete_elements", fast)


if __name__ == "__main__":
    unittest.main()
