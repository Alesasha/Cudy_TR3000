package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
