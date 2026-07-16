package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

type server struct {
	publicDir        string
	agentConfigPath  string
	agentCachePath   string
	maxAgentCacheAge time.Duration
	maxStateAge      time.Duration
	now              func() time.Time
	run              commandRunner
	httpClient       *http.Client
	fetchAgentConfig func(context.Context, agentSettings) (map[string]any, error)
	observerMu       sync.RWMutex
	observer         agentObserverStatus
}

type commandRunner func(ctx context.Context, name string, args ...string) (string, error)

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

type runtimeResponse struct {
	OK                  bool                `json:"ok"`
	GeneratedAt         string              `json:"generated_at"`
	Architecture        string              `json:"architecture,omitempty"`
	OpenWrtTarget       string              `json:"openwrt_target,omitempty"`
	SupportedInterfaces []string            `json:"supported_interfaces"`
	TargetInterface     string              `json:"target_interface,omitempty"`
	Links               []string            `json:"links"`
	IPv4                map[string][]string `json:"ipv4"`
	Services            map[string]string   `json:"services"`
	CronEntries         []string            `json:"cron_entries"`
	Listeners           []string            `json:"listeners"`
	Warnings            []string            `json:"warnings"`
}

type agentSettings struct {
	ControlURL       string                   `json:"control_url"`
	AgentConfigPath  string                   `json:"agent_config_path,omitempty"`
	DeviceID         string                   `json:"device_id,omitempty"`
	Token            string                   `json:"token,omitempty"`
	TokenFile        string                   `json:"token_file,omitempty"`
	CachePath        string                   `json:"cache_path,omitempty"`
	InterfaceMap     map[string]string        `json:"interface_map,omitempty"`
	CriticalServices []criticalServicePreview `json:"critical_services,omitempty"`
}

type agentPreviewResponse struct {
	OK               bool                     `json:"ok"`
	Configured       bool                     `json:"configured"`
	GeneratedAt      string                   `json:"generated_at"`
	ControlURL       string                   `json:"control_url,omitempty"`
	DeviceID         string                   `json:"device_id,omitempty"`
	UserID           string                   `json:"user_id,omitempty"`
	TransportPlan    []transportPreview       `json:"transport_plan,omitempty"`
	Routes           []routePreview           `json:"routes,omitempty"`
	CriticalServices []criticalServicePreview `json:"critical_services,omitempty"`
	Warnings         []string                 `json:"warnings,omitempty"`
	Error            string                   `json:"error,omitempty"`
	Source           string                   `json:"source,omitempty"`
	CacheAge         int64                    `json:"cache_age_seconds,omitempty"`
	ControlError     string                   `json:"control_error,omitempty"`
}

type criticalServicePreview struct {
	ServiceKey     string   `json:"service_key"`
	Label          string   `json:"label"`
	Targets        []string `json:"targets"`
	SuccessPattern string   `json:"success_pattern,omitempty"`
	FailurePattern string   `json:"failure_pattern,omitempty"`
}

type agentCache struct {
	CachedAt string         `json:"cached_at"`
	Config   map[string]any `json:"config"`
}

type agentObserverStatus struct {
	Enabled        bool   `json:"enabled"`
	LastAttemptAt  string `json:"last_attempt_at,omitempty"`
	LastSuccessAt  string `json:"last_success_at,omitempty"`
	LastError      string `json:"last_error,omitempty"`
	CachePath      string `json:"cache_path,omitempty"`
	CacheUpdatedAt string `json:"cache_updated_at,omitempty"`
}

type transportPreview struct {
	ServerID           string `json:"server_id"`
	Interface          string `json:"interface"`
	TransportType      string `json:"transport_type,omitempty"`
	InterfacePresent   bool   `json:"interface_present"`
	InterfaceSupported bool   `json:"interface_supported"`
	Applicable         bool   `json:"applicable"`
}

