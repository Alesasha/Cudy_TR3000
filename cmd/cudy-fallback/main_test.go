package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
		run: fakeRunner(map[string]string{
			"uname -m 2>/dev/null || true": "aarch64\n",
			"grep '^DISTRIB_TARGET=' /etc/openwrt_release 2>/dev/null | cut -d= -f2- | tr -d \"'\\\"\" || true":                             "mediatek/filogic\n",
			"uci -q get pbr.config.supported_interface 2>/dev/null || true":                                                                 "awg1 awg2 proxyde proxynl\n",
			"sed -n \"s/^TARGET_INTERFACE='\\([^']*\\)'.*/\\1/p\" /usr/share/pbr/pbr.user.opencck-merged-vpn 2>/dev/null | tail -1 || true": "awg1\n",
			"ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' || true":                                                 "lo\nbr-lan\nawg1\nproxyde\n",
			"ip -4 -o addr show 2>/dev/null | awk '{print $2 \"\\t\" $4}' || true":                                                          "br-lan\t192.168.8.1/24\nawg1\t10.8.1.8/32\nproxyde\t172.26.0.1/30\n",
			"cat /etc/crontabs/root 2>/dev/null || true":                                                                                    "# comment\n7 5 * * * /usr/bin/vpntype-proxy-refresh-all\n",
			"ss -ltnp 2>/dev/null || true": "LISTEN 0 128 127.0.0.1:8765 0.0.0.0:* users:((\"cudy-fallback\"))\n",
		}),
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
