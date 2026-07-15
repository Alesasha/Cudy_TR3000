package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestReadyOK(t *testing.T) {
	dir := t.TempDir()
	now := time.Date(2026, 6, 27, 10, 0, 0, 0, time.UTC)
	writeTestJSON(t, filepath.Join(dir, "endpoints.json"), map[string]any{
		"valid_until": now.Add(30 * time.Minute).Format(time.RFC3339),
		"endpoints": []map[string]any{
			{"id": "primary", "role": "primary", "url": "http://127.0.0.1:18765"},
		},
	})
	writeTestJSON(t, filepath.Join(dir, "state.json"), map[string]any{
		"created_at":   now.Add(-10 * time.Minute).Format(time.RFC3339),
		"archive_name": "control-state.tgz",
		"sha256":       "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		"bytes":        1234,
	})

	srv := &server{publicDir: dir, maxStateAge: time.Hour, now: func() time.Time { return now }}
	status := srv.ready()
	if !status.OK {
		t.Fatalf("ready() ok=false warnings=%v checks=%v", status.Warnings, status.Checks)
	}
}

func TestReadyFailsOnStaleState(t *testing.T) {
	dir := t.TempDir()
	now := time.Date(2026, 6, 27, 10, 0, 0, 0, time.UTC)
	writeTestJSON(t, filepath.Join(dir, "endpoints.json"), map[string]any{
		"valid_until": now.Add(30 * time.Minute).Format(time.RFC3339),
		"endpoints":   []map[string]any{{"id": "primary"}},
	})
	writeTestJSON(t, filepath.Join(dir, "state.json"), map[string]any{
		"created_at":   now.Add(-2 * time.Hour).Format(time.RFC3339),
		"archive_name": "control-state.tgz",
		"sha256":       "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		"bytes":        1234,
	})

	srv := &server{publicDir: dir, maxStateAge: time.Hour, now: func() time.Time { return now }}
	status := srv.ready()
	if status.OK {
		t.Fatalf("ready() ok=true for stale state")
	}
	if len(status.Warnings) == 0 {
		t.Fatalf("ready() did not include stale warning")
	}
}

func TestEndpointsHandler(t *testing.T) {
	dir := t.TempDir()
	writeTestJSON(t, filepath.Join(dir, "endpoints.json"), map[string]any{
		"valid_until": "2026-06-27T10:30:00Z",
		"endpoints":   []map[string]any{{"id": "primary"}},
	})
	srv := &server{publicDir: dir, maxStateAge: time.Hour, now: time.Now}

	req := httptest.NewRequest(http.MethodGet, "/api/control/endpoints", nil)
	res := httptest.NewRecorder()
	srv.handleEndpoints(res, req)

	if res.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", res.Code, res.Body.String())
	}
	var payload map[string]any
	if err := json.Unmarshal(res.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if _, ok := payload["endpoints"].([]any); !ok {
		t.Fatalf("missing endpoints in response: %#v", payload)
	}
}

func TestRuntimeStatus(t *testing.T) {
	now := time.Date(2026, 6, 27, 10, 0, 0, 0, time.UTC)
	srv := &server{
		publicDir:   t.TempDir(),
		maxStateAge: time.Hour,
		now:         func() time.Time { return now },
		run:         fakeRuntimeRunner(),
	}

	status := srv.runtime(context.Background())
	if !status.OK {
		t.Fatalf("runtime ok=false warnings=%v", status.Warnings)
	}
	if status.Architecture != "aarch64" || status.OpenWrtTarget != "mediatek/filogic" {
		t.Fatalf("unexpected platform: %#v", status)
	}
	if got := strings.Join(status.SupportedInterfaces, ","); got != "awg1,awg2,proxyde,proxynl" {
		t.Fatalf("supported interfaces=%s", got)
	}
	if got := status.IPv4["proxyde"][0]; got != "172.26.0.1/30" {
		t.Fatalf("proxyde IPv4=%s", got)
	}
	if status.Services["cudy-fallback"] != "running" {
		t.Fatalf("cudy-fallback service=%s", status.Services["cudy-fallback"])
	}
}

func TestAgentPreviewNotConfigured(t *testing.T) {
	srv := &server{agentConfigPath: filepath.Join(t.TempDir(), "missing.json"), now: time.Now}
	status := srv.agentPreview(context.Background())
	if status.Configured {
		t.Fatalf("preview configured=true for missing config")
	}
	if status.OK || status.Error == "" {
		t.Fatalf("unexpected preview for missing config: %#v", status)
	}
}