type routePreview struct {
	Kind               string `json:"kind"`
	Target             string `json:"target"`
	Source             string `json:"source,omitempty"`
	RequestedServerID  string `json:"requested_server_id,omitempty"`
	ServerID           string `json:"server_id"`
	ResolvedServerID   string `json:"resolved_server_id,omitempty"`
	Interface          string `json:"interface,omitempty"`
	InterfacePresent   bool   `json:"interface_present"`
	InterfaceSupported bool   `json:"interface_supported"`
	Applicable         bool   `json:"applicable"`
	Warning            string `json:"warning,omitempty"`
}

var cudyServices = []string{
	"cudy-fallback",
	"cudy-control-tunnel",
	"cudy-router-agent",
	"pbr",
	"sing-box",
	"sing-box-vpntype",
	"sing-box-lokvpn",
	"sing-box-proxygb",
	"sing-box-proxyca",
	"sing-box-proxyfr",
	"sing-box-proxyby",
	"sing-box-proxyae",
	"sing-box-proxyhk",
	"sing-box-proxykz",
	"sing-box-proxytr",
	"sing-box-proxyil",
	"sing-box-proxycz",
	"sing-box-proxypl",
	"sing-box-proxyfi",
	"sing-box-proxynl",
	"sing-box-proxyal",
	"sing-box-proxyru",
	"sing-box-proxyus",
	"sing-box-proxyde",
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8765", "HTTP listen address")
	publicDir := flag.String("public-dir", "/www/cudy-control", "directory containing endpoints.json and state.json")
	agentConfigPath := flag.String("agent-config", "/etc/cudy-fallback/agent.json", "optional agent settings for read-only policy preview")
	agentCachePath := flag.String("agent-cache", "/var/lib/cudy-fallback/agent-config-cache.json", "root-only cache for the last valid agent policy")
	agentPollInterval := flag.Duration("agent-poll-interval", time.Minute, "control policy refresh interval; zero disables background refresh")
	maxAgentCacheAge := flag.Duration("max-agent-cache-age", 24*time.Hour, "maximum accepted offline agent policy cache age")
	maxStateAge := flag.Duration("max-state-age", 3*time.Hour, "maximum accepted age for state.json")
	flag.Parse()

	srv := &server{
		publicDir:        *publicDir,
		agentConfigPath:  *agentConfigPath,
		agentCachePath:   *agentCachePath,
		maxAgentCacheAge: *maxAgentCacheAge,
		maxStateAge:      *maxStateAge,
		now:              func() time.Time { return time.Now().UTC() },
		run:              runCommand,
		httpClient:       &http.Client{Timeout: 12 * time.Second},
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", srv.handleHealth)
	mux.HandleFunc("/readyz", srv.handleReady)
	mux.HandleFunc("/api/control/endpoints", srv.handleEndpoints)
	mux.HandleFunc("/api/cudy/runtime", srv.handleRuntime)
	mux.HandleFunc("/api/cudy/agent-preview", srv.handleAgentPreview)
	mux.HandleFunc("/api/cudy/agent-observer", srv.handleAgentObserver)
	mux.HandleFunc("/cudy-control/endpoints.json", srv.handleEndpoints)
	mux.HandleFunc("/cudy-control/state.json", srv.handleState)

	if *agentPollInterval > 0 {
		go srv.runAgentObserver(context.Background(), *agentPollInterval)
	}
	log.Printf("cudy fallback service listening on %s public_dir=%s agent_poll=%s", *listen, *publicDir, *agentPollInterval)
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

func (s *server) handleRuntime(w http.ResponseWriter, _ *http.Request) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	writeJSON(w, http.StatusOK, s.runtime(ctx))
}

func (s *server) handleAgentPreview(w http.ResponseWriter, _ *http.Request) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	writeJSON(w, http.StatusOK, s.agentPreview(ctx))
}

