using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.Net;
using Android.OS;
using Android.Util;
using IO.Nekohasekai.Libbox;
using Renci.SshNet;
using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace CudyAndroidAgent;

[Service(
    Name = "com.nashvpn.cudyagent.CudyVpnService",
    Permission = "android.permission.BIND_VPN_SERVICE",
    Exported = false,
    ForegroundServiceType = ForegroundService.TypeDataSync)]
[IntentFilter(new[] { "android.net.VpnService" })]
public class CudyVpnService : VpnService
{
    public const string ActionStart = "com.nashvpn.cudyagent.START";
    public const string ActionStop = "com.nashvpn.cudyagent.STOP";
    private const int NotificationId = 24061;
    private const string NotificationChannelId = "cudy-agent";
    private const string LogTag = "CudyAgent";
    private static readonly object ActiveInstanceLock = new();
    private static WeakReference<CudyVpnService>? activeInstance;

    private ParcelFileDescriptor? tun;
    private CancellationTokenSource? loopCts;
    private Task? loopTask;
    private SshClient? sshClient;
    private readonly object sshRequestLock = new();
    private CudyAndroidLibboxEngine? libboxEngine;
    private bool useSshControl;
    private readonly object policyRoutesLock = new();
    private List<(string Address, int Prefix)> policyIpv4Routes = new();
    private string sshControlHost = "";
    private string sshControlUser = "";
    private string sshControlHostKeySha256 = "";
    private string sshControlKey = "";
    private string debugProbeUrl = "";
    private string debugProbeCandidates = "";
    private bool debugProbePending;
    private IReadOnlyList<CudyCriticalService> criticalServices = Array.Empty<CudyCriticalService>();
    private int consecutiveCriticalFailures;
    private string activeStartFingerprint = "";

    public override void OnCreate()
    {
        base.OnCreate();
        lock (ActiveInstanceLock)
        {
            activeInstance = new WeakReference<CudyVpnService>(this);
        }
    }

    public static bool HasSharedControl
    {
        get
        {
            lock (ActiveInstanceLock)
            {
                return activeInstance is not null
                    && activeInstance.TryGetTarget(out var service)
                    && service.useSshControl;
            }
        }
    }

    public static Task<CudySshControl.ControlResponse> RunSharedControlRequestAsync(
        string method,
        string? cookie,
        string path,
        string? body)
    {
        CudyVpnService service;
        lock (ActiveInstanceLock)
        {
            if (activeInstance is null || !activeInstance.TryGetTarget(out service!))
            {
                throw new InvalidOperationException("The Android agent service is not running.");
            }
        }
        return Task.Run(() => service.RunSharedControlRequest(method, cookie, path, body));
    }

    public override StartCommandResult OnStartCommand(Intent? intent, StartCommandFlags flags, int startId)
    {
        if (intent?.Action == ActionStop)
        {
            StopAgent("stopped");
            return StartCommandResult.NotSticky;
        }

        StartAgent(intent);
        return StartCommandResult.Sticky;
    }

    public override void OnDestroy()
    {
        lock (ActiveInstanceLock)
        {
            if (activeInstance is not null
                && activeInstance.TryGetTarget(out var service)
                && ReferenceEquals(service, this))
            {
                activeInstance = null;
            }
        }
        StopAgent("stopped");
        base.OnDestroy();
    }

