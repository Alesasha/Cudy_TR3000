package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"html"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

const (
	beginMarker = "# BEGIN cudy-router-agent"
	endMarker   = "# END cudy-router-agent"
)

type previewResponse struct {
	OK               bool               `json:"ok"`
	Configured       bool               `json:"configured"`
	DeviceID         string             `json:"device_id"`
	UserID           string             `json:"user_id"`
	Source           string             `json:"source"`
	ControlError     string             `json:"control_error"`
	CacheAge         int64              `json:"cache_age_seconds"`
	Routes           []routePreview     `json:"routes"`
	TransportPlan    []transportPreview `json:"transport_plan"`
	CriticalServices []criticalService  `json:"critical_services"`
	Warnings         []string           `json:"warnings"`
}

type criticalService struct {
	ServiceKey     string   `json:"service_key"`
	Label          string   `json:"label"`
	Targets        []string `json:"targets"`
	SuccessPattern string   `json:"success_pattern,omitempty"`
	FailurePattern string   `json:"failure_pattern,omitempty"`
}

type routePreview struct {
	Kind       string `json:"kind"`
	Target     string `json:"target"`
	ServerID   string `json:"server_id"`
	Interface  string `json:"interface"`
	Applicable bool   `json:"applicable"`
	Warning    string `json:"warning"`
}

type transportPreview struct {
	ServerID           string `json:"server_id"`
	Interface          string `json:"interface"`
	InterfacePresent   bool   `json:"interface_present"`
	InterfaceSupported bool   `json:"interface_supported"`
	Applicable         bool   `json:"applicable"`
}

type desiredState struct {
	SchemaVersion    int                 `json:"schema_version"`
	GeneratedAt      string              `json:"generated_at"`
	PolicySource     string              `json:"policy_source"`
	CacheAge         int64               `json:"cache_age_seconds"`
	Groups           map[string]routeSet `json:"groups"`
	ServerIDs        map[string]string   `json:"server_ids_by_interface,omitempty"`
	CriticalServices []criticalService   `json:"critical_services,omitempty"`
	Warnings         []string            `json:"warnings,omitempty"`
	Blockers         []string            `json:"blockers,omitempty"`
	Transports       []transportAction   `json:"transport_actions,omitempty"`
}

type transportAction struct {
	ServerID          string `json:"server_id"`
	Interface         string `json:"interface"`
	TransportType     string `json:"transport_type"`
	Action            string `json:"action"`
	ConfigPath        string `json:"config_path"`
	Service           string `json:"service"`
	RequiresBootstrap bool   `json:"requires_bootstrap,omitempty"`
}

type cachedPolicy struct {
	CachedAt string `json:"cached_at"`
	Config   struct {
		TransportPlan []rawTransport `json:"transport_plan"`
	} `json:"config"`
}

type rawTransport struct {
	ServerID      string         `json:"server_id"`
	InterfaceName string         `json:"interface_name"`
	TransportType string         `json:"transport_type"`
	Config        map[string]any `json:"config"`
}

type routeSet struct {
	Domains []string `json:"domains,omitempty"`
	IPs     []string `json:"ips,omitempty"`
}

type diffEntry struct {
	Path    string   `json:"path"`
	Added   []string `json:"added,omitempty"`
	Removed []string `json:"removed,omitempty"`
}

type statusFile struct {
	Mode                    string      `json:"mode"`
	OK                      bool        `json:"ok"`
	UpdatedAt               string      `json:"updated_at"`
	PolicySource            string      `json:"policy_source,omitempty"`
	RouteCount              int         `json:"route_count"`
	ChangedFiles            int         `json:"changed_files"`
	CriticalServiceCount    int         `json:"critical_service_count"`
	CriticalServicesOK      bool        `json:"critical_services_ok"`
	CriticalServiceFailures []string    `json:"critical_service_failures,omitempty"`
	Applied                 bool        `json:"applied"`
	RolledBack              bool        `json:"rolled_back"`
	ProbeJobsClaimed        int         `json:"probe_jobs_claimed"`
	ProbeJobsCompleted      int         `json:"probe_jobs_completed"`
	ProbeJobsFailed         int         `json:"probe_jobs_failed"`
	Error                   string      `json:"error,omitempty"`
	Warnings                []string    `json:"warnings,omitempty"`
	Diff                    []diffEntry `json:"diff,omitempty"`
}

type options struct {
	Mode                   string
	PreviewURL             string
	StateDir               string
	OverrideDir            string
	ApplyCommand           string
	BootstrapCommand       string
	HealthURL              string
	PolicyCache            string
	SingBoxDir             string
	ControlURL             string
	TokenFile              string
	ProbeLimit             int
	AllowApply             bool
	AuthoritativeOverrides bool
	AllowTransportPrepare  bool
	Once                   bool
	PollInterval           time.Duration
}

type agent struct {
	opts       options
	httpClient *http.Client
	runCommand func(context.Context, string) error
	now        func() time.Time
}

type probeJob struct {
	ID                 string   `json:"id"`
	Domain             string   `json:"domain"`
	URL                string   `json:"url"`
	CandidateServerIDs []string `json:"candidate_server_ids"`
	ConnectTimeout     int      `json:"connect_timeout"`
	MaxTime            int      `json:"max_time"`
	SuccessPattern     string   `json:"success_pattern,omitempty"`
	FailurePattern     string   `json:"failure_pattern,omitempty"`
}

type probeJobsResponse struct {
	Jobs []probeJob `json:"jobs"`
}

type probeSummary struct {
	Claimed   int
	Completed int
	Failed    int
}

var geoBlockPatterns = []string{
	"gemini isn't currently supported in your country",
	"isn't currently supported in your country",
	"not currently supported in your country",
	"not available in your country",
	"services are not available in your country",
	"country is not supported",
	"unsupported country",
}

func main() {
	var opts options
	flag.StringVar(&opts.Mode, "mode", "disabled", "disabled, observe, prepare, or apply")
	flag.StringVar(&opts.PreviewURL, "preview-url", "http://127.0.0.1:8765/api/cudy/agent-preview", "sanitized Cudy policy preview URL")
	flag.StringVar(&opts.StateDir, "state-dir", "/var/lib/cudy-router-agent", "root-only state and transaction directory")
	flag.StringVar(&opts.OverrideDir, "override-dir", "/etc/pbr-overrides", "PBR override directory")
	flag.StringVar(&opts.ApplyCommand, "apply-command", "/usr/bin/cudy-pbr-fast-apply", "command run after an override-only update")
	flag.StringVar(&opts.BootstrapCommand, "bootstrap-command", "/usr/bin/cudy-pbr-safe-restart", "command used when a new transport interface must be registered")
	flag.StringVar(&opts.HealthURL, "health-url", "http://127.0.0.1:8765/healthz", "post-apply health URL")
	flag.StringVar(&opts.PolicyCache, "policy-cache", "/var/lib/cudy-fallback/agent-config-cache.json", "root-only full policy cache used to prepare missing transports")
	flag.StringVar(&opts.SingBoxDir, "sing-box-dir", "/etc/sing-box", "managed sing-box config directory")
	flag.StringVar(&opts.ControlURL, "control-url", "http://127.0.0.1:18765", "primary control API used for status and probe jobs")
	flag.StringVar(&opts.TokenFile, "token-file", "/etc/cudy-fallback/agent.token", "root-only agent token file")
	flag.IntVar(&opts.ProbeLimit, "probe-limit", 2, "maximum probe jobs claimed per cycle; zero disables probing")
	flag.BoolVar(&opts.AllowApply, "allow-apply", false, "required safety gate for apply mode")
	flag.BoolVar(&opts.AuthoritativeOverrides, "authoritative-overrides", false, "replace all safe force-*.domains/ips files with the control-server plan")
	flag.BoolVar(&opts.AllowTransportPrepare, "allow-transport-prepare", false, "required safety gate for one-shot transport preparation")
	flag.BoolVar(&opts.Once, "once", false, "run one cycle and exit")
	flag.DurationVar(&opts.PollInterval, "poll-interval", time.Minute, "policy poll interval")
	flag.Parse()

	a := &agent{
		opts:       opts,
		httpClient: newAgentHTTPClient(),
		runCommand: shellCommand,
		now:        func() time.Time { return time.Now().UTC() },
	}
	if err := a.validate(); err != nil {
		log.Fatal(err)
	}
	if opts.Once {
		if err := a.cycle(context.Background()); err != nil {
			log.Fatal(err)
		}
		return
	}
	for {
		if err := a.cycle(context.Background()); err != nil {
			log.Printf("cycle failed: %v", err)
		}
		time.Sleep(opts.PollInterval)
	}
}

