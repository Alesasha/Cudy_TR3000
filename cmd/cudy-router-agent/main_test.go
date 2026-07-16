package main

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestBuildDesiredMapsDirectAndRejectsUnavailable(t *testing.T) {
	preview := previewResponse{OK: true, Configured: true, Source: "live", Routes: []routePreview{
		{Kind: "domain", Target: "example.com", ServerID: "proxyde", Interface: "proxyde", Applicable: true},
		{Kind: "ip", Target: "203.0.113.0/24", ServerID: "direct", Applicable: true},
	}}
	desired, err := buildDesired(preview, map[string]bool{}, time.Unix(0, 0))
	if err != nil {
		t.Fatal(err)
	}
	if got := desired.Groups["proxyde"].Domains; len(got) != 1 || got[0] != "example.com" {
		t.Fatalf("domains=%v", got)
	}
	if got := desired.Groups["wan"].IPs; len(got) != 1 || got[0] != "203.0.113.0/24" {
		t.Fatalf("ips=%v", got)
	}
	if desired.ServerIDs["proxyde"] != "proxyde" || desired.ServerIDs["wan"] != "direct" {
		t.Fatalf("server ids=%v", desired.ServerIDs)
	}

	preview.Routes = append(preview.Routes, routePreview{Kind: "ip", Target: "198.51.100.1/32", ServerID: "missing", Interface: "missing", Applicable: false})
	desired, err = buildDesired(preview, map[string]bool{}, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if len(desired.Blockers) != 1 {
		t.Fatalf("blockers=%v", desired.Blockers)
	}
}

func TestPreparableTransportMakesRouteEligible(t *testing.T) {
	preview := previewResponse{OK: true, Configured: true, Source: "live", Routes: []routePreview{
		{Kind: "domain", Target: "openai.com", ServerID: "lokvpn-de1", Interface: "lokvpn-de1", Applicable: false, Warning: "missing"},
	}}
	desired, err := buildDesired(preview, map[string]bool{"lokvpn-de1": true}, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if len(desired.Blockers) != 0 || len(desired.Groups["lokvpn-de1"].Domains) != 1 {
		t.Fatalf("desired=%+v", desired)
	}
}

func TestUnusedMissingTransportIsNotPrepared(t *testing.T) {
	a := &agent{opts: options{PolicyCache: filepath.Join(t.TempDir(), "missing.json")}}
	preview := previewResponse{
		TransportPlan: []transportPreview{{ServerID: "unused", Interface: "unused", Applicable: false}},
		Routes:        []routePreview{{Kind: "domain", Target: "example.com", ServerID: "proxyde", Interface: "proxyde", Applicable: true}},
	}
	actions, updates, preparable, err := a.planTransports(preview)
	if err != nil || len(actions) != 0 || len(updates) != 0 || len(preparable) != 0 {
		t.Fatalf("unused transport was prepared: actions=%v updates=%v preparable=%v err=%v", actions, updates, preparable, err)
	}
}

func TestApplicableTransportRefreshesChangedConfigWithoutBootstrap(t *testing.T) {
	root := t.TempDir()
	policyPath := filepath.Join(root, "cache.json")
	singBoxDir := filepath.Join(root, "sing-box")
	if err := os.MkdirAll(singBoxDir, 0o700); err != nil {
		t.Fatal(err)
	}
	cache := cachedPolicy{CachedAt: time.Now().UTC().Format(time.RFC3339)}
	cache.Config.TransportPlan = []rawTransport{{
		ServerID: "proxyde", InterfaceName: "proxyde", TransportType: "http-proxy-tun",
		Config: map[string]any{"server": "203.0.113.10", "server_port": float64(8080), "proxy_type": "http"},
	}}
	raw, err := json.Marshal(cache)
	if err != nil {
		t.Fatal(err)
	}
	var decoded cachedPolicy
	if err := json.Unmarshal(raw, &decoded); err != nil || len(decoded.Config.TransportPlan) != 1 {
		t.Fatalf("cache round trip failed: %s err=%v decoded=%+v", raw, err, decoded)
	}
	if err := os.WriteFile(policyPath, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(singBoxDir, "proxyde.json"), []byte("stale\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := &agent{opts: options{PolicyCache: policyPath, SingBoxDir: singBoxDir}, now: time.Now}
	preview := previewResponse{
		TransportPlan: []transportPreview{{ServerID: "proxyde", Interface: "proxyde", Applicable: true}},
		Routes:        []routePreview{{Kind: "domain", Target: "example.com", ServerID: "proxyde", Interface: "proxyde", Applicable: true}},
	}
	actions, updates, preparable, err := a.planTransports(preview)
	if err != nil {
		t.Fatal(err)
	}
	if len(actions) != 1 || actions[0].Action != "refresh-and-restart" || actions[0].RequiresBootstrap {
		t.Fatalf("actions=%+v", actions)
	}
	if len(updates) != 2 || len(preparable) != 0 {
		t.Fatalf("updates=%v preparable=%v", updates, preparable)
	}
}

func TestExistingUnsupportedTransportIsAcceptedWithoutManagement(t *testing.T) {
	root := t.TempDir()
	policyPath := filepath.Join(root, "cache.json")
	cache := cachedPolicy{CachedAt: time.Now().UTC().Format(time.RFC3339)}
	cache.Config.TransportPlan = []rawTransport{{
		ServerID: "aktau", InterfaceName: "awg1", TransportType: "amneziawg-conf",
	}}
	raw, err := json.Marshal(cache)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(policyPath, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	a := &agent{opts: options{PolicyCache: policyPath, SingBoxDir: filepath.Join(root, "sing-box")}, now: time.Now}
	preview := previewResponse{
		TransportPlan: []transportPreview{{
			ServerID: "aktau", Interface: "awg1", InterfacePresent: true,
			InterfaceSupported: false, Applicable: false,
		}},
		Routes: []routePreview{{
			Kind: "domain", Target: "chatgpt.com", ServerID: "aktau", Interface: "awg1", Applicable: false,
		}},
	}
	actions, updates, preparable, err := a.planTransports(preview)
	if err != nil {
		t.Fatal(err)
	}
	if len(actions) != 0 || len(updates) != 0 || !preparable["awg1"] {
		t.Fatalf("actions=%v updates=%v preparable=%v", actions, updates, preparable)
	}
	desired, err := buildDesired(preview, preparable, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if len(desired.Blockers) != 0 || len(desired.Groups["awg1"].Domains) != 1 {
		t.Fatalf("desired=%+v", desired)
	}
}

func TestRenderHTTPTransportDoesNotExposeAutoRoute(t *testing.T) {
	raw := rawTransport{ServerID: "proxyde", InterfaceName: "proxyde", TransportType: "http-proxy-tun", Config: map[string]any{
		"server": "203.0.113.10", "server_port": float64(8080), "proxy_type": "http",
	}}
	data, err := renderSingBox(raw, "proxyde")
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	if !strings.Contains(text, `"interface_name": "proxyde"`) || !strings.Contains(text, `"auto_route": false`) {
		t.Fatalf("config=%s", text)
	}
}

func TestManagedBlockPreservesManualLines(t *testing.T) {
	original := "manual.example\n" + beginMarker + "\nold.example\n" + endMarker + "\nkeep.example\n"
	updated := replaceManagedBlock(original, []string{"new.example"})
	want := "manual.example\nkeep.example\n" + beginMarker + "\nnew.example\n" + endMarker + "\n"
	if updated != want {
		t.Fatalf("updated:\n%s\nwant:\n%s", updated, want)
	}
	if got := extractManagedLines(updated); len(got) != 1 || got[0] != "new.example" {
		t.Fatalf("managed=%v", got)
	}
}

func TestObserveWritesDesiredAndDiffWithoutChangingOverrides(t *testing.T) {
	stateDir := t.TempDir()
	overrideDir := t.TempDir()
	path := filepath.Join(overrideDir, "force-proxyde.domains")
	if err := os.WriteFile(path, []byte("manual.example\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := &agent{opts: options{Mode: "observe", StateDir: stateDir, OverrideDir: overrideDir}, now: func() time.Time { return time.Unix(0, 0) }}
	desired := desiredState{SchemaVersion: 1, PolicySource: "live", Groups: map[string]routeSet{"proxyde": {Domains: []string{"example.com"}}}}
	diff, updates, err := a.planUpdates(desired)
	if err != nil {
		t.Fatal(err)
	}
	if len(diff) != 1 || len(updates) != 1 {
		t.Fatalf("diff=%v updates=%d", diff, len(updates))
	}
	data, _ := os.ReadFile(path)
	if string(data) != "manual.example\n" {
		t.Fatalf("observe changed file: %q", data)
	}
}

func TestValidateRequiresExplicitApplyGate(t *testing.T) {
	a := &agent{opts: options{Mode: "apply", PollInterval: time.Minute}}
	if err := a.validate(); err == nil {
		t.Fatal("expected apply gate failure")
	}
	a.opts.AllowApply = true
	if err := a.validate(); err != nil {
		t.Fatal(err)
	}
}

func TestValidateRequiresOneShotTransportPrepareGate(t *testing.T) {
	a := &agent{opts: options{Mode: "prepare", PollInterval: time.Minute}}
	if err := a.validate(); err == nil {
		t.Fatal("expected prepare gate failure")
	}
	a.opts.AllowTransportPrepare = true
	if err := a.validate(); err == nil {
		t.Fatal("continuous prepare mode must be rejected")
	}
	a.opts.Once = true
	if err := a.validate(); err != nil {
		t.Fatal(err)
	}
}

func TestObserveDoesNotAdvertiseTransportManagement(t *testing.T) {
	if canManageTransports("observe") || canManageTransports("disabled") {
		t.Fatal("non-apply modes must not advertise transport management")
	}
	if !canManageTransports("apply") {
		t.Fatal("apply mode must advertise transport management")
	}
}

func TestApplyTransactionRestoresFileWhenApplyFails(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "force-proxyde.domains")
	if err := os.WriteFile(path, []byte("before\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := &agent{
		opts: options{StateDir: filepath.Join(root, "state"), ApplyCommand: "fail", HealthURL: "http://invalid"},
		now:  func() time.Time { return time.Unix(0, 0) },
		runCommand: func(_ context.Context, command string) error {
			if command == "fail" {
				return errors.New("planned failure")
			}
			return nil
		},
	}
	rolledBack, err := a.applyTransaction(context.Background(), map[string][]byte{path: []byte("after\n")}, nil, nil, nil)
	if err == nil || !rolledBack {
		t.Fatalf("rolledBack=%t err=%v", rolledBack, err)
	}
	data, readErr := os.ReadFile(path)
	if readErr != nil || string(data) != "before\n" {
		t.Fatalf("restored=%q err=%v", data, readErr)
	}
}

func TestBuildDesiredRejectsInvalidCriticalPattern(t *testing.T) {
	preview := previewResponse{
		OK: true, Configured: true, Source: "live",
		CriticalServices: []criticalService{{ServiceKey: "broken", Targets: []string{"https://example.com/"}, FailurePattern: "("}},
	}
	if _, err := buildDesired(preview, nil, time.Now()); err == nil || !strings.Contains(err.Error(), "failure pattern") {
		t.Fatalf("invalid critical pattern was accepted: %v", err)
	}
}

func TestHealthCheckRequiresAndValidatesCriticalServices(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	mux.HandleFunc("/service", func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("service ready")) })
	server := httptest.NewServer(mux)
	defer server.Close()

	a := &agent{opts: options{HealthURL: server.URL + "/healthz"}, httpClient: server.Client()}
	if err := a.healthCheck(context.Background(), nil, nil); err == nil || !strings.Contains(err.Error(), "not configured") {
		t.Fatalf("empty critical service set was accepted: %v", err)
	}
	services := []criticalService{{ServiceKey: "work", Label: "Work", Targets: []string{server.URL + "/service"}, SuccessPattern: "service\\s+ready", FailurePattern: "blocked"}}
	if err := a.healthCheck(context.Background(), services, nil); err != nil {
		t.Fatalf("valid service failed: %v", err)
	}
	services[0].FailurePattern = "ready"
	if err := a.healthCheck(context.Background(), services, nil); err == nil || !strings.Contains(err.Error(), "Work") {
		t.Fatalf("failure pattern was ignored: %v", err)
	}
}

func TestCriticalServiceTCPProbe(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go func() {
		connection, acceptErr := listener.Accept()
		if acceptErr == nil {
			_ = connection.Close()
		}
	}()

	a := &agent{httpClient: http.DefaultClient}
	services := []criticalService{{ServiceKey: "telegram", Targets: []string{"tcp://" + listener.Addr().String()}}}
	if failures := a.checkCriticalServices(context.Background(), services, nil); len(failures) != 0 {
		t.Fatalf("TCP probe failed: %v", failures)
	}
}

func TestProbeProxyForInterfaceAndTCPConnect(t *testing.T) {
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodConnect || r.Host != "example.com:443" {
			http.Error(w, "unexpected proxy request", http.StatusBadRequest)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer proxy.Close()
	proxyURL, err := url.Parse(proxy.URL)
	if err != nil {
		t.Fatal(err)
	}
	port, err := strconv.Atoi(proxyURL.Port())
	if err != nil {
		t.Fatal(err)
	}
	cachePath := filepath.Join(t.TempDir(), "policy.json")
	cache := cachedPolicy{CachedAt: time.Now().UTC().Format(time.RFC3339)}
	cache.Config.TransportPlan = []rawTransport{{
		ServerID: "proxyde", InterfaceName: "proxyde", TransportType: "http-proxy-tun",
		Config: map[string]any{"server": proxyURL.Hostname(), "server_port": port},
	}}
	data, _ := json.Marshal(cache)
	if err := os.WriteFile(cachePath, data, 0o600); err != nil {
		t.Fatal(err)
	}
	a := &agent{opts: options{PolicyCache: cachePath}, httpClient: http.DefaultClient}
	resolved := a.probeProxyForInterface("proxyde")
	if resolved == nil || resolved.Host != proxyURL.Host {
		t.Fatalf("resolved proxy=%v", resolved)
	}
	result := a.probeTarget(context.Background(), "tcp://example.com:443", "proxyde", time.Second, 3*time.Second, "", "")
	if result["ok"] != true || result["probe_type"] != "tcp" {
		t.Fatalf("TCP proxy result=%v", result)
	}
}

func TestTargetInterfaceUsesDomainAndMostSpecificCIDR(t *testing.T) {
	groups := map[string]routeSet{
		"proxyde": {Domains: []string{"openai.com"}, IPs: []string{"149.154.160.0/20"}},
		"proxyfr": {IPs: []string{"149.154.167.0/24"}},
		"wan":     {Domains: []string{"gosuslugi.ru"}},
	}
	if got := targetInterface("https://api.openai.com/", groups); got != "proxyde" {
		t.Fatalf("domain interface=%q", got)
	}
	if got := targetInterface("tcp://149.154.167.50:443", groups); got != "proxyfr" {
		t.Fatalf("IP interface=%q", got)
	}
	if got := targetInterface("https://gosuslugi.ru/", groups); got != "" {
		t.Fatalf("direct interface=%q", got)
	}
}

func TestPBRMarkForInterfaceRules(t *testing.T) {
	rules := `29990: from all fwmark 0xb0000/0xff0000 lookup pbr_proxynl
29991: from all fwmark 0xa0000/0xff0000 lookup pbr_proxyde
30000: from all fwmark 0x10000/0xff0000 lookup pbr_wan`
	mark, ok := pbrMarkForInterfaceRules(rules, "proxyde")
	if !ok || mark != 0xa0000 {
		t.Fatalf("proxyde mark=%#x ok=%v", mark, ok)
	}
	if mark, ok := pbrMarkForInterfaceRules(rules, "proxyfr"); ok || mark != 0 {
		t.Fatalf("unexpected proxyfr mark=%#x ok=%v", mark, ok)
	}
}

func TestManagedTransportConfigsArePrivate(t *testing.T) {
	if got := managedFileMode("/etc/sing-box/lokvpn-de1.json"); got != 0o600 {
		t.Fatalf("sing-box mode=%#o", got)
	}
	if got := managedFileMode("/etc/init.d/sing-box-lokvpn-de1"); got != 0o755 {
		t.Fatalf("init mode=%#o", got)
	}
}

func TestProbeJobRoundTripUsesSemanticPatterns(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("service ready"))
	}))
	defer target.Close()

	tokenPath := filepath.Join(t.TempDir(), "agent.token")
	if err := os.WriteFile(tokenPath, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	posted := make(chan map[string]any, 1)
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-token" {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/api/agent/probe-jobs":
			_ = json.NewEncoder(w).Encode(map[string]any{"jobs": []map[string]any{{
				"id": "probe-test", "domain": "example.com", "url": target.URL,
				"candidate_server_ids": []string{"proxyde"}, "connect_timeout": 2, "max_time": 4,
				"success_pattern": "service\\s+ready", "failure_pattern": "blocked",
			}}})
		case "/api/agent/probe-jobs/result":
			var payload map[string]any
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Error(err)
			}
			posted <- payload
			_ = json.NewEncoder(w).Encode(map[string]any{"ok": true})
		default:
			http.NotFound(w, r)
		}
	}))
	defer control.Close()

	a := &agent{
		opts:       options{ControlURL: control.URL, TokenFile: tokenPath, ProbeLimit: 2},
		httpClient: control.Client(),
	}
	preview := previewResponse{
		DeviceID: "cudy-home", Source: "live",
		TransportPlan: []transportPreview{{ServerID: "proxyde", InterfacePresent: true}},
	}
	summary, err := a.processProbeJobs(context.Background(), preview)
	if err != nil {
		t.Fatal(err)
	}
	if summary.Claimed != 1 || summary.Completed != 1 || summary.Failed != 0 {
		t.Fatalf("summary=%+v", summary)
	}
	payload := <-posted
	result, _ := payload["result"].(map[string]any)
	winner, _ := result["winner"].(map[string]any)
	if winner == nil || winner["server_id"] != "proxyde" || winner["semantic_status"] != "ok" {
		t.Fatalf("payload=%+v", payload)
	}
}

func TestProbeRejectsGeoBlockAndFailurePattern(t *testing.T) {
	if !bodyHasGeoBlock("Service is NOT AVAILABLE IN YOUR COUNTRY") {
		t.Fatal("geo block evidence was missed")
	}
	if !bodyHasGeoBlock("Gemini isn&rsquo;t currently supported in your country") {
		t.Fatal("HTML-escaped Gemini geo block evidence was missed")
	}
	if !bodyHasGeoBlock("Gemini isn&#39;t currently supported in your country") {
		t.Fatal("numeric HTML-escaped Gemini geo block evidence was missed")
	}
	matched, err := patternMatches("access\\s+denied", "Access denied", false)
	if err != nil || !matched {
		t.Fatalf("matched=%t err=%v", matched, err)
	}
	if _, err := patternMatches("(", "body", false); err == nil {
		t.Fatal("invalid regex was accepted")
	}
}