    private void StartAgent(Intent? intent)
    {
        var controlUrl = (intent?.GetStringExtra("control_url") ?? "").Trim().TrimEnd('/');
        var deviceId = (intent?.GetStringExtra("device_id") ?? "").Trim();
        var token = intent?.GetStringExtra("token") ?? "";
        var sshHost = (intent?.GetStringExtra("ssh_host") ?? "").Trim();
        var sshUser = (intent?.GetStringExtra("ssh_user") ?? "").Trim();
        var sshHostKeySha256 = (intent?.GetStringExtra("ssh_host_key_sha256") ?? "").Trim();
        var sshKey = intent?.GetStringExtra("ssh_key") ?? "";
        var controlOnly = intent?.GetBooleanExtra("control_only", false) ?? false;
        var startupDelaySeconds = Math.Clamp(intent?.GetIntExtra("startup_delay_seconds", 0) ?? 0, 0, 300);
        debugProbeUrl = (intent?.GetStringExtra("debug_probe_url") ?? "").Trim();
        debugProbeCandidates = (intent?.GetStringExtra("debug_probe_candidates") ?? "").Trim();
        debugProbePending = !string.IsNullOrWhiteSpace(debugProbeUrl)
            && !string.IsNullOrWhiteSpace(debugProbeCandidates);
        Log.Info(LogTag, $"Start requested controlOnly={controlOnly} controlUrl={controlUrl} deviceId={deviceId}");
        if (string.IsNullOrWhiteSpace(controlUrl) || string.IsNullOrWhiteSpace(token))
        {
            StopAgent("missing control URL or token");
            return;
        }

        var startFingerprint = StartFingerprint(
            controlUrl,
            deviceId,
            sshHost,
            sshUser,
            sshHostKeySha256,
            controlOnly);
        if (loopTask is { IsCompleted: false }
            && string.Equals(activeStartFingerprint, startFingerprint, StringComparison.Ordinal))
        {
            Log.Info(LogTag, "Duplicate start request ignored; control loop and TUN remain active");
            return;
        }
        activeStartFingerprint = startFingerprint;

        SaveServiceStatus("starting");
        StartForeground(NotificationId, BuildNotification("Starting"));
        if (!string.IsNullOrWhiteSpace(sshHost) && !string.IsNullOrWhiteSpace(sshUser) && !string.IsNullOrWhiteSpace(sshKey))
        {
            sshControlHost = sshHost;
            sshControlUser = sshUser;
            sshControlHostKeySha256 = sshHostKeySha256;
            sshControlKey = sshKey;
            useSshControl = true;
            SaveServiceStatus("ssh control pending");
            Log.Info(LogTag, $"SSH control pending {sshHost}:22 -> 127.0.0.1:8765");
        }

        libboxEngine ??= new CudyAndroidLibboxEngine(this);
        tun?.Close();
        tun = null;
        SaveServiceStatus(controlOnly ? "control-only started" : "libbox engine starting");

        loopCts?.Cancel();
        loopCts = new CancellationTokenSource();
        Log.Info(LogTag, "Starting control loop task");
        loopTask = Task.Run(
            () => RunControlLoopAsync(controlUrl, deviceId, token, controlOnly, startupDelaySeconds, loopCts.Token),
            loopCts.Token);
    }

    private void StopAgent(string? finalStatus)
    {
        loopCts?.Cancel();
        loopCts = null;
        loopTask = null;
        libboxEngine?.Stop();
        tun?.Close();
        tun = null;
        lock (sshRequestLock)
        {
            try
            {
                sshClient?.Disconnect();
                sshClient?.Dispose();
            }
            catch
            {
                // Best effort shutdown.
            }
            sshClient = null;
        }
        useSshControl = false;
        sshControlHost = "";
        sshControlUser = "";
        sshControlHostKeySha256 = "";
        sshControlKey = "";
        debugProbeUrl = "";
        debugProbeCandidates = "";
        debugProbePending = false;
        activeStartFingerprint = "";
        if (!string.IsNullOrWhiteSpace(finalStatus))
        {
            SaveServiceStatus(finalStatus);
        }
        StopSelf();
    }