func newAgentHTTPClient() *http.Client {
	// The local fallback preview has a 20-second server-side deadline because
	// it may fall back from a stalled live control request to cached policy.
	return &http.Client{Timeout: 25 * time.Second}
}

func (a *agent) validate() error {
	switch a.opts.Mode {
	case "disabled", "observe":
	case "prepare":
		if !a.opts.AllowTransportPrepare || !a.opts.Once {
			return errors.New("prepare mode requires --allow-transport-prepare and --once")
		}
	case "apply":
		if !a.opts.AllowApply {
			return errors.New("apply mode requires --allow-apply")
		}
	default:
		return fmt.Errorf("unsupported mode %q", a.opts.Mode)
	}
	if a.opts.PollInterval <= 0 {
		return errors.New("poll interval must be positive")
	}
	if a.opts.ProbeLimit < 0 || a.opts.ProbeLimit > 10 {
		return errors.New("probe limit must be between zero and ten")
	}
	return nil
}

func (a *agent) cycle(ctx context.Context) error {
	if err := os.MkdirAll(a.opts.StateDir, 0o700); err != nil {
		return err
	}
	status := statusFile{Mode: a.opts.Mode, UpdatedAt: a.now().Format(time.RFC3339)}
	if a.opts.Mode == "disabled" {
		status.OK = true
		return writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
	}

	preview, err := a.fetchPreview(ctx)
	if err != nil {
		status.Error = err.Error()
		_ = writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
		return err
	}
	actions, transportUpdates, preparable, err := a.planTransports(preview)
	if err != nil {
		status.Error = err.Error()
		_ = writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
		return err
	}
	if err := a.validateTransportConfigs(ctx, transportUpdates); err != nil {
		status.Error = err.Error()
		_ = writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
		return err
	}
	desired, err := buildDesired(preview, preparable, a.now())
	if err != nil {
		status.Error = err.Error()
		_ = writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
		return err
	}
	desired.Transports = actions
	status.PolicySource = desired.PolicySource
	status.Warnings = desired.Warnings
	status.RouteCount = desiredRouteCount(desired)
	status.CriticalServiceCount = len(desired.CriticalServices)
	if preview.Source == "live" && a.opts.ProbeLimit > 0 {
		probeResult, probeErr := a.processProbeJobs(ctx, preview)
		status.ProbeJobsClaimed = probeResult.Claimed
		status.ProbeJobsCompleted = probeResult.Completed
		status.ProbeJobsFailed = probeResult.Failed
		if probeErr != nil {
			status.Warnings = append(status.Warnings, "probe jobs: "+probeErr.Error())
		}
	}
	if err := writeJSONAtomic(filepath.Join(a.opts.StateDir, "desired.json"), desired, 0o600); err != nil {
		return err
	}

	diff, updates, err := a.planUpdates(desired)
	if err != nil {
		return err
	}
	for path, data := range transportUpdates {
		if current, readErr := os.ReadFile(path); readErr != nil || !bytes.Equal(current, data) {
			updates[path] = data
			diff = append(diff, diffEntry{Path: path, Added: []string{"<managed transport artifact>"}})
		}
	}
	sort.Slice(diff, func(i, j int) bool { return diff[i].Path < diff[j].Path })
	status.Diff = diff
	status.ChangedFiles = len(updates)
	if err := writeJSONAtomic(filepath.Join(a.opts.StateDir, "diff.json"), diff, 0o600); err != nil {
		return err
	}
	if a.opts.Mode == "observe" {
		status.CriticalServiceFailures = a.checkCriticalServices(ctx, desired.CriticalServices, desired.Groups)
		status.CriticalServicesOK = len(desired.CriticalServices) > 0 && len(status.CriticalServiceFailures) == 0
		status.OK = len(desired.Blockers) == 0 && len(desired.CriticalServices) > 0
		if len(desired.Blockers) > 0 {
			status.Error = "policy has blockers: " + strings.Join(desired.Blockers, "; ")
		} else if len(desired.CriticalServices) == 0 {
			status.Error = "critical service preflight is not configured"
		} else if len(status.CriticalServiceFailures) > 0 {
			status.Warnings = append(status.Warnings, "critical service preflight failed: "+strings.Join(status.CriticalServiceFailures, "; "))
		}
		return a.persistStatus(ctx, status, desired)
	}
	if len(desired.Blockers) > 0 {
		err := fmt.Errorf("refusing %s while policy has blockers: %s", a.opts.Mode, strings.Join(desired.Blockers, "; "))
		status.Error = err.Error()
		_ = a.persistStatus(ctx, status, desired)
		return err
	}
	if a.opts.Mode == "prepare" {
		rolledBack, prepareErr := a.prepareTransportTransaction(ctx, transportUpdates, desired.Transports)
		status.Applied = prepareErr == nil
		status.RolledBack = rolledBack
		status.OK = prepareErr == nil
		status.CriticalServicesOK = prepareErr == nil
		if prepareErr != nil {
			status.Error = prepareErr.Error()
		}
		_ = a.persistStatus(ctx, status, desired)
		return prepareErr
	}
	if len(updates) == 0 {
		if err := a.healthCheck(ctx, desired.CriticalServices, desired.Groups); err != nil {
			status.Error = err.Error()
			_ = a.persistStatus(ctx, status, desired)
			return err
		}
		status.CriticalServicesOK = true
		status.OK = true
		return a.persistStatus(ctx, status, desired)
	}

	rolledBack, err := a.applyTransaction(ctx, updates, desired.Transports, desired.CriticalServices, desired.Groups)
	status.Applied = err == nil
	status.RolledBack = rolledBack
	status.OK = err == nil
	status.CriticalServicesOK = err == nil
	if err != nil {
		status.Error = err.Error()
	}
	_ = a.persistStatus(ctx, status, desired)
	return err
}

func (a *agent) persistStatus(ctx context.Context, status statusFile, desired desiredState) error {
	if err := a.postAgentStatus(ctx, status, desired); err != nil {
		status.Warnings = append(status.Warnings, "status post: "+err.Error())
	}
	return writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
}

func (a *agent) fetchPreview(ctx context.Context) (previewResponse, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.opts.PreviewURL, nil)
	if err != nil {
		return previewResponse{}, err
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return previewResponse{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return previewResponse{}, fmt.Errorf("preview returned HTTP %d", resp.StatusCode)
	}
	var result previewResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&result); err != nil {
		return result, err
	}
	if !result.OK || !result.Configured || (result.Source != "live" && result.Source != "cache") {
		return result, fmt.Errorf("preview is not usable: ok=%t configured=%t source=%q", result.OK, result.Configured, result.Source)
	}
	return result, nil
}

