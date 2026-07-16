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
        self.assertIn("fw4 check", text)
        self.assertIn("pbr_dataplane_ready", text)
        self.assertIn("wait_for_dataplane", text)
        self.assertNotIn("/var/run/pbr.lock", text)
        self.assertIn("fail_open", text)

    def test_router_agent_uses_safe_bootstrap(self) -> None:
        main = (ROOT / "cmd" / "cudy-router-agent" / "main.go").read_text(encoding="utf-8")
        init = (ROOT / "openwrt" / "cudy-router-agent.init").read_text(encoding="utf-8")
        self.assertIn('/usr/bin/cudy-pbr-safe-restart", "command used', main)
        self.assertIn("-bootstrap-command /usr/bin/cudy-pbr-safe-restart", init)

    def test_pbr_paths_collapse_cidrs(self) -> None:
        full = (ROOT / "openwrt" / "pbr.user.opencck-merged-vpn").read_text(encoding="utf-8")
        fast = (ROOT / "openwrt" / "cudy-pbr-fast-apply").read_text(encoding="utf-8")
        collapse = (ROOT / "openwrt" / "cudy-cidr-collapse").read_text(encoding="utf-8")
        self.assertIn("collapse_file", full)
        self.assertIn("collapse_file", fast)
        self.assertIn('printf "%010.0f %010.0f', collapse)
        self.assertIn("sort -k1,1 -k2,2r", collapse)


if __name__ == "__main__":
    unittest.main()