    private static string StartFingerprint(
        string controlUrl,
        string deviceId,
        string sshHost,
        string sshUser,
        string sshHostKeySha256,
        bool controlOnly)
    {
        var value = string.Join(
            '\n',
            controlUrl,
            deviceId,
            sshHost,
            sshUser,
            sshHostKeySha256,
            controlOnly ? "control-only" : "vpn");
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value)));
    }

    private void StartSshControl(string host, string user, string privateKey, string hostKeySha256)
    {
        lock (sshRequestLock)
        {
            sshClient?.Disconnect();
            sshClient?.Dispose();
            sshClient = null;

            var client = CudySshControl.CreateClient(host, user, privateKey, hostKeySha256);
            Log.Info(LogTag, $"SSH control connecting {host}:22");
            client.Connect();

            sshClient = client;
            sshControlHost = host;
            sshControlUser = user;
            sshControlHostKeySha256 = hostKeySha256;
            sshControlKey = privateKey;
            SaveServiceStatus("ssh control ok");
            Log.Info(LogTag, $"SSH control ok {host}:22 -> 127.0.0.1:8765");
        }
    }

    private string RunSshControlWithRetry(string method, string token, string path, string? body)
    {
        lock (sshRequestLock)
        {
            try
            {
                return RunSshControlOnce(method, token, path, body);
            }
            catch (Exception firstError)
            {
                Log.Warn(LogTag, $"SSH control request failed, reconnecting once: {firstError.Message}");
                ReconnectSshControl();
                return RunSshControlOnce(method, token, path, body);
            }
        }
    }

    private CudySshControl.ControlResponse RunSharedControlRequest(
        string method,
        string? cookie,
        string path,
        string? body)
    {
        lock (sshRequestLock)
        {
            try
            {
                if (sshClient?.IsConnected != true)
                {
                    ReconnectSshControl();
                }
                return CudySshControl.RunControlRequestDetailed(
                    sshClient ?? throw new InvalidOperationException("SSH control client is not connected."),
                    method,
                    null,
                    cookie,
                    path,
                    body);
            }
            catch (Exception firstError)
            {
                Log.Warn(LogTag, $"Shared admin request failed, reconnecting once: {firstError.Message}");
                ReconnectSshControl();
                return CudySshControl.RunControlRequestDetailed(
                    sshClient ?? throw new InvalidOperationException("SSH control client is not connected."),
                    method,
                    null,
                    cookie,
                    path,
                    body);
            }
        }
    }

    private string RunSshControlOnce(string method, string token, string path, string? body)
    {
        if (!useSshControl)
        {
            throw new InvalidOperationException("SSH control is not enabled.");
        }
        if (sshClient?.IsConnected != true)
        {
            ReconnectSshControl();
        }
        return CudySshControl.RunControlRequest(
            sshClient ?? throw new InvalidOperationException("SSH control client is not connected."),
            method,
            token,
            path,
            body);
    }

    private void ReconnectSshControl()
    {
        if (string.IsNullOrWhiteSpace(sshControlHost)
            || string.IsNullOrWhiteSpace(sshControlUser)
            || string.IsNullOrWhiteSpace(sshControlKey))
        {
            throw new InvalidOperationException("SSH control settings are missing.");
        }

        try
        {
            sshClient?.Disconnect();
            sshClient?.Dispose();
        }
        catch
        {
            // Best effort reconnect.
        }

        var client = CudySshControl.CreateClient(
            sshControlHost,
            sshControlUser,
            sshControlKey,
            sshControlHostKeySha256);
        client.Connect();
        sshClient = client;
        Log.Info(LogTag, $"SSH control reconnected {sshControlHost}:22");
    }

    private async Task RunControlLoopAsync(
        string controlUrl,
        string deviceId,
        string token,
        bool controlOnly,
        int startupDelaySeconds,
        CancellationToken cancellationToken)
    {
        Log.Info(LogTag, "Control loop task started");
        if (startupDelaySeconds > 0)
        {
            SaveServiceStatus($"waiting for network after boot ({startupDelaySeconds}s)");
            Log.Info(LogTag, $"Control loop waiting {startupDelaySeconds}s before first policy fetch");
            await Task.Delay(TimeSpan.FromSeconds(startupDelaySeconds), cancellationToken);
        }

        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        while (!cancellationToken.IsCancellationRequested)
        {
            var ok = false;
            var error = "";
            var domainRoutes = 0;
            var ipRoutes = 0;
            var cleanupRoutes = 0;
            var transports = 0;
            var preparedTransports = 0;
            var storedTransports = 0;
            var runtimeSummary = "libbox=not-probed";
            var engineSummary = controlOnly ? "engine=control-only" : "engine=not-started";
            var probeSummary = "probe_jobs jobs=0 completed=0 failed=0";
            try
            {
                Log.Info(LogTag, "Control loop fetching policy");
                var configJson = await GetControlStringAsync(client, controlUrl, token, "/api/agent/config", cancellationToken);
                SavePolicySummary(configJson);
                using var doc = JsonDocument.Parse(configJson);
                var root = doc.RootElement;
                ApplyAuthenticatedControlEndpoint(root);
                criticalServices = CudyCriticalServiceMonitor.Parse(root);
                domainRoutes = ArrayLength(root, "domain_routes");
                ipRoutes = ArrayLength(root, "ip_routes");
                cleanupRoutes = ArrayLength(root, "cleanup_ip_routes");
                var transportPlan = CudyTransportPlan.Parse(root);
                transports = transportPlan.Count;
                CudyPreparedTransport[] prepared = transportPlan.Count > 0
                    ? new[]
                    {
                        CudySingBoxConfig.BuildAndroidUnified(
                            root,
                            transportPlan,
                            localProbes: CudyAndroidProbeRunner.BuildLocalProbes(transportPlan)),
                    }
                    : Array.Empty<CudyPreparedTransport>();
                preparedTransports = prepared.Length;
                var stored = StorePreparedTransports(prepared);
                storedTransports = stored.Count;
                runtimeSummary = CudySingBoxRuntime.Probe(stored).SafeSummary();
                if (!controlOnly && stored.Count > 0)
                {
                    SetPolicyRoutes(root);
                    engineSummary = libboxEngine?.StartOrReload(stored[0]) ?? "engine=unavailable";
                    if (libboxEngine is not null)
                    {
                        var probeRunner = new CudyAndroidProbeRunner(deviceId);
                        var probes = await probeRunner.RunAsync(
                            root,
                            transportPlan,
                            (path, tokenArg) => GetControlStringAsync(client, controlUrl, token, path, tokenArg),
                            (path, json, tokenArg) => PostControlJsonAsync(client, controlUrl, token, path, json, tokenArg),
                            cancellationToken);
                        probeSummary = probes.SafeSummary();
                        if (debugProbePending)
                        {
                            debugProbePending = false;
                            var debugResult = await probeRunner.RunDebugAsync(
                                root,
                                transportPlan,
                                debugProbeUrl,
                                ParseCsv(debugProbeCandidates),
                                cancellationToken);
                            var debugJson = JsonSerializer.Serialize(debugResult);
                            SaveDebugProbeResult(debugJson);
                            Log.Info(LogTag, "Debug probe result: " + debugJson);
                            probeSummary += " debug_probe=done";
                        }
                    }
                }

                var criticalResult = controlOnly
                    ? new CudyCriticalCheckResult(Array.Empty<CudyCriticalServiceResult>())
                    : await CudyCriticalServiceMonitor.CheckAsync(client, criticalServices, cancellationToken);
                if (criticalResult.Ok)
                {
                    consecutiveCriticalFailures = 0;
                }
                else
                {
                    consecutiveCriticalFailures++;
                    Log.Warn(LogTag, $"Critical connectivity failure {consecutiveCriticalFailures}/3: {string.Join(", ", criticalResult.FailedServices)}");
                }

                var installedAppVersion = InstalledAppVersion();
                var status = new
                {
                    schema_version = 1,
                    platform = "android",
                    agent_version = "0.1",
                    app_version_name = installedAppVersion.Name,
                    app_version_code = installedAppVersion.Code,
                    reported_at = DateTimeOffset.UtcNow.ToString("O"),
                    device_id = deviceId,
                    vpn_interfaces = new[] { "android-vpn-placeholder" },
                    routes = new
                    {
                        domain_count = domainRoutes,
                        ip_route_count = ipRoutes,
                        cleanup_ip_route_count = cleanupRoutes,
                    },
                    health = new
                    {
                        ok = criticalResult.Ok,
                        mode = controlOnly ? "android-control-only" : "android-libbox",
                        transports,
                        prepared_transports = preparedTransports,
                        stored_transports = storedTransports,
                        sing_box_runtime = runtimeSummary,
                        sing_box_engine = engineSummary,
                        probe_jobs = probeSummary,
                        tunnel_established = tun is not null,
                        control_tunnel_established = useSshControl && sshClient?.IsConnected == true,
                        configured_control_host = sshControlHost,
                        critical_services_ok = criticalResult.Ok,
                        critical_service_failures = consecutiveCriticalFailures,
                    },
                    capabilities = new
                    {
                        can_probe = !controlOnly && storedTransports > 0,
                        can_route = !controlOnly && tun is not null,
                        can_manage_transports = !controlOnly && storedTransports > 0,
                    },
                    errors = criticalResult.FailedServices,
                };
                await PostControlJsonAsync(
                    client,
                    controlUrl,
                    token,
                    "/api/agent/status",
                    JsonSerializer.Serialize(status),
                    cancellationToken);
                ok = criticalResult.Ok;
                SaveLoopDetails(
                    domainRoutes,
                    ipRoutes,
                    cleanupRoutes,
                    transports,
                    storedTransports,
                    runtimeSummary,
                    engineSummary,
                    probeSummary,
                    useSshControl && sshClient?.IsConnected == true,
                    error: "");
                SaveServiceStatus(criticalResult.Ok
                    ? $"ok ip={ipRoutes} cleanup={cleanupRoutes} transports={transports} prepared={preparedTransports} stored={storedTransports} {runtimeSummary} {engineSummary} {probeSummary}"
                    : $"critical services unavailable: {string.Join(", ", criticalResult.FailedServices)}");
                Log.Info(LogTag, $"Control loop {(criticalResult.Ok ? "ok" : "degraded")} ip={ipRoutes} cleanup={cleanupRoutes} transports={transports} prepared={preparedTransports} stored={storedTransports} {runtimeSummary} {engineSummary} {probeSummary}");

                if (consecutiveCriticalFailures >= 3)
                {
                    var diagnostic = new
                    {
                        summary = "Android agent watchdog: critical connectivity failure",
                        report = JsonSerializer.Serialize(new
                        {
                            device_id = deviceId,
                            consecutive_failures = consecutiveCriticalFailures,
                            failed_services = criticalResult.FailedServices,
                            services = criticalResult.Services,
                            action = "stop_vpn_restore_direct",
                            reported_at = DateTimeOffset.UtcNow.ToString("O"),
                        }),
                    };
                    try
                    {
                        await PostControlJsonAsync(client, controlUrl, token, "/api/agent/diagnostics", JsonSerializer.Serialize(diagnostic), cancellationToken);
                    }
                    catch (Exception ex) when (ex is not System.OperationCanceledException)
                    {
                        Log.Warn(LogTag, "Critical diagnostic report failed: " + ex.Message);
                    }
                    SaveServiceStatus("watchdog stopped VPN; direct internet restored");
                    UpdateNotification("Safety stop: direct internet restored");
                    StopAgent(finalStatus: null);
                    return;
                }
            }
            catch (Exception ex) when (ex is not System.OperationCanceledException)
            {
                error = ex.Message;
                SaveLoopDetails(
                    domainRoutes,
                    ipRoutes,
                    cleanupRoutes,
                    transports,
                    storedTransports,
                    runtimeSummary,
                    engineSummary,
                    probeSummary,
                    useSshControl && sshClient?.IsConnected == true,
                    error);
                SaveServiceStatus($"error {error}");
                Log.Warn(LogTag, $"Control loop error: {error}");
            }

            UpdateNotification(ok
                ? $"Policy ok: ip={ipRoutes}, cleanup={cleanupRoutes}, transports={transports}/{storedTransports}"
                : $"Control error: {error}");

            try
            {
                await Task.Delay(TimeSpan.FromSeconds(60), cancellationToken);
            }
            catch (System.OperationCanceledException)
            {
                break;
            }
        }
    }

    private void ApplyAuthenticatedControlEndpoint(JsonElement root)
    {
        if (!useSshControl
            || !root.TryGetProperty("control", out var control)
            || control.ValueKind != JsonValueKind.Object
            || !control.TryGetProperty("endpoints", out var manifest)
            || manifest.ValueKind != JsonValueKind.Object
            || !manifest.TryGetProperty("endpoints", out var endpoints)
            || endpoints.ValueKind != JsonValueKind.Array)
        {
            return;
        }

        string? selectedHost = null;
        string? selectedHostKey = null;
        var selectedPriority = int.MaxValue;
        foreach (var endpoint in endpoints.EnumerateArray())
        {
            if (endpoint.ValueKind != JsonValueKind.Object
                || !endpoint.TryGetProperty("role", out var role)
                || !string.Equals(role.GetString(), "primary", StringComparison.OrdinalIgnoreCase)
                || !endpoint.TryGetProperty("ssh_tunnel", out var tunnel)
                || tunnel.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            var host = tunnel.TryGetProperty("host", out var hostElement)
                ? (hostElement.GetString() ?? "").Trim()
                : "";
            var hostKey = tunnel.TryGetProperty("host_key_sha256", out var hostKeyElement)
                ? (hostKeyElement.GetString() ?? "").Trim()
                : "";
            var priority = endpoint.TryGetProperty("priority", out var priorityElement)
                && priorityElement.TryGetInt32(out var parsedPriority)
                    ? parsedPriority
                    : 1000;
            if (string.IsNullOrWhiteSpace(host)
                || host.Any(char.IsWhiteSpace)
                || !hostKey.StartsWith("SHA256:", StringComparison.Ordinal)
                || hostKey.Any(char.IsWhiteSpace)
                || priority >= selectedPriority)
            {
                continue;
            }
            selectedHost = host;
            selectedHostKey = hostKey;
            selectedPriority = priority;
        }

        if (string.IsNullOrWhiteSpace(selectedHost)
            || string.IsNullOrWhiteSpace(selectedHostKey)
            || (string.Equals(selectedHost, sshControlHost, StringComparison.OrdinalIgnoreCase)
                && string.Equals(selectedHostKey, sshControlHostKeySha256, StringComparison.Ordinal)))
        {
            return;
        }

        var previousHost = sshControlHost;
        sshControlHost = selectedHost;
        sshControlHostKeySha256 = selectedHostKey;
        GetSharedPreferences("cudy-agent", FileCreationMode.Private)
            ?.Edit()
            ?.PutString("ssh_host", selectedHost)
            ?.PutString("ssh_host_key_sha256", selectedHostKey)
            ?.Apply();
        Log.Info(
            LogTag,
            $"Authenticated control endpoint cached: {previousHost} -> {selectedHost}; active SSH session is kept until reconnect");
    }

    private async Task<string> GetControlStringAsync(
        HttpClient client,
        string controlUrl,
        string token,
        string path,
        CancellationToken cancellationToken)
    {
        if (useSshControl)
        {
            Log.Info(LogTag, $"SSH control GET {path}");
            return await Task.Run(
                () =>
                {
                    var result = RunSshControlWithRetry("GET", token, path, body: null);
                    Log.Info(LogTag, $"SSH control GET ok {path} bytes={result.Length}");
                    return result;
                },
                cancellationToken);
        }

        using var configReply = await client.GetAsync(controlUrl + path, cancellationToken);
        var configJson = await configReply.Content.ReadAsStringAsync(cancellationToken);
        configReply.EnsureSuccessStatusCode();
        return configJson;
    }

    private async Task PostControlJsonAsync(
        HttpClient client,
        string controlUrl,
        string token,
        string path,
        string json,
        CancellationToken cancellationToken)
    {
        if (useSshControl)
        {
            Log.Info(LogTag, $"SSH control POST {path} bytes={json.Length}");
            await Task.Run(
                () =>
                {
                    var result = RunSshControlWithRetry("POST", token, path, json);
                    Log.Info(LogTag, $"SSH control POST ok {path} bytes={result.Length}");
                },
                cancellationToken);
            return;
        }

        var body = new StringContent(json, Encoding.UTF8, "application/json");
        using var statusReply = await client.PostAsync(controlUrl + path, body, cancellationToken);
        statusReply.EnsureSuccessStatusCode();
    }

    private IReadOnlyList<CudyStoredTransport> StorePreparedTransports(IReadOnlyList<CudyPreparedTransport> prepared)
    {
        if (prepared.Count == 0)
        {
            return Array.Empty<CudyStoredTransport>();
        }
        var filesPath = FilesDir?.AbsolutePath;
        if (string.IsNullOrWhiteSpace(filesPath))
        {
            throw new InvalidOperationException("App private files directory is unavailable.");
        }
        return CudyTransportStore.WriteAll(filesPath, prepared);
    }

    internal int OpenLibboxTun(ITunOptions? options)
    {
        if (Prepare(this) is not null)
        {
            throw new InvalidOperationException("Android VPN permission is not granted.");
        }

        tun?.Close();
        tun = null;

        var mtu = options?.MTU > 0 ? options.MTU : 1400;
        var builder = new Builder(this)
            .SetSession("Cudy Agent")
            .SetMtu(mtu);

        if ((int)Build.VERSION.SdkInt >= 29)
        {
#pragma warning disable CA1416
            builder.SetMetered(false);
#pragma warning restore CA1416
        }

        var addedAddress = false;
        addedAddress |= AddTunAddresses(builder, options?.Inet4Address);
        addedAddress |= AddTunAddresses(builder, options?.Inet6Address);
        if (!addedAddress)
        {
            builder.AddAddress("10.210.0.2", 32);
        }

        if (options?.AutoRoute == true)
        {
            AddTunDnsServers(builder, options);
            var ipv4Routes = AddTunRoutes(builder, options.Inet4RouteRange);
            var ipv6Routes = AddTunRoutes(builder, options.Inet6RouteRange);
            if (ipv4Routes == 0)
            {
                builder.AddRoute("0.0.0.0", 0);
                ipv4Routes = 1;
            }
            AddTunPackages(builder, options.IncludePackage, allowed: true);
            AddTunPackages(builder, options.ExcludePackage, allowed: false);
            Log.Info(LogTag, $"Added Android auto routes: ipv4={ipv4Routes} ipv6={ipv6Routes}");
        }
        else
        {
            AddPolicyRoutes(builder);
        }

        tun = builder.Establish() ?? throw new InvalidOperationException("Android VPN tunnel could not be established.");
        Log.Info(LogTag, "libbox opened Android VPN tun fd=" + tun.Fd);
        return tun.Fd;
    }

    internal void CloseLibboxTun()
    {
        tun?.Close();
        tun = null;
    }

    internal void ProtectLibboxSocket(int fd)
    {
        if (!Protect(fd))
        {
            throw new InvalidOperationException("Android VPN protect() failed for fd=" + fd);
        }
    }

    internal void RequestLibboxServiceStop()
    {
        StopAgent("libbox requested stop");
    }

    private static bool AddTunAddresses(Builder builder, IRoutePrefixIterator? iterator)
    {
        var added = false;
        while (iterator?.HasNext == true)
        {
            var prefix = iterator.Next();
            var address = prefix?.Address();
            var bits = prefix?.Prefix() ?? 0;
            if (!string.IsNullOrWhiteSpace(address) && bits > 0)
            {
                builder.AddAddress(address, bits);
                added = true;
            }
        }
        return added;
    }

    private static int AddTunRoutes(Builder builder, IRoutePrefixIterator? iterator)
    {
        var added = 0;
        while (iterator?.HasNext == true)
        {
            var prefix = iterator.Next();
            var address = prefix?.Address();
            var bits = prefix?.Prefix() ?? 0;
            if (!string.IsNullOrWhiteSpace(address) && bits >= 0)
            {
                builder.AddRoute(address, bits);
                added++;
            }
        }
        return added;
    }

    private static void AddTunDnsServers(Builder builder, ITunOptions options)
    {
        if (string.Equals(options.DNSMode?.Value, Libbox.DNSModeDisabled, StringComparison.OrdinalIgnoreCase))
        {
            return;
        }
        var iterator = options.DNSServerAddress;
        var added = 0;
        while (iterator?.HasNext == true)
        {
            var address = iterator.Next();
            if (string.IsNullOrWhiteSpace(address))
            {
                continue;
            }
            builder.AddDnsServer(address);
            added++;
        }
        Log.Info(LogTag, $"Added Android VPN DNS servers: {added}");
    }

    private static void AddTunPackages(Builder builder, IStringIterator? iterator, bool allowed)
    {
        while (iterator?.HasNext == true)
        {
            var packageName = iterator.Next();
            if (string.IsNullOrWhiteSpace(packageName))
            {
                continue;
            }
            try
            {
                if (allowed)
                {
                    builder.AddAllowedApplication(packageName);
                }
                else
                {
                    builder.AddDisallowedApplication(packageName);
                }
                Log.Info(LogTag, $"Android VPN {(allowed ? "allowed" : "excluded")} package: {packageName}");
            }
            catch (PackageManager.NameNotFoundException ex)
            {
                Log.Warn(LogTag, $"Android VPN package rule ignored for {packageName}: {ex.Message}");
            }
        }
    }

    private void SetPolicyRoutes(JsonElement root)
    {
        var routes = new List<(string Address, int Prefix)>();
        if (root.TryGetProperty("ip_routes", out var ipRoutes) && ipRoutes.ValueKind == JsonValueKind.Array)
        {
            foreach (var route in ipRoutes.EnumerateArray())
            {
                var serverId = route.TryGetProperty("server_id", out var server)
                    ? server.GetString() ?? ""
                    : "";
                if (string.Equals(serverId, "direct", StringComparison.OrdinalIgnoreCase)
                    || string.Equals(serverId, "block", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                var cidr = route.TryGetProperty("target_cidr", out var target)
                    ? target.GetString() ?? ""
                    : "";
                if (TryParseIpv4Cidr(cidr, out var address, out var prefix))
                {
                    routes.Add((address, prefix));
                }
            }
        }

        lock (policyRoutesLock)
        {
            policyIpv4Routes = routes;
        }
    }

    private void AddPolicyRoutes(Builder builder)
    {
        List<(string Address, int Prefix)> routes;
        lock (policyRoutesLock)
        {
            routes = policyIpv4Routes.ToList();
        }
        foreach (var (address, prefix) in routes)
        {
            try
            {
                builder.AddRoute(address, prefix);
            }
            catch (Exception ex)
            {
                Log.Warn(LogTag, $"Skipping Android policy route {address}/{prefix}: {ex.Message}");
            }
        }
        if (routes.Count > 0)
        {
            Log.Info(LogTag, $"Added Android policy routes: {routes.Count}");
        }
    }

    private static bool TryParseIpv4Cidr(string value, out string address, out int prefix)
    {
        address = "";
        prefix = 0;
        var parts = value.Split('/', 2, StringSplitOptions.TrimEntries);
        if (parts.Length != 2 || !IPAddress.TryParse(parts[0], out var ip))
        {
            return false;
        }
        if (ip.AddressFamily != System.Net.Sockets.AddressFamily.InterNetwork)
        {
            return false;
        }
        if (!int.TryParse(parts[1], out prefix) || prefix < 0 || prefix > 32)
        {
            return false;
        }
        address = ip.ToString();
        return true;
    }

    private static int ArrayLength(JsonElement root, string propertyName)
    {
        return root.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.Array
            ? value.GetArrayLength()
            : 0;
    }

    private Android.App.Notification BuildNotification(string text)
    {
        EnsureNotificationChannel();
        Android.App.Notification.Builder builder;
        if ((int)Build.VERSION.SdkInt >= 26)
        {
#pragma warning disable CA1416
            builder = new Android.App.Notification.Builder(this, NotificationChannelId);
#pragma warning restore CA1416
        }
        else
        {
#pragma warning disable CA1422
            builder = new Android.App.Notification.Builder(this);
#pragma warning restore CA1422
        }
        builder
            .SetContentTitle("Cudy Agent")
            .SetContentText(text)
            .SetSmallIcon(Android.Resource.Drawable.StatSysDownloadDone)
            .SetOngoing(true);
        return builder.Build();
    }

    private void UpdateNotification(string text)
    {
        var manager = (NotificationManager?)GetSystemService(NotificationService);
        manager?.Notify(NotificationId, BuildNotification(text));
    }

    private void SaveServiceStatus(string text)
    {
        var preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        preferences?.Edit()
            ?.PutString("service_status", text)
            ?.PutString("service_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
            ?.Apply();
    }

    private void SavePolicySummary(string json)
    {
        var preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        preferences?.Edit()
            ?.PutString("last_policy_summary", CudyPolicy.Summarize(json))
            ?.PutString("last_policy_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
            ?.Apply();
    }

    private (string Name, long Code) InstalledAppVersion()
    {
        try
        {
#pragma warning disable CA1422
            var packageInfo = PackageManager?.GetPackageInfo(PackageName ?? "", 0);
#pragma warning restore CA1422
            if (packageInfo is null)
            {
                return ("", 0);
            }
#pragma warning disable CA1422
            var code = (int)Build.VERSION.SdkInt >= 28
#pragma warning disable CA1416
                ? packageInfo.LongVersionCode
#pragma warning restore CA1416
                : packageInfo.VersionCode;
#pragma warning restore CA1422
            return (packageInfo.VersionName ?? "", code);
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "Cannot read installed app version: " + ex.Message);
            return ("", 0);
        }
    }

    private void SaveLoopDetails(
        int domainRoutes,
        int ipRoutes,
        int cleanupRoutes,
        int transports,
        int storedTransports,
        string runtimeSummary,
        string engineSummary,
        string probeSummary,
        bool controlTunnelEstablished,
        string error)
    {
        var preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        preferences?.Edit()
            ?.PutInt("last_domain_routes", domainRoutes)
            ?.PutInt("last_ip_routes", ipRoutes)
            ?.PutInt("last_cleanup_routes", cleanupRoutes)
            ?.PutInt("last_transports", transports)
            ?.PutInt("last_stored_transports", storedTransports)
            ?.PutString("last_runtime_summary", runtimeSummary)
            ?.PutString("last_engine_summary", engineSummary)
            ?.PutString("last_probe_summary", probeSummary)
            ?.PutBoolean("last_control_tunnel_established", controlTunnelEstablished)
            ?.PutString("last_control_error", error)
            ?.Apply();
    }

    private void SaveDebugProbeResult(string json)
    {
        var preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        preferences?.Edit()
            ?.PutString("debug_probe_result", json)
            ?.PutString("debug_probe_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
            ?.Apply();
    }

    private static IReadOnlyList<string> ParseCsv(string value)
    {
        return value
            .Split(new[] { ',', ' ', ';', '\t', '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToList();
    }

    private void EnsureNotificationChannel()
    {
        if ((int)Build.VERSION.SdkInt < 26)
        {
            return;
        }
#pragma warning disable CA1416
        var manager = (NotificationManager?)GetSystemService(NotificationService);
        var channel = new NotificationChannel(NotificationChannelId, "Cudy Agent", NotificationImportance.Low)
        {
            Description = "Cudy managed routing status",
        };
        manager?.CreateNotificationChannel(channel);
#pragma warning restore CA1416
    }
}