func (a *agent) controlJSON(ctx context.Context, method, path string, requestBody any, responseBody any) error {
	tokenRaw, err := os.ReadFile(a.opts.TokenFile)
	if err != nil {
		return fmt.Errorf("read agent token: %w", err)
	}
	token := strings.TrimSpace(string(tokenRaw))
	if token == "" {
		return errors.New("agent token is empty")
	}
	var body io.Reader
	if requestBody != nil {
		raw, marshalErr := json.Marshal(requestBody)
		if marshalErr != nil {
			return marshalErr
		}
		body = bytes.NewReader(raw)
	}
	endpoint := strings.TrimRight(a.opts.ControlURL, "/") + "/" + strings.TrimLeft(path, "/")
	req, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "cudy-router-agent/0.2")
	if requestBody != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("control %s returned HTTP %d: %s", path, resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	if responseBody == nil {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return nil
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(responseBody)
}

func (a *agent) processProbeJobs(ctx context.Context, preview previewResponse) (probeSummary, error) {
	result := probeSummary{}
	var response probeJobsResponse
	path := fmt.Sprintf("/api/agent/probe-jobs?limit=%d", a.opts.ProbeLimit)
	requestCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	err := a.controlJSON(requestCtx, http.MethodGet, path, nil, &response)
	cancel()
	if err != nil {
		return result, err
	}
	result.Claimed = len(response.Jobs)
	for _, job := range response.Jobs {
		probeResult := a.runProbeJob(ctx, job, preview)
		winner, _ := probeResult["winner"].(map[string]any)
		if winner == nil {
			result.Failed++
		}
		payload := map[string]any{"job_id": job.ID, "result": probeResult}
		postCtx, postCancel := context.WithTimeout(ctx, 20*time.Second)
		postErr := a.controlJSON(postCtx, http.MethodPost, "/api/agent/probe-jobs/result", payload, &map[string]any{})
		postCancel()
		if postErr != nil {
			result.Failed++
			return result, fmt.Errorf("post probe %s: %w", job.ID, postErr)
		}
		result.Completed++
	}
	return result, nil
}

func (a *agent) runProbeJob(ctx context.Context, job probeJob, preview previewResponse) map[string]any {
	domain := strings.ToLower(strings.TrimSpace(job.Domain))
	probeURL := strings.TrimSpace(job.URL)
	if probeURL == "" && domain != "" {
		probeURL = "https://" + domain + "/"
	}
	connectTimeout := boundedSeconds(job.ConnectTimeout, 5, 1, 30)
	maxTime := boundedSeconds(job.MaxTime, 12, 1, 60)
	successPattern, failurePattern := probePatterns(job, preview.CriticalServices, probeURL, domain)
	transports := map[string]transportPreview{}
	for _, transport := range preview.TransportPlan {
		transports[transport.ServerID] = transport
	}
	checks := []map[string]any{}
	var winner map[string]any
	for index, serverID := range job.CandidateServerIDs {
		transport, exists := transports[serverID]
		check := map[string]any{
			"server_id": serverID,
			"index":     index + 1,
			"interface": transport.Interface,
			"ok":        false,
		}
		if !exists || !transport.InterfacePresent {
			check["status"] = "no_interface"
			checks = append(checks, check)
			continue
		}
		probe := a.probeTarget(ctx, probeURL, transport.Interface, connectTimeout, maxTime, successPattern, failurePattern)
		for key, value := range probe {
			check[key] = value
		}
		if check["ok"] == true {
			check["status"] = "ok"
			if winner == nil || intValue(check["time_total_ms"], 1<<30) < intValue(winner["time_total_ms"], 1<<30) {
				winner = check
			}
		} else if check["status"] == nil {
			check["status"] = firstNonEmpty(anyString(check["semantic_status"]), "failed")
		}
		checks = append(checks, check)
	}
	return map[string]any{
		"schema_version":       1,
		"agent_version":        "0.2",
		"platform":             "openwrt",
		"device_id":            preview.DeviceID,
		"domain":               domain,
		"url":                  probeURL,
		"candidate_server_ids": job.CandidateServerIDs,
		"winner":               winner,
		"checks":               checks,
		"ok":                   winner != nil,
	}
}

func (a *agent) probeTarget(ctx context.Context, target, iface string, connectTimeout, maxTime time.Duration, successPattern, failurePattern string) map[string]any {
	parsed, err := url.Parse(target)
	if err != nil || parsed.Host == "" {
		return map[string]any{"error": "invalid probe URL", "time_total_ms": 0}
	}
	dialer := &net.Dialer{Timeout: connectTimeout}
	proxyURL := a.probeProxyForInterface(iface)
	if proxyURL == nil {
		if err := bindDialerToInterface(dialer, iface); err != nil {
			return map[string]any{"error": err.Error(), "time_total_ms": 0}
		}
	}
	started := time.Now()
	requestCtx, cancel := context.WithTimeout(ctx, maxTime)
	defer cancel()
	if parsed.Scheme == "tcp" {
		var connection net.Conn
		var dialErr error
		if proxyURL != nil {
			connection, dialErr = dialHTTPConnectProxy(requestCtx, dialer, proxyURL, parsed.Host)
		} else {
			connection, dialErr = dialer.DialContext(requestCtx, "tcp", parsed.Host)
		}
		elapsed := time.Since(started)
		if dialErr != nil {
			return map[string]any{"probe_type": "tcp", "error": dialErr.Error(), "time_total_ms": elapsed.Milliseconds()}
		}
		_ = connection.Close()
		return map[string]any{"probe_type": "tcp", "ok": true, "time_total_ms": elapsed.Milliseconds()}
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return map[string]any{"error": "unsupported probe scheme", "time_total_ms": 0}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.DialContext = dialer.DialContext
	if proxyURL != nil {
		transport.Proxy = http.ProxyURL(proxyURL)
	}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: maxTime}
	req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, target, nil)
	if err != nil {
		return map[string]any{"error": err.Error(), "time_total_ms": 0}
	}
	req.Header.Set("Range", "bytes=0-1048575")
	req.Header.Set("User-Agent", "cudy-router-agent/0.2")
	resp, err := client.Do(req)
	if err != nil {
		return map[string]any{"probe_type": "http", "error": err.Error(), "time_total_ms": time.Since(started).Milliseconds()}
	}
	body, readErr := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	elapsed := time.Since(started)
	result := map[string]any{
		"probe_type":      map[bool]string{true: "http_proxy", false: "http_interface"}[proxyURL != nil],
		"http_code":       resp.StatusCode,
		"time_total_ms":   elapsed.Milliseconds(),
		"bytes":           len(body),
		"speed_mbps":      float64(len(body)*8) / maxFloat(elapsed.Seconds(), 0.001) / 1_000_000,
		"semantic_status": "ok",
	}
	if readErr != nil {
		result["error"] = readErr.Error()
		return result
	}
	text := string(body)
	success, patternErr := patternMatches(successPattern, text, true)
	if patternErr != nil {
		result["semantic_status"] = "invalid_success_pattern"
		result["error"] = patternErr.Error()
		return result
	}
	failure, patternErr := patternMatches(failurePattern, text, false)
	if patternErr != nil {
		result["semantic_status"] = "invalid_failure_pattern"
		result["error"] = patternErr.Error()
		return result
	}
	geoBlocked := bodyHasGeoBlock(text)
	result["ok"] = resp.StatusCode >= 200 && resp.StatusCode < 500 && success && !failure && !geoBlocked
	if geoBlocked {
		result["semantic_status"] = "geo_blocked"
	} else if failure {
		result["semantic_status"] = "failure_pattern"
	} else if !success {
		result["semantic_status"] = "success_pattern_missing"
	}
	return result
}

func (a *agent) probeProxyForInterface(iface string) *url.URL {
	if iface == "" || a.opts.PolicyCache == "" {
		return nil
	}
	data, err := os.ReadFile(a.opts.PolicyCache)
	if err != nil {
		return nil
	}
	var cache cachedPolicy
	if json.Unmarshal(data, &cache) != nil {
		return nil
	}
	for _, raw := range cache.Config.TransportPlan {
		if raw.InterfaceName != iface || raw.TransportType != "http-proxy-tun" {
			continue
		}
		proxyType := firstNonEmpty(anyString(raw.Config["proxy_type"]), "http")
		if proxyType != "http" {
			return nil
		}
		host := anyString(raw.Config["server"])
		port, ok := anyInt(raw.Config["server_port"])
		if host == "" || !ok || port < 1 || port > 65535 {
			return nil
		}
		result := &url.URL{Scheme: "http", Host: net.JoinHostPort(host, fmt.Sprint(port))}
		username := anyString(raw.Config["username"])
		password := anyString(raw.Config["password"])
		if username != "" {
			result.User = url.UserPassword(username, password)
		}
		return result
	}
	return nil
}

