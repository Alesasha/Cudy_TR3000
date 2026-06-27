package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

type server struct {
	publicDir   string
	maxStateAge time.Duration
	now         func() time.Time
}

type check struct {
	Name    string `json:"name"`
	OK      bool   `json:"ok"`
	Summary string `json:"summary"`
}

type readyResponse struct {
	OK          bool     `json:"ok"`
	GeneratedAt string   `json:"generated_at"`
	Checks      []check  `json:"checks"`
	Warnings    []string `json:"warnings"`
}

type healthResponse struct {
	OK bool `json:"ok"`
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8765", "HTTP listen address")
	publicDir := flag.String("public-dir", "/www/cudy-control", "directory containing endpoints.json and state.json")
	maxStateAge := flag.Duration("max-state-age", 3*time.Hour, "maximum accepted age for state.json")
	flag.Parse()

	srv := &server{
		publicDir:   *publicDir,
		maxStateAge: *maxStateAge,
		now:         func() time.Time { return time.Now().UTC() },
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.handleHealth)
	mux.HandleFunc("/readyz", srv.handleReady)
	mux.HandleFunc("/api/control/endpoints", srv.handleEndpoints)
	mux.HandleFunc("/cudy-control/endpoints.json", srv.handleEndpoints)
	mux.HandleFunc("/cudy-control/state.json", srv.handleState)

	log.Printf("cudy fallback service listening on %s public_dir=%s", *listen, *publicDir)
	if err := http.ListenAndServe(*listen, mux); err != nil {
		log.Fatal(err)
	}
}

func (s *server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, healthResponse{OK: true})
}

func (s *server) handleReady(w http.ResponseWriter, _ *http.Request) {
	status := s.ready()
	code := http.StatusOK
	if !status.OK {
		code = http.StatusServiceUnavailable
	}
	writeJSON(w, code, status)
}

func (s *server) handleEndpoints(w http.ResponseWriter, _ *http.Request) {
	payload, err := readJSONFile(s.path("endpoints.json"))
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err)
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *server) handleState(w http.ResponseWriter, _ *http.Request) {
	payload, err := readJSONFile(s.path("state.json"))
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, err)
		return
	}
	writeJSON(w, http.StatusOK, payload)
}

func (s *server) ready() readyResponse {
	now := s.now().UTC()
	checks := []check{}
	warnings := []string{}

	endpoints, err := readJSONFile(s.path("endpoints.json"))
	endpointsOK, endpointsSummary := validateEndpoints(endpoints, err, now)
	checks = append(checks, check{Name: "endpoints", OK: endpointsOK, Summary: endpointsSummary})
	if !endpointsOK {
		warnings = append(warnings, endpointsSummary)
	}

	state, err := readJSONFile(s.path("state.json"))
	stateOK, stateSummary := validateState(state, err, now, s.maxStateAge)
	checks = append(checks, check{Name: "state", OK: stateOK, Summary: stateSummary})
	if !stateOK {
		warnings = append(warnings, stateSummary)
	}

	ok := true
	for _, item := range checks {
		if !item.OK {
			ok = false
			break
		}
	}

	return readyResponse{
		OK:          ok,
		GeneratedAt: now.Format(time.RFC3339),
		Checks:      checks,
		Warnings:    warnings,
	}
}

func (s *server) path(name string) string {
	return filepath.Join(s.publicDir, name)
}

func validateEndpoints(payload map[string]any, err error, now time.Time) (bool, string) {
	if err != nil {
		return false, err.Error()
	}
	endpoints, ok := payload["endpoints"].([]any)
	if !ok || len(endpoints) == 0 {
		return false, "endpoint list is empty"
	}
	rawValidUntil, ok := payload["valid_until"].(string)
	if !ok || rawValidUntil == "" {
		return false, "valid_until is missing"
	}
	validUntil, err := parseTime(rawValidUntil)
	if err != nil {
		return false, fmt.Sprintf("valid_until is invalid: %v", err)
	}
	remaining := int(validUntil.Sub(now).Seconds())
	if remaining <= 0 {
		return false, fmt.Sprintf("endpoint manifest expired %ds ago", -remaining)
	}
	return true, fmt.Sprintf("%d endpoint(s), valid_for=%ds", len(endpoints), remaining)
}

func validateState(payload map[string]any, err error, now time.Time, maxAge time.Duration) (bool, string) {
	if err != nil {
		return false, err.Error()
	}
	rawCreatedAt, ok := payload["created_at"].(string)
	if !ok || rawCreatedAt == "" {
		return false, "created_at is missing"
	}
	createdAt, err := parseTime(rawCreatedAt)
	if err != nil {
		return false, fmt.Sprintf("created_at is invalid: %v", err)
	}
	age := now.Sub(createdAt)
	if age < 0 {
		return false, "created_at is in the future"
	}
	if age > maxAge {
		return false, fmt.Sprintf("state is stale: age=%s max=%s", age.Round(time.Second), maxAge)
	}
	archive, _ := payload["archive_name"].(string)
	digest, _ := payload["sha256"].(string)
	size, _ := payload["bytes"].(float64)
	if archive == "" || len(digest) != 64 || size <= 0 {
		return false, "archive metadata is incomplete"
	}
	return true, fmt.Sprintf("archive=%s age=%s", archive, age.Round(time.Second))
}

func parseTime(value string) (time.Time, error) {
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return time.Time{}, err
	}
	return parsed.UTC(), nil
}

func readJSONFile(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	if payload == nil {
		return nil, errors.New("JSON object is empty")
	}
	return payload, nil
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]any{"ok": false, "error": err.Error()})
}