func (s *server) handleAgentObserver(w http.ResponseWriter, _ *http.Request) {
	s.observerMu.RLock()
	status := s.observer
	s.observerMu.RUnlock()
	writeJSON(w, http.StatusOK, status)
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

func (s *server) runtime(ctx context.Context) runtimeResponse {
	now := s.now().UTC()
	warnings := []string{}
	run := s.run
	if run == nil {
		run = runCommand
	}

	runShell := func(label string, script string) string {
		out, err := run(ctx, "/bin/sh", "-c", script)
		if err != nil {
			warnings = append(warnings, fmt.Sprintf("%s: %v", label, err))
			return ""
		}
		return strings.TrimSpace(out)
	}

	architecture := runShell("architecture", "uname -m 2>/dev/null || true")
	openwrtTarget := parseOpenWrtTarget(runShell("openwrt_target", "grep '^DISTRIB_TARGET=' /etc/openwrt_release 2>/dev/null | cut -d= -f2- | tr -d \"'\\\"\" || true"))
	supported := strings.Fields(runShell("supported_interfaces", "uci -q get pbr.config.supported_interface 2>/dev/null || true"))
	targetInterface := runShell("target_interface", "sed -n \"s/^TARGET_INTERFACE='\\([^']*\\)'.*/\\1/p\" /usr/share/pbr/pbr.user.opencck-merged-vpn 2>/dev/null | tail -1 || true")
	links := parseLinks(runShell("links", "ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' || true"))
	ipv4 := parseIPv4(runShell("ipv4", "ip -4 -o addr show 2>/dev/null | awk '{print $2 \"\\t\" $4}' || true"))
	cronEntries := parseActiveLines(runShell("cron", "cat /etc/crontabs/root 2>/dev/null || true"))
	listeners := parseActiveLines(runShell("listeners", "ss -ltnp 2>/dev/null || true"))
	services := s.serviceStatus(ctx, run, &warnings)

	return runtimeResponse{
		OK:                  len(warnings) == 0,
		GeneratedAt:         now.Format(time.RFC3339),
		Architecture:        architecture,
		OpenWrtTarget:       openwrtTarget,
		SupportedInterfaces: supported,
		TargetInterface:     targetInterface,
		Links:               links,
		IPv4:                ipv4,
		Services:            services,
		CronEntries:         cronEntries,
		Listeners:           listeners,
		Warnings:            warnings,
	}
}

func (s *server) serviceStatus(ctx context.Context, run commandRunner, warnings *[]string) map[string]string {
	services := make(map[string]string, len(cudyServices))
	for _, name := range cudyServices {
		script := fmt.Sprintf("if [ -x /etc/init.d/%[1]s ]; then /etc/init.d/%[1]s status 2>/dev/null | head -1 || true; else echo missing; fi", name)
		out, err := run(ctx, "/bin/sh", "-c", script)
		if err != nil {
			*warnings = append(*warnings, fmt.Sprintf("service %s: %v", name, err))
			services[name] = "error"
			continue
		}
		status := strings.TrimSpace(out)
		if status == "" {
			status = "unknown"
		}
		services[name] = status
	}
	return services
}

func (s *server) path(name string) string {
	return filepath.Join(s.publicDir, name)
}

func (s *server) agentPreview(ctx context.Context) agentPreviewResponse {
	now := s.now().UTC()
	settings, err := readAgentSettings(s.agentConfigPath)
	if err != nil {
		return agentPreviewResponse{
			OK:          false,
			Configured:  false,
			GeneratedAt: now.Format(time.RFC3339),
			Error:       err.Error(),
		}
	}

	config, source, cacheAge, controlError, err := s.loadAgentConfig(ctx, settings)
	if err != nil {
		return agentPreviewResponse{
			OK:           false,
			Configured:   true,
			GeneratedAt:  now.Format(time.RFC3339),
			ControlURL:   settings.ControlURL,
			DeviceID:     settings.DeviceID,
			Error:        err.Error(),
			ControlError: controlError,
		}
	}

	runtime := s.runtime(ctx)
	supported := stringSet(runtime.SupportedInterfaces)
	present := stringSet(runtime.Links)
	transports := previewTransports(config, supported, present, settings.InterfaceMap)
	routes := previewRoutes(config, transports, supported, present)
	criticalServices := mergeCriticalServices(previewCriticalServices(config), settings.CriticalServices)
	warnings := append([]string{}, runtime.Warnings...)
	if controlError != "" {
		warnings = append(warnings, "live control unavailable; using cached policy: "+controlError)
	}
	for _, route := range routes {
		if route.Warning != "" {
			warnings = append(warnings, fmt.Sprintf("%s %s: %s", route.Kind, route.Target, route.Warning))
		}
	}

	return agentPreviewResponse{
		OK:               len(runtime.Warnings) == 0,
		Configured:       true,
		GeneratedAt:      now.Format(time.RFC3339),
		ControlURL:       settings.ControlURL,
		DeviceID:         firstNonEmpty(settings.DeviceID, stringFromMap(config, "device.id"), stringFromMap(config, "device_id")),
		UserID:           stringFromMap(config, "user.id"),
		TransportPlan:    transports,
		Routes:           routes,
		CriticalServices: criticalServices,
		Warnings:         warnings,
		Source:           source,
		CacheAge:         int64(cacheAge.Seconds()),
		ControlError:     controlError,
	}
}

func previewCriticalServices(config map[string]any) []criticalServicePreview {
	result := []criticalServicePreview{}
	for _, item := range asMapSlice(config["critical_services"]) {
		targets := []string{}
		if rawTargets, ok := item["targets"].([]any); ok {
			for _, raw := range rawTargets {
				value := stringValue(raw)
				if validCriticalTarget(value) {
					targets = append(targets, value)
				}
			}
		}
		if len(targets) == 0 {
			continue
		}
		result = append(result, criticalServicePreview{
			ServiceKey:     stringValue(item["service_key"]),
			Label:          firstNonEmpty(stringValue(item["label"]), stringValue(item["service_key"])),
			Targets:        targets,
			SuccessPattern: stringValue(item["success_pattern"]),
			FailurePattern: stringValue(item["failure_pattern"]),
		})
	}
	return result
}

func mergeCriticalServices(control, local []criticalServicePreview) []criticalServicePreview {
	result := []criticalServicePreview{}
	positions := map[string]int{}
	for _, service := range append(append([]criticalServicePreview{}, control...), local...) {
		service.ServiceKey = strings.TrimSpace(service.ServiceKey)
		service.Label = firstNonEmpty(service.Label, service.ServiceKey)
		targets := []string{}
		seenTargets := map[string]bool{}
		for _, target := range service.Targets {
			target = strings.TrimSpace(target)
			if validCriticalTarget(target) && !seenTargets[target] {
				seenTargets[target] = true
				targets = append(targets, target)
			}
		}
		service.Targets = targets
		if service.ServiceKey == "" || len(service.Targets) == 0 {
			continue
		}
		if index, exists := positions[service.ServiceKey]; exists {
			result[index] = service
		} else {
			positions[service.ServiceKey] = len(result)
			result = append(result, service)
		}
	}
	return result
}

func validCriticalTarget(value string) bool {
	parsed, err := url.Parse(strings.TrimSpace(value))
	if err != nil || parsed.User != nil || parsed.Host == "" {
		return false
	}
	switch parsed.Scheme {
	case "http", "https":
		return true
	case "tcp":
		if parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" {
			return false
		}
		host, port, err := net.SplitHostPort(parsed.Host)
		return err == nil && host != "" && port != ""
	default:
		return false
	}
}