func dialHTTPConnectProxy(ctx context.Context, dialer *net.Dialer, proxyURL *url.URL, target string) (net.Conn, error) {
	connection, err := dialer.DialContext(ctx, "tcp", proxyURL.Host)
	if err != nil {
		return nil, err
	}
	closeOnError := true
	defer func() {
		if closeOnError {
			_ = connection.Close()
		}
	}()
	header := ""
	if proxyURL.User != nil {
		password, _ := proxyURL.User.Password()
		token := base64.StdEncoding.EncodeToString([]byte(proxyURL.User.Username() + ":" + password))
		header = "Proxy-Authorization: Basic " + token + "\r\n"
	}
	if _, err := fmt.Fprintf(connection, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n%s\r\n", target, target, header); err != nil {
		return nil, err
	}
	response, err := http.ReadResponse(bufio.NewReader(connection), &http.Request{Method: http.MethodConnect})
	if err != nil {
		return nil, err
	}
	if response.Body != nil {
		_ = response.Body.Close()
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP proxy CONNECT returned %s", response.Status)
	}
	closeOnError = false
	return connection, nil
}

func (a *agent) postAgentStatus(ctx context.Context, status statusFile, desired desiredState) error {
	domains := []map[string]any{}
	ipRoutes := []map[string]any{}
	interfaces := []string{}
	for iface, routes := range desired.Groups {
		interfaces = append(interfaces, iface)
		serverID := firstNonEmpty(desired.ServerIDs[iface], iface)
		for _, domain := range routes.Domains {
			domains = append(domains, map[string]any{"domain": domain, "server_id": serverID, "ok": status.OK})
		}
		for _, target := range routes.IPs {
			ipRoutes = append(ipRoutes, map[string]any{"target_cidr": target, "server_id": serverID, "ok": status.OK})
		}
	}
	sort.Strings(interfaces)
	payload := map[string]any{
		"schema_version": 1,
		"platform":       "openwrt",
		"agent_version":  "0.2",
		"vpn_interfaces": interfaces,
		"routes": map[string]any{
			"domain_count":   len(domains),
			"ip_route_count": len(ipRoutes),
		},
		"domain_routes": domains,
		"ip_routes":     ipRoutes,
		"health": map[string]any{
			"ok":      status.OK,
			"mode":    status.Mode,
			"applied": status.Applied,
		},
		"capabilities": map[string]any{
			"can_probe":             a.opts.ProbeLimit > 0,
			"can_route":             a.opts.Mode == "apply",
			"can_manage_transports": canManageTransports(a.opts.Mode),
		},
		"errors": []string{},
	}
	if status.Error != "" {
		payload["errors"] = []string{status.Error}
	}
	requestCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	return a.controlJSON(requestCtx, http.MethodPost, "/api/agent/status", payload, &map[string]any{})
}

func canManageTransports(mode string) bool {
	return strings.EqualFold(strings.TrimSpace(mode), "apply")
}

func probePatterns(job probeJob, services []criticalService, target, domain string) (string, string) {
	if job.SuccessPattern != "" || job.FailurePattern != "" {
		return job.SuccessPattern, job.FailurePattern
	}
	host := strings.ToLower(strings.TrimSuffix(domain, "."))
	if parsed, err := url.Parse(target); err == nil && parsed.Hostname() != "" {
		host = strings.ToLower(strings.TrimSuffix(parsed.Hostname(), "."))
	}
	for _, service := range services {
		for _, serviceTarget := range service.Targets {
			parsed, err := url.Parse(serviceTarget)
			if err != nil {
				continue
			}
			serviceHost := strings.ToLower(strings.TrimSuffix(parsed.Hostname(), "."))
			if serviceHost != "" && (host == serviceHost || strings.HasSuffix(host, "."+serviceHost) || strings.HasSuffix(serviceHost, "."+host)) {
				return service.SuccessPattern, service.FailurePattern
			}
		}
	}
	return "", ""
}

func bodyHasGeoBlock(body string) bool {
	normalized := strings.ToLower(html.UnescapeString(body))
	for _, apostrophe := range []string{"‘", "’", "ʼ", "`"} {
		normalized = strings.ReplaceAll(normalized, apostrophe, "'")
	}
	for _, pattern := range geoBlockPatterns {
		normalizedPattern := strings.ToLower(html.UnescapeString(pattern))
		normalizedPattern = strings.ReplaceAll(normalizedPattern, "’", "'")
		if strings.Contains(normalized, normalizedPattern) {
			return true
		}
	}
	return false
}

func patternMatches(pattern, body string, emptyValue bool) (bool, error) {
	if pattern == "" {
		return emptyValue, nil
	}
	compiled, err := regexp.Compile("(?im)" + pattern)
	if err != nil {
		return false, err
	}
	return compiled.MatchString(body), nil
}

func boundedSeconds(value, fallback, minimum, maximum int) time.Duration {
	if value == 0 {
		value = fallback
	}
	if value < minimum {
		value = minimum
	}
	if value > maximum {
		value = maximum
	}
	return time.Duration(value) * time.Second
}

func intValue(value any, fallback int) int {
	switch typed := value.(type) {
	case int:
		return typed
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	default:
		return fallback
	}
}

func maxFloat(left, right float64) float64 {
	if left > right {
		return left
	}
	return right
}

func (a *agent) planTransports(preview previewResponse) ([]transportAction, map[string][]byte, map[string]bool, error) {
	needed := map[string]transportPreview{}
	transportByServer := map[string]transportPreview{}
	for _, item := range preview.TransportPlan {
		transportByServer[item.ServerID] = item
	}
	for _, route := range preview.Routes {
		if route.ServerID == "direct" {
			continue
		}
		item, ok := transportByServer[route.ServerID]
		if !ok {
			if route.Applicable {
				continue
			}
			return nil, nil, nil, fmt.Errorf("route %s needs missing transport %s", route.Target, route.ServerID)
		}
		needed[item.ServerID] = item
	}
	if len(needed) == 0 {
		return nil, map[string][]byte{}, map[string]bool{}, nil
	}
	data, err := os.ReadFile(a.opts.PolicyCache)
	if err != nil {
		return nil, nil, nil, fmt.Errorf("read policy cache: %w", err)
	}
	var cache cachedPolicy
	if err := json.Unmarshal(data, &cache); err != nil {
		return nil, nil, nil, fmt.Errorf("decode policy cache: %w", err)
	}
	cachedAt, err := time.Parse(time.RFC3339, cache.CachedAt)
	if err != nil || a.now().Sub(cachedAt) > 24*time.Hour {
		return nil, nil, nil, fmt.Errorf("policy cache is stale or invalid: cached_at=%q", cache.CachedAt)
	}
	actions := []transportAction{}
	updates := map[string][]byte{}
	preparable := map[string]bool{}
	rawByServer := map[string]rawTransport{}
	for _, raw := range cache.Config.TransportPlan {
		rawByServer[raw.ServerID] = raw
	}
	serverIDs := make([]string, 0, len(needed))
	for serverID := range needed {
		serverIDs = append(serverIDs, serverID)
	}
	sort.Strings(serverIDs)
	for _, serverID := range serverIDs {
		previewItem := needed[serverID]
		raw, ok := rawByServer[serverID]
		if !ok {
			if !previewItem.Applicable {
				return nil, nil, nil, fmt.Errorf("missing raw transport config for %s", serverID)
			}
			continue
		}
		iface := firstNonEmpty(raw.InterfaceName, previewItem.Interface)
		if !safeName(iface) || !safeName(raw.ServerID) {
			return nil, nil, nil, fmt.Errorf("unsafe transport identity server=%q interface=%q", raw.ServerID, iface)
		}
		if !managedSingBoxTransport(raw.TransportType) {
			if previewItem.InterfacePresent || previewItem.Applicable {
				// Existing non-sing-box transports (for example AmneziaWG) are
				// usable, but their lifecycle remains outside this agent.
				if !previewItem.Applicable {
					preparable[iface] = true
				}
				continue
			}
			return nil, nil, nil, fmt.Errorf("cannot prepare missing unsupported transport %s type=%s", raw.ServerID, raw.TransportType)
		}
		config, err := renderSingBox(raw, iface)
		if err != nil {
			return nil, nil, nil, fmt.Errorf("prepare %s: %w", raw.ServerID, err)
		}
		configPath := filepath.Join(a.opts.SingBoxDir, iface+".json")
		service := "sing-box-" + iface
		initPath := filepath.Join("/etc/init.d", service)
		initData := []byte(renderTransportInit(configPath))
		configChanged := fileDiffers(configPath, config)
		initChanged := fileDiffers(initPath, initData)
		if configChanged {
			updates[configPath] = config
		}
		if initChanged {
			updates[initPath] = initData
		}
		missing := !previewItem.Applicable
		if missing {
			preparable[iface] = true
		}
		if missing || configChanged || initChanged {
			action := "refresh-and-restart"
			if missing {
				action = "prepare-and-start"
			}
			actions = append(actions, transportAction{
				ServerID: raw.ServerID, Interface: iface, TransportType: raw.TransportType,
				Action: action, ConfigPath: configPath, Service: service, RequiresBootstrap: missing,
			})
		}
	}
	for serverID, item := range needed {
		if !item.Applicable && !preparable[item.Interface] {
			return nil, nil, nil, fmt.Errorf("missing supported raw transport config for %s", serverID)
		}
	}
	sort.Slice(actions, func(i, j int) bool { return actions[i].ServerID < actions[j].ServerID })
	return actions, updates, preparable, nil
}