func TestPreviewTransportsUsesLocalInterfaceMap(t *testing.T) {
	config := map[string]any{
		"transport_plan": []any{map[string]any{
			"server_id": "aktau", "interface_name": "AmneziaVPN", "transport_type": "amneziawg-conf",
		}},
	}
	rows := previewTransports(
		config,
		map[string]bool{"awg1": true},
		map[string]bool{"awg1": true},
		map[string]string{"aktau": "awg1"},
	)
	if len(rows) != 1 || rows[0].Interface != "awg1" || !rows[0].InterfacePresent || !rows[0].InterfaceSupported || !rows[0].Applicable {
		t.Fatalf("rows=%+v", rows)
	}
}

func TestAgentPreviewFromControlServer(t *testing.T) {
	now := time.Date(2026, 6, 27, 10, 0, 0, 0, time.UTC)
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/agent/config" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer test-token" {
			t.Fatalf("unexpected auth header: %s", got)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"user":   map[string]any{"id": "DC_via_Cudy"},
			"device": map[string]any{"id": "DC_via_Cudy-linux"},
			"critical_services": []map[string]any{
				{"service_key": "work", "label": "Work", "targets": []any{"https://example.com/", "tcp://149.154.167.50:443", "ftp://ignored.example"}, "failure_pattern": "blocked"},
			},
			"transport_plan": []map[string]any{
				{
					"server_id":      "proxyde",
					"interface_name": "proxyde",
					"transport_type": "sing-box-json",
					"config_json":    map[string]any{"secret": "must-not-leak"},
				},
				{
					"server_id":      "proxynl",
					"interface_name": "proxynl",
					"transport_type": "sing-box-json",
				},
			},
			"domain_routes": []map[string]any{
				{"domain": "ifconfig.me", "source": "user", "requested_server_id": "auto", "server_id": "proxyde"},
			},
			"ip_routes": []map[string]any{
				{"target_cidr": "149.154.160.0/20", "source": "global", "requested_server_id": "auto", "server_id": "proxynl"},
			},
		})
	}))
	defer control.Close()

	dir := t.TempDir()
	settingsPath := filepath.Join(dir, "agent.json")
	writeTestJSON(t, settingsPath, map[string]any{
		"control_url": control.URL,
		"device_id":   "cudy-home",
		"token":       "test-token",
	})
	srv := &server{
		agentConfigPath: settingsPath,
		now:             func() time.Time { return now },
		run:             fakeRuntimeRunner(),
	}
	status := srv.agentPreview(context.Background())
	if !status.OK {
		t.Fatalf("preview ok=false: %#v", status)
	}
	if status.DeviceID != "cudy-home" || status.UserID != "DC_via_Cudy" {
		t.Fatalf("identity mismatch: device=%s user=%s", status.DeviceID, status.UserID)
	}
	if len(status.TransportPlan) != 2 || !status.TransportPlan[0].Applicable {
		t.Fatalf("unexpected transport preview: %#v", status.TransportPlan)
	}
	if len(status.Routes) != 2 || !status.Routes[0].Applicable || !status.Routes[1].Applicable {
		t.Fatalf("unexpected route preview: %#v", status.Routes)
	}
	if len(status.CriticalServices) != 1 || len(status.CriticalServices[0].Targets) != 2 || status.CriticalServices[0].Targets[0] != "https://example.com/" || status.CriticalServices[0].Targets[1] != "tcp://149.154.167.50:443" {
		t.Fatalf("critical services=%#v", status.CriticalServices)
	}
	raw, err := json.Marshal(status)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "must-not-leak") || strings.Contains(string(raw), "config_json") {
		t.Fatalf("preview leaked raw transport config: %s", string(raw))
	}
}