func (s *server) runAgentObserver(ctx context.Context, interval time.Duration) {
	if interval <= 0 {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		s.refreshAgentCache(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (s *server) refreshAgentCache(parent context.Context) {
	s.observerMu.RLock()
	previous := s.observer
	s.observerMu.RUnlock()
	status := agentObserverStatus{
		Enabled:        true,
		LastAttemptAt:  s.now().UTC().Format(time.RFC3339),
		LastSuccessAt:  previous.LastSuccessAt,
		LastError:      "",
		CachePath:      s.agentCachePath,
		CacheUpdatedAt: previous.CacheUpdatedAt,
	}
	settings, err := readAgentSettings(s.agentConfigPath)
	if err == nil {
		ctx, cancel := context.WithTimeout(parent, 20*time.Second)
		defer cancel()
		fetch := s.fetchAgentConfig
		if fetch == nil {
			fetch = s.fetchAgentConfigHTTP
		}
		var config map[string]any
		for attempt := 0; attempt < 2; attempt++ {
			attemptCtx, attemptCancel := context.WithTimeout(ctx, 9*time.Second)
			config, err = fetch(attemptCtx, settings)
			attemptCancel()
			if err == nil || attempt == 1 {
				break
			}
			select {
			case <-ctx.Done():
				err = ctx.Err()
				break
			case <-time.After(500 * time.Millisecond):
			}
		}
		if err == nil {
			cachePath := s.cachePath(settings)
			status.CachePath = cachePath
			if cachePath != "" {
				err = writeAgentCache(cachePath, agentCache{CachedAt: s.now().UTC().Format(time.RFC3339), Config: config})
			}
		}
	}
	if err != nil {
		status.LastError = err.Error()
	} else {
		status.LastSuccessAt = s.now().UTC().Format(time.RFC3339)
		status.CacheUpdatedAt = status.LastSuccessAt
	}
	s.observerMu.Lock()
	s.observer = status
	s.observerMu.Unlock()
}

func (s *server) loadAgentConfig(ctx context.Context, settings agentSettings) (map[string]any, string, time.Duration, string, error) {
	fetch := s.fetchAgentConfig
	if fetch == nil {
		fetch = s.fetchAgentConfigHTTP
	}
	config, controlErr := fetch(ctx, settings)
	if controlErr == nil {
		cachePath := s.cachePath(settings)
		if cachePath != "" {
			if err := writeAgentCache(cachePath, agentCache{CachedAt: s.now().UTC().Format(time.RFC3339), Config: config}); err != nil {
				log.Printf("agent cache write failed: %v", err)
			}
		}
		return config, "live", 0, "", nil
	}

	cachePath := s.cachePath(settings)
	cache, age, cacheErr := readAgentCache(cachePath, s.now().UTC(), s.maxAgentCacheAge)
	if cacheErr != nil {
		return nil, "", 0, controlErr.Error(), fmt.Errorf("control fetch failed: %v; cache unavailable: %w", controlErr, cacheErr)
	}
	return cache.Config, "cache", age, controlErr.Error(), nil
}

func (s *server) cachePath(settings agentSettings) string {
	if settings.CachePath != "" {
		if filepath.IsAbs(settings.CachePath) {
			return settings.CachePath
		}
		return filepath.Join(filepath.Dir(s.agentConfigPath), settings.CachePath)
	}
	return s.agentCachePath
}

func writeAgentCache(path string, cache agentCache) error {
	if path == "" {
		return nil
	}
	raw, err := json.Marshal(cache)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o600); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return os.Chmod(path, 0o600)
}

func readAgentCache(path string, now time.Time, maxAge time.Duration) (agentCache, time.Duration, error) {
	if path == "" {
		return agentCache{}, 0, errors.New("agent cache path is empty")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return agentCache{}, 0, err
	}
	var cache agentCache
	if err := json.Unmarshal(raw, &cache); err != nil {
		return agentCache{}, 0, err
	}
	if cache.Config == nil || cache.CachedAt == "" {
		return agentCache{}, 0, errors.New("agent cache is incomplete")
	}
	cachedAt, err := parseTime(cache.CachedAt)
	if err != nil {
		return agentCache{}, 0, fmt.Errorf("agent cache time: %w", err)
	}
	age := now.Sub(cachedAt)
	if age < 0 {
		return agentCache{}, age, errors.New("agent cache timestamp is in the future")
	}
	if maxAge > 0 && age > maxAge {
		return agentCache{}, age, fmt.Errorf("agent cache is stale: age=%s max=%s", age.Round(time.Second), maxAge)
	}
	return cache, age, nil
}

func readAgentSettings(path string) (agentSettings, error) {
	if path == "" {
		return agentSettings{}, errors.New("agent config path is empty")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return agentSettings{}, err
	}
	var settings agentSettings
	if err := json.Unmarshal(raw, &settings); err != nil {
		return agentSettings{}, err
	}
	settings.ControlURL = strings.TrimSpace(settings.ControlURL)
	settings.AgentConfigPath = strings.TrimSpace(settings.AgentConfigPath)
	settings.DeviceID = strings.TrimSpace(settings.DeviceID)
	settings.Token = strings.TrimSpace(settings.Token)
	settings.TokenFile = strings.TrimSpace(settings.TokenFile)
	settings.CachePath = strings.TrimSpace(settings.CachePath)
	if settings.ControlURL == "" {
		return agentSettings{}, errors.New("control_url is missing in agent config")
	}
	if settings.AgentConfigPath == "" {
		settings.AgentConfigPath = "/api/agent/config"
	}
	if settings.Token == "" && settings.TokenFile != "" {
		tokenPath := settings.TokenFile
		if !filepath.IsAbs(tokenPath) {
			tokenPath = filepath.Join(filepath.Dir(path), tokenPath)
		}
		tokenRaw, err := os.ReadFile(tokenPath)
		if err != nil {
			return agentSettings{}, fmt.Errorf("read token_file: %w", err)
		}
		settings.Token = strings.TrimSpace(string(tokenRaw))
	}
	if settings.Token == "" {
		return agentSettings{}, errors.New("token or token_file is missing in agent config")
	}
	return settings, nil
}

func (s *server) fetchAgentConfigHTTP(ctx context.Context, settings agentSettings) (map[string]any, error) {
	client := s.httpClient
	if client == nil {
		client = &http.Client{Timeout: 12 * time.Second}
	}
	endpoint := strings.TrimRight(settings.ControlURL, "/") + "/" + strings.TrimLeft(settings.AgentConfigPath, "/")
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "cudy-go-fallback/0.1")
	req.Header.Set("Authorization", "Bearer "+settings.Token)
	res, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("agent config fetch failed: status=%d body=%s", res.StatusCode, strings.TrimSpace(string(raw)))
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	if payload == nil {
		return nil, errors.New("agent config response is empty")
	}
	return payload, nil
}

func previewTransports(config map[string]any, supported map[string]bool, present map[string]bool, interfaceMap map[string]string) []transportPreview {
	rows := []transportPreview{}
	for _, item := range asMapSlice(config["transport_plan"]) {
		serverID := stringValue(item["server_id"])
		iface := stringValue(item["interface_name"])
		if mapped := strings.TrimSpace(interfaceMap[serverID]); mapped != "" {
			iface = mapped
		}
		if iface == "" {
			iface = serverID
		}
		if serverID == "" && iface == "" {
			continue
		}
		presentOK := iface != "" && present[iface]
		supportedOK := iface != "" && supported[iface]
		rows = append(rows, transportPreview{
			ServerID:           serverID,
			Interface:          iface,
			TransportType:      stringValue(item["transport_type"]),
			InterfacePresent:   presentOK,
			InterfaceSupported: supportedOK,
			Applicable:         iface != "" && presentOK && supportedOK,
		})
	}
	return rows
}

func previewRoutes(config map[string]any, transports []transportPreview, supported map[string]bool, present map[string]bool) []routePreview {
	transportByServer := map[string]transportPreview{}
	for _, item := range transports {
		if item.ServerID != "" {
			transportByServer[item.ServerID] = item
		}
	}

	rows := []routePreview{}
	for _, item := range asMapSlice(config["domain_routes"]) {
		rows = append(rows, buildRoutePreview("domain", stringValue(item["domain"]), item, transportByServer, supported, present))
	}
	for _, item := range asMapSlice(config["ip_routes"]) {
		rows = append(rows, buildRoutePreview("ip", stringValue(item["target_cidr"]), item, transportByServer, supported, present))
	}
	for _, item := range asMapSlice(config["cleanup_ip_routes"]) {
		row := buildRoutePreview("cleanup_ip", stringValue(item["target_cidr"]), item, transportByServer, supported, present)
		row.Applicable = row.Target != ""
		row.Warning = ""
		rows = append(rows, row)
	}
	return rows
}

func buildRoutePreview(kind string, target string, item map[string]any, transportByServer map[string]transportPreview, supported map[string]bool, present map[string]bool) routePreview {
	serverID := stringValue(item["server_id"])
	requestedServerID := stringValue(item["requested_server_id"])
	resolvedServerID := stringValue(item["resolved_server_id"])
	source := stringValue(item["source"])
	if serverID == "" && kind == "cleanup_ip" {
		serverID = "cleanup"
	}

	if serverID == "direct" || serverID == "" {
		return routePreview{
			Kind:              kind,
			Target:            target,
			Source:            source,
			RequestedServerID: requestedServerID,
			ServerID:          firstNonEmpty(serverID, "direct"),
			ResolvedServerID:  resolvedServerID,
			Applicable:        target != "",
		}
	}

	if transport, ok := transportByServer[serverID]; ok {
		return routePreview{
			Kind:               kind,
			Target:             target,
			Source:             source,
			RequestedServerID:  requestedServerID,
			ServerID:           serverID,
			ResolvedServerID:   resolvedServerID,
			Interface:          transport.Interface,
			InterfacePresent:   transport.InterfacePresent,
			InterfaceSupported: transport.InterfaceSupported,
			Applicable:         target != "" && transport.Applicable,
			Warning:            routeWarning(target, transport.Interface, transport.InterfacePresent, transport.InterfaceSupported),
		}
	}

	iface := stringFromMap(item, "server.interface")
	if iface == "" && (supported[serverID] || present[serverID]) {
		iface = serverID
	}
	presentOK := iface != "" && present[iface]
	supportedOK := iface != "" && supported[iface]
	return routePreview{
		Kind:               kind,
		Target:             target,
		Source:             source,
		RequestedServerID:  requestedServerID,
		ServerID:           serverID,
		ResolvedServerID:   resolvedServerID,
		Interface:          iface,
		InterfacePresent:   presentOK,
		InterfaceSupported: supportedOK,
		Applicable:         target != "" && iface != "" && presentOK && supportedOK,
		Warning:            routeWarning(target, iface, presentOK, supportedOK),
	}
}

func routeWarning(target string, iface string, present bool, supported bool) string {
	if target == "" {
		return "target is empty"
	}
	if iface == "" {
		return "server has no interface mapping"
	}
	if !present {
		return "interface is not present on Cudy"
	}
	if !supported {
		return "interface is not in pbr supported_interface"
	}
	return ""
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

func runCommand(ctx context.Context, name string, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	raw, err := cmd.CombinedOutput()
	if ctx.Err() != nil {
		return string(raw), ctx.Err()
	}
	if err != nil {
		return string(raw), err
	}
	return string(raw), nil
}

func parseOpenWrtTarget(value string) string {
	return strings.Trim(strings.TrimSpace(value), "'\"")
}

func parseLinks(raw string) []string {
	return parseActiveLines(raw)
}

func parseIPv4(raw string) map[string][]string {
	result := map[string][]string{}
	for _, line := range strings.Split(raw, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		result[fields[0]] = append(result[fields[0]], fields[1])
	}
	return result
}

func parseActiveLines(raw string) []string {
	lines := []string{}
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		lines = append(lines, line)
	}
	return lines
}

func stringSet(values []string) map[string]bool {
	result := map[string]bool{}
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			result[value] = true
		}
	}
	return result
}

func asMapSlice(value any) []map[string]any {
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	result := []map[string]any{}
	for _, item := range items {
		row, ok := item.(map[string]any)
		if ok {
			result = append(result, row)
		}
	}
	return result
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case string:
		return strings.TrimSpace(typed)
	case fmt.Stringer:
		return strings.TrimSpace(typed.String())
	default:
		return ""
	}
}

func stringFromMap(payload map[string]any, path string) string {
	var current any = payload
	for _, part := range strings.Split(path, ".") {
		row, ok := current.(map[string]any)
		if !ok {
			return ""
		}
		current = row[part]
	}
	return stringValue(current)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" {
			return value
		}
	}
	return ""
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(payload)
}

func writeError(w http.ResponseWriter, code int, err error) {
	writeJSON(w, code, map[string]any{"ok": false, "error": err.Error()})
}