func managedSingBoxTransport(transportType string) bool {
	switch transportType {
	case "http-proxy-tun", "vless-reality-tun", "sing-box-json":
		return true
	default:
		return false
	}
}

func fileDiffers(path string, desired []byte) bool {
	current, err := os.ReadFile(path)
	return err != nil || !bytes.Equal(current, desired)
}

func (a *agent) validateTransportConfigs(ctx context.Context, updates map[string][]byte) error {
	validationDir := filepath.Join(a.opts.StateDir, "validation")
	if err := os.MkdirAll(validationDir, 0o700); err != nil {
		return err
	}
	defer os.RemoveAll(validationDir)
	paths := make([]string, 0, len(updates))
	for path := range updates {
		if strings.HasSuffix(path, ".json") {
			paths = append(paths, path)
		}
	}
	sort.Strings(paths)
	for index, path := range paths {
		tempPath := filepath.Join(validationDir, fmt.Sprintf("%03d.json", index))
		if err := os.WriteFile(tempPath, updates[path], 0o600); err != nil {
			return err
		}
		checkCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
		err := a.runCommand(checkCtx, fmt.Sprintf("/usr/bin/sing-box check -c %s", shellQuote(tempPath)))
		cancel()
		if err != nil {
			return fmt.Errorf("sing-box check failed for %s: %w", filepath.Base(path), err)
		}
	}
	return nil
}

func renderSingBox(raw rawTransport, iface string) ([]byte, error) {
	host := anyString(raw.Config["server"])
	port, ok := anyInt(raw.Config["server_port"])
	if host == "" || !ok || port < 1 || port > 65535 {
		return nil, errors.New("missing server or server_port")
	}
	tun := map[string]any{
		"type": "tun", "tag": iface + "-tun", "interface_name": iface,
		"address": []string{tunAddress(iface, map[string]int{"http-proxy-tun": 41, "vless-reality-tun": 43}[raw.TransportType])},
		"mtu":     1400, "auto_route": false, "strict_route": false, "stack": "gvisor",
	}
	var proxy map[string]any
	switch raw.TransportType {
	case "http-proxy-tun":
		proxyType := firstNonEmpty(anyString(raw.Config["proxy_type"]), "http")
		proxy = map[string]any{"type": proxyType, "tag": "proxy-out", "server": host, "server_port": port}
	case "vless-reality-tun":
		tls, _ := raw.Config["tls"].(map[string]any)
		reality, _ := tls["reality"].(map[string]any)
		uuid := anyString(raw.Config["uuid"])
		serverName := anyString(tls["server_name"])
		publicKey := anyString(reality["public_key"])
		if uuid == "" || serverName == "" || publicKey == "" {
			return nil, errors.New("incomplete VLESS Reality settings")
		}
		proxy = map[string]any{
			"type": "vless", "tag": "proxy-out", "server": host, "server_port": port, "uuid": uuid,
			"tls": map[string]any{
				"enabled": true, "server_name": serverName,
				"utls":    map[string]any{"enabled": true, "fingerprint": "chrome"},
				"reality": map[string]any{"enabled": true, "public_key": publicKey, "short_id": anyString(reality["short_id"])},
			},
		}
		if flow := anyString(raw.Config["flow"]); flow != "" {
			proxy["flow"] = flow
		}
	case "sing-box-json":
		return json.MarshalIndent(raw.Config, "", "  ")
	default:
		return nil, fmt.Errorf("unsupported transport type %q", raw.TransportType)
	}
	config := map[string]any{
		"log":       map[string]any{"level": "info", "timestamp": true},
		"inbounds":  []any{tun},
		"outbounds": []any{proxy, map[string]any{"type": "direct", "tag": "direct"}, map[string]any{"type": "block", "tag": "block"}},
		"route": map[string]any{
			"auto_detect_interface": true,
			"rules":                 []any{map[string]any{"ip_cidr": []string{host + "/32"}, "outbound": "direct"}},
			"final":                 "proxy-out",
		},
	}
	data, err := json.MarshalIndent(config, "", "  ")
	if err == nil {
		data = append(data, '\n')
	}
	return data, err
}

func renderTransportInit(configPath string) string {
	return fmt.Sprintf(`#!/bin/sh /etc/rc.common
USE_PROCD=1
START=94
STOP=11

start_service() {
  procd_open_instance
  procd_set_param command /usr/bin/sing-box run -c %s
  procd_set_param respawn 3600 5 5
  procd_set_param stdout 1
  procd_set_param stderr 1
  procd_close_instance
}
`, configPath)
}

func tunAddress(name string, base int) string {
	if base == 0 {
		base = 43
	}
	digest := sha256.Sum256([]byte(name))
	octet := 2 + int(digest[0])%230
	return fmt.Sprintf("172.%d.%d.1/30", base, octet)
}