func TestAgentPreviewFallsBackToFreshCache(t *testing.T) {
	now := time.Date(2026, 7, 13, 12, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	settingsPath := filepath.Join(dir, "agent.json")
	cachePath := filepath.Join(dir, "cache", "agent-config.json")
	writeTestJSON(t, settingsPath, map[string]any{
		"control_url": "http://127.0.0.1:1",
		"device_id":   "cudy-home",
		"token":       "test-token",
		"cache_path":  cachePath,
	})
	if err := writeAgentCache(cachePath, agentCache{
		CachedAt: now.Add(-5 * time.Minute).Format(time.RFC3339),
		Config: map[string]any{
			"user":   map[string]any{"id": "cudy_lan"},
			"device": map[string]any{"id": "cudy-home"},
			"transport_plan": []map[string]any{
				{"server_id": "proxyde", "interface_name": "proxyde", "transport_type": "sing-box-json"},
			},
			"domain_routes": []map[string]any{
				{"domain": "example.com", "server_id": "proxyde"},
			},
		},
	}); err != nil {
		t.Fatal(err)
	}
	srv := &server{
		agentConfigPath:  settingsPath,
		agentCachePath:   cachePath,
		maxAgentCacheAge: time.Hour,
		now:              func() time.Time { return now },
		run:              fakeRuntimeRunner(),
		fetchAgentConfig: func(context.Context, agentSettings) (map[string]any, error) {
			return nil, errors.New("control unavailable")
		},
	}
	status := srv.agentPreview(context.Background())
	if !status.OK || status.Source != "cache" || status.ControlError == "" {
		t.Fatalf("unexpected cached preview: %#v", status)
	}
	if status.CacheAge != 300 || len(status.Routes) != 1 || !status.Routes[0].Applicable {
		t.Fatalf("unexpected cached routes/age: %#v", status)
	}
}

func TestLocalCriticalServicesOverrideControl(t *testing.T) {
	merged := mergeCriticalServices(
		[]criticalServicePreview{{ServiceKey: "chat", Label: "Control", Targets: []string{"https://control.example/"}}},
		[]criticalServicePreview{
			{ServiceKey: "chat", Label: "Local", Targets: []string{"https://local.example/"}},
			{ServiceKey: "internet", Targets: []string{"https://example.com/", "tcp://149.154.167.50:443", "tcp://missing-port", "ftp://ignored.example"}},
		},
	)
	if len(merged) != 2 || merged[0].Label != "Local" || merged[0].Targets[0] != "https://local.example/" {
		t.Fatalf("merged=%#v", merged)
	}
	if len(merged[1].Targets) != 2 || merged[1].Targets[1] != "tcp://149.154.167.50:443" || merged[1].Label != "internet" {
		t.Fatalf("sanitized local service=%#v", merged[1])
	}
}

func TestAgentCacheRejectsStalePolicy(t *testing.T) {
	now := time.Date(2026, 7, 13, 12, 0, 0, 0, time.UTC)
	path := filepath.Join(t.TempDir(), "agent-cache.json")
	if err := writeAgentCache(path, agentCache{CachedAt: now.Add(-2 * time.Hour).Format(time.RFC3339), Config: map[string]any{"ok": true}}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := readAgentCache(path, now, time.Hour); err == nil || !strings.Contains(err.Error(), "stale") {
		t.Fatalf("stale cache was accepted: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("cache permissions=%o want=600", info.Mode().Perm())
	}
}

func writeTestJSON(t *testing.T, path string, payload any) {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
}

func fakeRuntimeRunner() commandRunner {
	return fakeRunner(map[string]string{
		"uname -m 2>/dev/null || true": "aarch64\n",
		"grep '^DISTRIB_TARGET=' /etc/openwrt_release 2>/dev/null | cut -d= -f2- | tr -d \"'\\\"\" || true":                             "mediatek/filogic\n",
		"uci -q get pbr.config.supported_interface 2>/dev/null || true":                                                                 "awg1 awg2 proxyde proxynl\n",
		"sed -n \"s/^TARGET_INTERFACE='\\([^']*\\)'.*/\\1/p\" /usr/share/pbr/pbr.user.opencck-merged-vpn 2>/dev/null | tail -1 || true": "awg1\n",
		"ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' || true":                                                 "lo\nbr-lan\nawg1\nproxyde\nproxynl\n",
		"ip -4 -o addr show 2>/dev/null | awk '{print $2 \"\\t\" $4}' || true":                                                          "br-lan\t192.168.8.1/24\nawg1\t10.8.1.8/32\nproxyde\t172.26.0.1/30\nproxynl\t172.26.1.1/30\n",
		"cat /etc/crontabs/root 2>/dev/null || true":                                                                                    "# comment\n7 5 * * * /usr/bin/vpntype-proxy-refresh-all\n",
		"ss -ltnp 2>/dev/null || true": "LISTEN 0 128 127.0.0.1:8765 0.0.0.0:* users:((\"cudy-fallback\"))\n",
	})
}

func fakeRunner(outputs map[string]string) commandRunner {
	return func(_ context.Context, name string, args ...string) (string, error) {
		if name != "/bin/sh" || len(args) != 2 || args[0] != "-c" {
			return "", fmt.Errorf("unexpected command: %s %v", name, args)
		}
		script := args[1]
		if strings.HasPrefix(script, "if [ -x /etc/init.d/") {
			if strings.Contains(script, "/etc/init.d/cudy-fallback") || strings.Contains(script, "/etc/init.d/pbr") {
				return "running\n", nil
			}
			return "missing\n", nil
		}
		out, ok := outputs[script]
		if !ok {
			return "", fmt.Errorf("unexpected shell script: %s", script)
		}
		return out, nil
	}
}
