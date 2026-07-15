package main

import (
	"bytes"
	"context"
	"crypto/sha256"
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
	Error                   string      `json:"error,omitempty"`
	Warnings                []string    `json:"warnings,omitempty"`
	Diff                    []diffEntry `json:"diff,omitempty"`
}

type options struct {
	Mode             string
	PreviewURL       string
	StateDir         string
	OverrideDir      string
	ApplyCommand     string
	BootstrapCommand string
	HealthURL        string
	PolicyCache      string
	SingBoxDir       string
	AllowApply       bool
	Once             bool
	PollInterval     time.Duration
}

type agent struct {
	opts       options
	httpClient *http.Client
	runCommand func(context.Context, string) error
	now        func() time.Time
}

func main() {
	var opts options
	flag.StringVar(&opts.Mode, "mode", "disabled", "disabled, observe, or apply")
	flag.StringVar(&opts.PreviewURL, "preview-url", "http://127.0.0.1:8765/api/cudy/agent-preview", "sanitized Cudy policy preview URL")
	flag.StringVar(&opts.StateDir, "state-dir", "/var/lib/cudy-router-agent", "root-only state and transaction directory")
	flag.StringVar(&opts.OverrideDir, "override-dir", "/etc/pbr-overrides", "PBR override directory")
	flag.StringVar(&opts.ApplyCommand, "apply-command", "/usr/bin/cudy-pbr-fast-apply", "command run after an override-only update")
	flag.StringVar(&opts.BootstrapCommand, "bootstrap-command", "/usr/bin/cudy-pbr-safe-restart", "command used when a new transport interface must be registered")
	flag.StringVar(&opts.HealthURL, "health-url", "http://127.0.0.1:8765/healthz", "post-apply health URL")
	flag.StringVar(&opts.PolicyCache, "policy-cache", "/var/lib/cudy-fallback/agent-config-cache.json", "root-only full policy cache used to prepare missing transports")
	flag.StringVar(&opts.SingBoxDir, "sing-box-dir", "/etc/sing-box", "managed sing-box config directory")
	flag.BoolVar(&opts.AllowApply, "allow-apply", false, "required safety gate for apply mode")
	flag.BoolVar(&opts.Once, "once", false, "run one cycle and exit")
	flag.DurationVar(&opts.PollInterval, "poll-interval", time.Minute, "policy poll interval")
	flag.Parse()

	a := &agent{
		opts:       opts,
		httpClient: &http.Client{Timeout: 15 * time.Second},
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

func (a *agent) validate() error {
	switch a.opts.Mode {
	case "disabled", "observe":
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
		return writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
	}
	if len(desired.Blockers) > 0 {
		err := errors.New("refusing apply while policy has blockers: " + strings.Join(desired.Blockers, "; "))
		status.Error = err.Error()
		_ = writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
		return err
	}
	if len(updates) == 0 {
		if err := a.healthCheck(ctx, desired.CriticalServices, desired.Groups); err != nil {
			status.Error = err.Error()
			_ = writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
			return err
		}
		status.CriticalServicesOK = true
		status.OK = true
		return writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
	}

	rolledBack, err := a.applyTransaction(ctx, updates, desired.Transports, desired.CriticalServices, desired.Groups)
	status.Applied = err == nil
	status.RolledBack = rolledBack
	status.OK = err == nil
	status.CriticalServicesOK = err == nil
	if err != nil {
		status.Error = err.Error()
	}
	_ = writeJSONAtomic(filepath.Join(a.opts.StateDir, "status.json"), status, 0o600)
	return err
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
		if !bytes.Equal(old, []byte(merged)) {
			updates[path] = []byte(merged)
			diffs = append(diffs, diffEntry{
				Path: path, Added: subtract(newManaged, oldManaged), Removed: subtract(oldManaged, newManaged),
			})
		}
	}
	currentPaths := make([]string, 0, len(desired.Groups)*2)
	for iface := range desired.Groups {
		currentPaths = append(currentPaths,
			filepath.Join(a.opts.OverrideDir, "force-"+iface+".domains"),
			filepath.Join(a.opts.OverrideDir, "force-"+iface+".ips"))
	}
	sort.Strings(currentPaths)
	if err := writeJSONAtomic(filepath.Join(a.opts.StateDir, "managed-paths.next.json"), currentPaths, 0o600); err != nil {
		return nil, nil, err
	}
	return diffs, updates, nil
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
	bootstrapRequired := transportBootstrapRequired(transports)
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
		rollbackCtx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
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
	if len(criticalServices) == 0 {
		return errors.New("critical service health checks are not configured")
	}
	if failures := a.checkCriticalServices(ctx, criticalServices, groups); len(failures) > 0 {
		return fmt.Errorf("critical services failed: %s", strings.Join(failures, "; "))
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
			requestCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
			parsed, parseErr := url.Parse(target)
			iface := targetInterface(target, groups)
			dialer := &net.Dialer{}
			if bindErr := bindDialerToInterface(dialer, iface); bindErr != nil {
				lastError = fmt.Sprintf("%s via %s: %v", target, iface, bindErr)
				cancel()
				continue
			}
			if parseErr == nil && parsed.Scheme == "tcp" {
				if service.SuccessPattern != "" || service.FailurePattern != "" {
					lastError = target + ": content patterns are not supported for TCP targets"
					cancel()
					continue
				}
				connection, dialErr := dialer.DialContext(requestCtx, "tcp", parsed.Host)
				if dialErr == nil {
					_ = connection.Close()
					passed = true
					cancel()
					break
				}
				lastError = fmt.Sprintf("%s%s: %v", target, viaInterface(iface), dialErr)
				cancel()
				continue
			}
			req, err := http.NewRequestWithContext(requestCtx, http.MethodGet, target, nil)
			if err == nil {
				req.Header.Set("Range", "bytes=0-262143")
				var resp *http.Response
				client := a.httpClient
				var transport *http.Transport
				if iface != "" {
					transport = http.DefaultTransport.(*http.Transport).Clone()
					transport.DialContext = dialer.DialContext
					client = &http.Client{Transport: transport}
				}
				resp, err = client.Do(req)
				if transport != nil {
					transport.CloseIdleConnections()
				}
				if err == nil {
					body, readErr := io.ReadAll(io.LimitReader(resp.Body, 262144))
					resp.Body.Close()
					if readErr != nil {
						err = readErr
					} else {
						text := string(body)
						success := service.SuccessPattern == "" || regexp.MustCompile("(?im)"+service.SuccessPattern).MatchString(text)
						failure := service.FailurePattern != "" && regexp.MustCompile("(?im)"+service.FailurePattern).MatchString(text)
						if resp.StatusCode > 0 && success && !failure {
							passed = true
							cancel()
							break
						}
						lastError = fmt.Sprintf("%s returned HTTP %d or content mismatch", target, resp.StatusCode)
					}
				}
			}
			if err != nil {
				lastError = fmt.Sprintf("%s%s: %v", target, viaInterface(iface), err)
			}
			cancel()
		}
		if !passed {
			failures = append(failures, label+": "+lastError)
		}
	}
	return failures
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