func buildDesired(preview previewResponse, preparable map[string]bool, now time.Time) (desiredState, error) {
	desired := desiredState{
		SchemaVersion:    1,
		GeneratedAt:      now.UTC().Format(time.RFC3339),
		PolicySource:     preview.Source,
		CacheAge:         preview.CacheAge,
		Groups:           map[string]routeSet{},
		ServerIDs:        map[string]string{},
		CriticalServices: append([]criticalService{}, preview.CriticalServices...),
		Warnings:         append([]string{}, preview.Warnings...),
	}
	for _, service := range desired.CriticalServices {
		if len(service.Targets) == 0 {
			return desired, fmt.Errorf("critical service %q has no targets", service.ServiceKey)
		}
		if service.SuccessPattern != "" {
			if _, err := regexp.Compile(service.SuccessPattern); err != nil {
				return desired, fmt.Errorf("critical service %q success pattern: %w", service.ServiceKey, err)
			}
		}
		if service.FailurePattern != "" {
			if _, err := regexp.Compile(service.FailurePattern); err != nil {
				return desired, fmt.Errorf("critical service %q failure pattern: %w", service.ServiceKey, err)
			}
		}
	}
	for _, route := range preview.Routes {
		if strings.TrimSpace(route.Target) == "" {
			continue
		}
		iface := route.Interface
		if route.ServerID == "direct" {
			iface = "wan"
		}
		if iface == "" || !safeName(iface) {
			return desired, fmt.Errorf("route %q has unsafe or missing interface %q", route.Target, iface)
		}
		if route.ServerID != "direct" && !route.Applicable && !preparable[iface] {
			desired.Blockers = append(desired.Blockers, fmt.Sprintf("route %q is not applicable on %s: %s", route.Target, iface, route.Warning))
			continue
		}
		set := desired.Groups[iface]
		switch route.Kind {
		case "domain":
			set.Domains = append(set.Domains, normalizeLine(route.Target))
		case "ip":
			set.IPs = append(set.IPs, normalizeLine(route.Target))
		case "cleanup_ip":
			continue
		default:
			return desired, fmt.Errorf("unsupported route kind %q", route.Kind)
		}
		desired.Groups[iface] = set
		if current := desired.ServerIDs[iface]; current == "" || current == route.ServerID {
			desired.ServerIDs[iface] = route.ServerID
		} else {
			// A shared interface cannot be attributed to a single backend in the
			// status report. Keep the interface name instead of reporting a lie.
			desired.ServerIDs[iface] = iface
		}
	}
	for iface, set := range desired.Groups {
		set.Domains = uniqueSorted(set.Domains)
		set.IPs = uniqueSorted(set.IPs)
		desired.Groups[iface] = set
	}
	desired.Blockers = uniqueSorted(desired.Blockers)
	return desired, nil
}

func (a *agent) planUpdates(desired desiredState) ([]diffEntry, map[string][]byte, error) {
	if err := os.MkdirAll(a.opts.OverrideDir, 0o755); err != nil {
		return nil, nil, err
	}
	managed := map[string][]string{}
	for iface, set := range desired.Groups {
		managed[filepath.Join(a.opts.OverrideDir, "force-"+iface+".domains")] = set.Domains
		managed[filepath.Join(a.opts.OverrideDir, "force-"+iface+".ips")] = set.IPs
	}
	if a.opts.AuthoritativeOverrides {
		existing, err := authoritativeOverridePaths(a.opts.OverrideDir)
		if err != nil {
			return nil, nil, err
		}
		for _, path := range existing {
			if _, ok := managed[path]; !ok {
				managed[path] = nil
			}
		}
	}
	previous, _ := readManagedPaths(filepath.Join(a.opts.StateDir, "managed-paths.json"))
	for _, path := range previous {
		if _, ok := managed[path]; !ok {
			managed[path] = nil
		}
	}
	paths := make([]string, 0, len(managed))
	for path := range managed {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	diffs := []diffEntry{}
	updates := map[string][]byte{}
	for _, path := range paths {
		old, err := os.ReadFile(path)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, nil, err
		}
		oldManaged := extractManagedLines(string(old))
		newManaged := uniqueSorted(managed[path])
		merged := replaceManagedBlock(string(old), newManaged)
		if a.opts.AuthoritativeOverrides {
			oldManaged = extractOverrideLines(string(old))
			merged = renderManagedBlock(newManaged)
		}
		if !bytes.Equal(old, []byte(merged)) {
			updates[path] = []byte(merged)
			diffs = append(diffs, diffEntry{
				Path: path, Added: subtract(newManaged, oldManaged), Removed: subtract(oldManaged, newManaged),
			})
		}
	}
	currentPaths := make([]string, 0, len(managed))
	if a.opts.AuthoritativeOverrides {
		currentPaths = append(currentPaths, paths...)
	} else {
		for iface := range desired.Groups {
			currentPaths = append(currentPaths,
				filepath.Join(a.opts.OverrideDir, "force-"+iface+".domains"),
				filepath.Join(a.opts.OverrideDir, "force-"+iface+".ips"))
		}
	}
	sort.Strings(currentPaths)
	if err := writeJSONAtomic(filepath.Join(a.opts.StateDir, "managed-paths.next.json"), currentPaths, 0o600); err != nil {
		return nil, nil, err
	}
	return diffs, updates, nil
}

func (a *agent) prepareTransportTransaction(ctx context.Context, updates map[string][]byte, transports []transportAction) (bool, error) {
	if len(transports) == 0 {
		return false, nil
	}
	txDir := filepath.Join(a.opts.StateDir, "transport-transactions", a.now().Format("20060102T150405.000000000Z"))
	if err := os.MkdirAll(txDir, 0o700); err != nil {
		return false, err
	}
	type backup struct {
		Path, Backup string
		Existed      bool
		Mode         os.FileMode
	}
	pathSet := map[string]bool{"/etc/config/pbr": true}
	for path := range updates {
		pathSet[path] = true
	}
	paths := make([]string, 0, len(pathSet))
	for path := range pathSet {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	backups := make([]backup, 0, len(paths))
	for index, path := range paths {
		old, err := os.ReadFile(path)
		existed := err == nil
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return false, err
		}
		backupPath := filepath.Join(txDir, fmt.Sprintf("%03d.backup", index))
		if existed {
			if err := os.WriteFile(backupPath, old, 0o600); err != nil {
				return false, err
			}
		}
		mode := managedFileMode(path)
		if existed {
			if info, statErr := os.Stat(path); statErr == nil {
				mode = info.Mode().Perm()
			}
		}
		backups = append(backups, backup{Path: path, Backup: backupPath, Existed: existed, Mode: mode})
	}

	rollback := func(cause error) (bool, error) {
		for _, transport := range transports {
			_ = a.runCommand(context.Background(), fmt.Sprintf(
				"/etc/init.d/%[1]s stop 2>/dev/null || true; /etc/init.d/%[1]s disable 2>/dev/null || true",
				transport.Service,
			))
		}
		for _, item := range backups {
			if item.Existed {
				data, _ := os.ReadFile(item.Backup)
				_ = writeFileAtomic(item.Path, data, item.Mode)
			} else {
				_ = os.Remove(item.Path)
			}
		}
		for _, transport := range transports {
			for _, item := range backups {
				if item.Path == filepath.Join("/etc/init.d", transport.Service) && item.Existed {
					_ = a.runCommand(context.Background(), fmt.Sprintf(
						"chmod 0755 /etc/init.d/%[1]s && /etc/init.d/%[1]s enable && /etc/init.d/%[1]s restart",
						transport.Service,
					))
				}
			}
		}
		rollbackCtx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
		defer cancel()
		_ = a.runCommand(rollbackCtx, a.opts.BootstrapCommand)
		return true, cause
	}

	for path, data := range updates {
		if err := writeFileAtomic(path, data, managedFileMode(path)); err != nil {
			return rollback(fmt.Errorf("write %s failed: %w", path, err))
		}
	}
	for _, transport := range transports {
		command := fmt.Sprintf(
			"chmod 0755 /etc/init.d/%[1]s && /etc/init.d/%[1]s enable && /etc/init.d/%[1]s restart && "+
				"uci -q get pbr.config.supported_interface | tr ' ' '\\n' | grep -qxF '%[2]s' || uci add_list pbr.config.supported_interface='%[2]s'; "+
				"i=0; while [ ! -d /sys/class/net/%[2]s ] && [ $i -lt 20 ]; do sleep 1; i=$((i+1)); done; test -d /sys/class/net/%[2]s",
			transport.Service, transport.Interface,
		)
		transportCtx, cancel := context.WithTimeout(ctx, 35*time.Second)
		err := a.runCommand(transportCtx, command)
		cancel()
		if err != nil {
			return rollback(fmt.Errorf("transport %s failed: %w", transport.ServerID, err))
		}
	}
	if err := a.runCommand(ctx, "uci commit pbr"); err != nil {
		return rollback(fmt.Errorf("commit PBR transport interfaces: %w", err))
	}
	bootstrapCtx, cancel := context.WithTimeout(ctx, 180*time.Second)
	err := a.runCommand(bootstrapCtx, a.opts.BootstrapCommand)
	cancel()
	if err != nil {
		return rollback(fmt.Errorf("transport PBR bootstrap failed: %w", err))
	}
	if err := a.controlHealthCheck(ctx); err != nil {
		return rollback(fmt.Errorf("post-bootstrap control health failed: %w", err))
	}
	for _, transport := range transports {
		probe := a.probeTarget(ctx, "https://ifconfig.me/ip", transport.Interface, 5*time.Second, 15*time.Second, "", "")
		if probe["ok"] != true {
			return rollback(fmt.Errorf("transport %s health failed: %s", transport.ServerID, probeFailure(probe)))
		}
	}
	return false, nil
}

func probeFailure(probe map[string]any) string {
	if value := anyString(probe["error"]); value != "" {
		return value
	}
	return fmt.Sprintf("HTTP %v semantic=%s", probe["http_code"], anyString(probe["semantic_status"]))
}

func (a *agent) applyTransaction(ctx context.Context, updates map[string][]byte, transports []transportAction, criticalServices []criticalService, groups map[string]routeSet) (bool, error) {
	txDir := filepath.Join(a.opts.StateDir, "transactions", a.now().Format("20060102T150405.000000000Z"))
	if err := os.MkdirAll(txDir, 0o700); err != nil {
		return false, err
	}
	type backup struct {
		Path, Backup string
		Existed      bool
		Mode         os.FileMode
	}
	backups := []backup{}
	pathSet := make(map[string]bool, len(updates)+1)
	for path := range updates {
		pathSet[path] = true
	}
	bootstrapRequired := transportBootstrapRequired(transports) || !a.pbrDataplaneReady(ctx, groups)
	if bootstrapRequired {
		pathSet["/etc/config/pbr"] = true
	}
	paths := make([]string, 0, len(pathSet))
	for path := range pathSet {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	for index, path := range paths {
		old, err := os.ReadFile(path)
		existed := err == nil
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return false, err
		}
		backupPath := filepath.Join(txDir, fmt.Sprintf("%03d.backup", index))
		if existed {
			if err := os.WriteFile(backupPath, old, 0o600); err != nil {
				return false, err
			}
		}
		mode := managedFileMode(path)
		if existed {
			if info, statErr := os.Stat(path); statErr == nil {
				mode = info.Mode().Perm()
			}
		}
		backups = append(backups, backup{Path: path, Backup: backupPath, Existed: existed, Mode: mode})
	}
	rollback := func(cause error) (bool, error) {
		for _, transport := range transports {
			_ = a.runCommand(context.Background(), fmt.Sprintf(
				"/etc/init.d/%[1]s stop 2>/dev/null || true; /etc/init.d/%[1]s disable 2>/dev/null || true",
				transport.Service,
			))
		}
		for _, item := range backups {
			if item.Existed {
				data, _ := os.ReadFile(item.Backup)
				_ = writeFileAtomic(item.Path, data, item.Mode)
			} else {
				_ = os.Remove(item.Path)
			}
		}
		for _, transport := range transports {
			for _, item := range backups {
				if item.Path == filepath.Join("/etc/init.d", transport.Service) && item.Existed {
					_ = a.runCommand(context.Background(), fmt.Sprintf(
						"chmod 0755 /etc/init.d/%[1]s && /etc/init.d/%[1]s enable && /etc/init.d/%[1]s restart",
						transport.Service,
					))
				}
			}
		}
		rollbackTimeout := 45 * time.Second
		if bootstrapRequired {
			rollbackTimeout = 210 * time.Second
		}
		rollbackCtx, cancel := context.WithTimeout(context.Background(), rollbackTimeout)
		defer cancel()
		rollbackCommand := a.opts.ApplyCommand
		if bootstrapRequired {
			rollbackCommand = a.opts.BootstrapCommand
		}
		_ = a.runCommand(rollbackCtx, rollbackCommand)
		return true, cause
	}
	for _, path := range paths {
		if data, shouldWrite := updates[path]; shouldWrite {
			if err := writeFileAtomic(path, data, managedFileMode(path)); err != nil {
				return rollback(fmt.Errorf("write %s failed: %w", path, err))
			}
		}
	}
	for _, transport := range transports {
		command := fmt.Sprintf(
			"chmod 0755 /etc/init.d/%[1]s && /etc/init.d/%[1]s enable && /etc/init.d/%[1]s restart && "+
				"uci -q get pbr.config.supported_interface | tr ' ' '\\n' | grep -qxF '%[2]s' || { uci add_list pbr.config.supported_interface='%[2]s'; uci commit pbr; }; "+
				"i=0; while [ ! -d /sys/class/net/%[2]s ] && [ $i -lt 20 ]; do sleep 1; i=$((i+1)); done; test -d /sys/class/net/%[2]s",
			transport.Service, transport.Interface,
		)
		transportCtx, transportCancel := context.WithTimeout(ctx, 35*time.Second)
		err := a.runCommand(transportCtx, command)
		transportCancel()
		if err != nil {
			return rollback(fmt.Errorf("transport %s failed: %w", transport.ServerID, err))
		}
	}
	applyCommand := a.opts.ApplyCommand
	applyTimeout := 30 * time.Second
	if bootstrapRequired {
		applyCommand = a.opts.BootstrapCommand
		applyTimeout = 180 * time.Second
	}
	applyCtx, cancel := context.WithTimeout(ctx, applyTimeout)
	defer cancel()
	if err := a.runCommand(applyCtx, applyCommand); err != nil {
		return rollback(fmt.Errorf("PBR apply failed: %w", err))
	}
	if err := a.healthCheck(ctx, criticalServices, groups); err != nil {
		return rollback(fmt.Errorf("post-apply health failed: %w", err))
	}
	next := filepath.Join(a.opts.StateDir, "managed-paths.next.json")
	data, err := os.ReadFile(next)
	if err == nil {
		_ = writeFileAtomic(filepath.Join(a.opts.StateDir, "managed-paths.json"), data, 0o600)
		_ = os.Remove(next)
	}
	return false, nil
}

func (a *agent) pbrDataplaneReady(ctx context.Context, groups map[string]routeSet) bool {
	checkCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if a.runCommand(
		checkCtx,
		"ip -4 rule show 2>/dev/null | grep -Eq 'fwmark .* lookup pbr_' && "+
			"nft list chain inet fw4 pbr_prerouting 2>/dev/null | grep -q 'goto pbr_mark_'",
	) != nil {
		return false
	}
	interfaces := make([]string, 0, len(groups))
	for iface, routes := range groups {
		if iface == "wan" || len(routes.Domains)+len(routes.IPs) == 0 {
			continue
		}
		if !safeName(iface) {
			return false
		}
		interfaces = append(interfaces, iface)
	}
	sort.Strings(interfaces)
	for _, iface := range interfaces {
		setName := "pbr_" + iface + "_4_dst_ip_user"
		if a.runCommand(checkCtx, "nft list set inet fw4 "+shellQuote(setName)+" >/dev/null 2>&1") != nil {
			return false
		}
	}
	return true
}

func transportBootstrapRequired(transports []transportAction) bool {
	for _, transport := range transports {
		if transport.RequiresBootstrap {
			return true
		}
	}
	return false
}

func managedFileMode(path string) os.FileMode {
	switch {
	case strings.HasPrefix(path, "/etc/init.d/"):
		return 0o755
	case strings.HasPrefix(path, "/etc/sing-box/") && strings.HasSuffix(path, ".json"):
		return 0o600
	default:
		return 0o644
	}
}

func (a *agent) healthCheck(ctx context.Context, criticalServices []criticalService, groups map[string]routeSet) error {
	if err := a.controlHealthCheck(ctx); err != nil {
		return err
	}
	if len(criticalServices) == 0 {
		return errors.New("critical service health checks are not configured")
	}
	if failures := a.checkCriticalServices(ctx, criticalServices, groups); len(failures) > 0 {
		return fmt.Errorf("critical services failed: %s", strings.Join(failures, "; "))
	}
	return nil
}

func (a *agent) controlHealthCheck(ctx context.Context) error {
	healthCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(healthCtx, http.MethodGet, a.opts.HealthURL, nil)
	if err != nil {
		return err
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("health returned HTTP %d", resp.StatusCode)
	}
	return nil
}

func (a *agent) checkCriticalServices(ctx context.Context, services []criticalService, groups map[string]routeSet) []string {
	failures := []string{}
	for _, service := range services {
		label := firstNonEmpty(service.Label, service.ServiceKey, "unnamed")
		passed := false
		lastError := "no targets"
		for _, target := range service.Targets {
			iface := targetInterface(target, groups)
			probe := a.probeTarget(ctx, target, iface, 5*time.Second, 12*time.Second, service.SuccessPattern, service.FailurePattern)
			if shouldRetryNetworkProbe(probe) {
				select {
				case <-ctx.Done():
					probe["error"] = ctx.Err().Error()
				case <-time.After(500 * time.Millisecond):
					probe = a.probeTarget(ctx, target, iface, 5*time.Second, 12*time.Second, service.SuccessPattern, service.FailurePattern)
				}
			}
			if probe["ok"] == true {
				passed = true
				break
			}
			if probeError := anyString(probe["error"]); probeError != "" {
				lastError = fmt.Sprintf("%s%s: %s", target, viaInterface(iface), probeError)
			} else {
				lastError = fmt.Sprintf("%s%s: HTTP %v or content mismatch (%s)", target, viaInterface(iface), probe["http_code"], anyString(probe["semantic_status"]))
			}
		}
		if !passed {
			failures = append(failures, label+": "+lastError)
		}
	}
	return failures
}

func shouldRetryNetworkProbe(probe map[string]any) bool {
	if probe["ok"] == true {
		return false
	}
	message := strings.ToLower(anyString(probe["error"]))
	for _, marker := range []string{
		"timeout", "connection reset", "connection refused", "network is unreachable",
		"no route to host", "temporary", "unexpected eof", "tls handshake",
	} {
		if strings.Contains(message, marker) {
			return true
		}
	}
	return message == "eof"
}

func viaInterface(iface string) string {
	if iface == "" {
		return ""
	}
	return " via " + iface
}

func targetInterface(target string, groups map[string]routeSet) string {
	parsed, err := url.Parse(target)
	if err != nil {
		return ""
	}
	host := strings.ToLower(strings.TrimSuffix(parsed.Hostname(), "."))
	if host == "" {
		return ""
	}
	interfaces := make([]string, 0, len(groups))
	for iface := range groups {
		if iface != "wan" {
			interfaces = append(interfaces, iface)
		}
	}
	sort.Strings(interfaces)
	if ip := net.ParseIP(host); ip != nil {
		bestInterface := ""
		bestPrefix := -1
		for _, iface := range interfaces {
			for _, cidr := range groups[iface].IPs {
				_, network, parseErr := net.ParseCIDR(cidr)
				if parseErr != nil || !network.Contains(ip) {
					continue
				}
				prefix, _ := network.Mask.Size()
				if prefix > bestPrefix {
					bestInterface = iface
					bestPrefix = prefix
				}
			}
		}
		return bestInterface
	}
	for _, iface := range interfaces {
		for _, domain := range groups[iface].Domains {
			domain = strings.ToLower(strings.TrimSuffix(domain, "."))
			if host == domain || strings.HasSuffix(host, "."+domain) {
				return iface
			}
		}
	}
	return ""
}

func replaceManagedBlock(content string, lines []string) string {
	content = strings.ReplaceAll(content, "\r\n", "\n")
	start := strings.Index(content, beginMarker)
	if start >= 0 {
		endRel := strings.Index(content[start:], endMarker)
		if endRel >= 0 {
			end := start + endRel + len(endMarker)
			if end < len(content) && content[end] == '\n' {
				end++
			}
			content = content[:start] + content[end:]
		}
	}
	content = strings.TrimRight(content, "\n")
	if len(lines) == 0 {
		if content == "" {
			return ""
		}
		return content + "\n"
	}
	block := beginMarker + "\n" + strings.Join(lines, "\n") + "\n" + endMarker + "\n"
	if content == "" {
		return block
	}
	return content + "\n" + block
}

func renderManagedBlock(lines []string) string {
	lines = uniqueSorted(lines)
	if len(lines) == 0 {
		return ""
	}
	return beginMarker + "\n" + strings.Join(lines, "\n") + "\n" + endMarker + "\n"
}

func extractOverrideLines(content string) []string {
	result := []string{}
	for _, line := range strings.Split(strings.ReplaceAll(content, "\r\n", "\n"), "\n") {
		line = normalizeLine(line)
		if line != "" {
			result = append(result, line)
		}
	}
	return uniqueSorted(result)
}

func authoritativeOverridePaths(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	paths := []string{}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !strings.HasPrefix(name, "force-") {
			continue
		}
		var iface string
		switch {
		case strings.HasSuffix(name, ".domains"):
			iface = strings.TrimSuffix(strings.TrimPrefix(name, "force-"), ".domains")
		case strings.HasSuffix(name, ".ips"):
			iface = strings.TrimSuffix(strings.TrimPrefix(name, "force-"), ".ips")
		default:
			continue
		}
		if !safeName(iface) {
			continue
		}
		paths = append(paths, filepath.Join(dir, name))
	}
	sort.Strings(paths)
	return paths, nil
}

func extractManagedLines(content string) []string {
	start := strings.Index(content, beginMarker)
	if start < 0 {
		return nil
	}
	start += len(beginMarker)
	endRel := strings.Index(content[start:], endMarker)
	if endRel < 0 {
		return nil
	}
	result := []string{}
	for _, line := range strings.Split(content[start:start+endRel], "\n") {
		line = normalizeLine(line)
		if line != "" {
			result = append(result, line)
		}
	}
	return uniqueSorted(result)
}

func normalizeLine(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "\r", ""))
	if strings.ContainsAny(value, "\n\t") || strings.HasPrefix(value, "#") {
		return ""
	}
	return value
}

func safeName(value string) bool {
	if value == "" {
		return false
	}
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || strings.ContainsRune("_.-", r) {
			continue
		}
		return false
	}
	return true
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

func anyString(value any) string {
	if value == nil {
		return ""
	}
	if text, ok := value.(string); ok {
		return strings.TrimSpace(text)
	}
	return strings.TrimSpace(fmt.Sprint(value))
}

func anyInt(value any) (int, bool) {
	switch typed := value.(type) {
	case float64:
		return int(typed), typed == float64(int(typed))
	case int:
		return typed, true
	case json.Number:
		parsed, err := typed.Int64()
		return int(parsed), err == nil
	default:
		var parsed int
		_, err := fmt.Sscan(anyString(value), &parsed)
		return parsed, err == nil
	}
}

func uniqueSorted(values []string) []string {
	seen := map[string]bool{}
	result := []string{}
	for _, value := range values {
		value = normalizeLine(value)
		if value != "" && !seen[value] {
			seen[value] = true
			result = append(result, value)
		}
	}
	sort.Strings(result)
	return result
}

func subtract(left, right []string) []string {
	has := map[string]bool{}
	for _, item := range right {
		has[item] = true
	}
	result := []string{}
	for _, item := range left {
		if !has[item] {
			result = append(result, item)
		}
	}
	return result
}

func desiredRouteCount(desired desiredState) int {
	count := 0
	for _, set := range desired.Groups {
		count += len(set.Domains) + len(set.IPs)
	}
	return count
}

func readManagedPaths(path string) ([]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var result []string
	return result, json.Unmarshal(data, &result)
}

func writeJSONAtomic(path string, value any, mode os.FileMode) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return writeFileAtomic(path, data, mode)
}

func writeFileAtomic(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".cudy-router-agent-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(mode); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

func shellCommand(ctx context.Context, command string) error {
	output, err := exec.CommandContext(ctx, "/bin/sh", "-c", command).CombinedOutput()
	if err != nil {
		return fmt.Errorf("%w: %s", err, strings.TrimSpace(string(output)))
	}
	return nil
}
