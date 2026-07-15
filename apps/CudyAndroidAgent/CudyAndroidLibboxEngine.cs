using Android.OS;
using Android.Util;
using Android.Content;
using Android.Net;
using IO.Nekohasekai.Libbox;
using System.Security.Cryptography;
using System.Text;
using Libbox = IO.Nekohasekai.Libbox.Libbox;
using LibboxNetworkInterface = IO.Nekohasekai.Libbox.NetworkInterface;

namespace CudyAndroidAgent;

public sealed class CudyAndroidLibboxEngine : IDisposable
{
    private const string LogTag = "CudyAgent";

    private readonly CudyVpnService service;
    private readonly CudyLibboxPlatform platform;
    private readonly CudyLibboxCommandHandler handler;
    private readonly CudyLibboxCommandClientHandler clientHandler;
    private CommandServer? commandServer;
    private CommandClient? commandClient;
    private bool setupDone;
    private string activeConfigPath = "";
    private string activeServerId = "";
    private string activeConfigHash = "";

    public CudyAndroidLibboxEngine(CudyVpnService service)
    {
        this.service = service;
        platform = new CudyLibboxPlatform(service);
        handler = new CudyLibboxCommandHandler(service);
        clientHandler = new CudyLibboxCommandClientHandler();
    }

    public string StartOrReload(CudyStoredTransport transport)
    {
        EnsureSetup();
        var configJson = File.ReadAllText(transport.ConfigPath);
        var configHash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(configJson)));
        if (commandServer is null)
        {
            commandServer = new CommandServer(handler, platform);
            commandServer.Start();
        }

        if (!string.Equals(activeConfigPath, transport.ConfigPath, StringComparison.Ordinal)
            || !string.Equals(activeServerId, transport.ServerId, StringComparison.Ordinal)
            || !string.Equals(activeConfigHash, configHash, StringComparison.Ordinal))
        {
            commandServer.StartOrReloadService(configJson, new OverrideOptions());
            activeConfigPath = transport.ConfigPath;
            activeServerId = transport.ServerId;
            activeConfigHash = configHash;
        }

        return $"engine=running server={transport.ServerId} iface={transport.InterfaceName}";
    }

    public async Task<CudyNativeProbeResult> RunNativeProbeAsync(
        string url,
        string outboundTag,
        int maxRuntimeSeconds,
        CancellationToken cancellationToken)
    {
        if (commandServer is null)
        {
            throw new InvalidOperationException("libbox service is not running.");
        }

        var client = EnsureCommandClient();
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(Math.Max(3, maxRuntimeSeconds + 3)));

        using var qualityHandler = new CudyNetworkQualityHandler();
        NetworkQualityTestSession? session = null;
        try
        {
            session = client.StartNetworkQualityTest(
                url,
                outboundTag,
                serial: true,
                maxRuntimeSeconds: Math.Max(1, maxRuntimeSeconds),
                http3: false,
                handler: qualityHandler);
            return await qualityHandler.WaitAsync(timeout.Token);
        }
        finally
        {
            try
            {
                session?.Close();
            }
            catch (Exception ex)
            {
                Log.Warn(LogTag, "native probe session close failed: " + ex.Message);
            }
        }
    }

    public async Task<CudyUrlTestResult> RunUrlTestAsync(
        string groupTag,
        IReadOnlyList<string> outboundTags,
        int maxRuntimeSeconds,
        CancellationToken cancellationToken)
    {
        if (commandServer is null)
        {
            throw new InvalidOperationException("libbox service is not running.");
        }
        var client = EnsureCommandClient();
        return await clientHandler.RunUrlTestAsync(
            client,
            groupTag,
            outboundTags,
            Math.Max(2, maxRuntimeSeconds),
            cancellationToken);
    }

    public void Stop()
    {
        try
        {
            commandServer?.CloseService();
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "libbox closeService failed: " + ex.Message);
        }

        try
        {
            commandServer?.Close();
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "libbox close failed: " + ex.Message);
        }

        commandServer = null;
        try
        {
            commandClient?.Disconnect();
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "libbox command client disconnect failed: " + ex.Message);
        }

        commandClient = null;
        activeConfigPath = "";
        activeServerId = "";
        activeConfigHash = "";
        platform.CloseAllDefaultInterfaceMonitors();
        service.CloseLibboxTun();
    }

    public void Dispose()
    {
        Stop();
        platform.Dispose();
        handler.Dispose();
    }

    private void EnsureSetup()
    {
        var filesPath = service.FilesDir?.AbsolutePath
            ?? throw new InvalidOperationException("App private files directory is unavailable.");
        var workPath = Path.Combine(filesPath, "libbox-work");
        var tempPath = service.CacheDir?.AbsolutePath ?? Path.Combine(filesPath, "libbox-temp");
        Directory.CreateDirectory(workPath);
        Directory.CreateDirectory(tempPath);

        var options = new SetupOptions
        {
            BasePath = filesPath,
            WorkingPath = workPath,
            TempPath = tempPath,
            FixAndroidStack = true,
            Debug = true,
            LogMaxLines = 1000,
            CommandServerListenPort = 0,
            CommandServerSecret = "",
        };

        if (setupDone)
        {
            Libbox.ReloadSetupOptions(options);
        }
        else
        {
            Libbox.Setup(options);
            setupDone = true;
        }
    }

    private CommandClient EnsureCommandClient()
    {
        if (commandClient is not null)
        {
            return commandClient;
        }

        var options = new CommandClientOptions
        {
            StatusInterval = 1_000_000_000L,
        };
        options.AddCommand(Libbox.CommandStatus);
        options.AddCommand(Libbox.CommandOutbounds);
        options.AddCommand(Libbox.CommandGroup);
        commandClient = Libbox.NewCommandClient(clientHandler, options)
            ?? throw new InvalidOperationException("Unable to create libbox command client.");
        commandClient.Connect();
        return commandClient;
    }
}

public sealed record CudyNativeProbeResult(
    bool Ok,
    int TimeTotalMs,
    int IdleLatencyMs,
    long DownloadCapacity,
    long UploadCapacity,
    string Status,
    string? Error);

public sealed record CudyUrlTestItem(
    string OutboundTag,
    int DelayMs,
    long TestTime,
    string Status);

public sealed record CudyUrlTestResult(
    IReadOnlyList<CudyUrlTestItem> Items,
    bool Complete,
    string? Error);

public sealed class CudyNetworkQualityHandler : Java.Lang.Object, INetworkQualityTestHandler
{
    private readonly TaskCompletionSource<CudyNativeProbeResult> completion =
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    public void OnError(string? message)
    {
        completion.TrySetResult(new CudyNativeProbeResult(
            Ok: false,
            TimeTotalMs: 0,
            IdleLatencyMs: 0,
            DownloadCapacity: 0,
            UploadCapacity: 0,
            Status: "error",
            Error: message ?? "native network quality test failed"));
    }

    public void OnProgress(NetworkQualityProgress? progress)
    {
    }

    public void OnResult(NetworkQualityResult? result)
    {
        if (result is null)
        {
            OnError("native network quality test returned no result");
            return;
        }

        var latency = Math.Max(1, result.IdleLatencyMs);
        completion.TrySetResult(new CudyNativeProbeResult(
            Ok: true,
            TimeTotalMs: latency,
            IdleLatencyMs: result.IdleLatencyMs,
            DownloadCapacity: result.DownloadCapacity,
            UploadCapacity: result.UploadCapacity,
            Status: "ok",
            Error: null));
    }

    public async Task<CudyNativeProbeResult> WaitAsync(CancellationToken cancellationToken)
    {
        using var registration = cancellationToken.Register(() =>
            completion.TrySetResult(new CudyNativeProbeResult(
                Ok: false,
                TimeTotalMs: 0,
                IdleLatencyMs: 0,
                DownloadCapacity: 0,
                UploadCapacity: 0,
                Status: "timeout",
                Error: "native network quality test timed out")));
        return await completion.Task;
    }
}

public sealed class CudyLibboxCommandClientHandler : Java.Lang.Object, ICommandClientHandler
{
    private const string LogTag = "CudyAgent";
    private readonly object urlTestLock = new();
    private TaskCompletionSource<CudyUrlTestResult>? urlTestCompletion;
    private string urlTestGroupTag = "";
    private HashSet<string> urlTestExpectedTags = new(StringComparer.OrdinalIgnoreCase);
    private Dictionary<string, CudyUrlTestItem> urlTestLatest = new(StringComparer.OrdinalIgnoreCase);

    public void ClearLogs()
    {
    }

    public void Connected()
    {
        Log.Debug(LogTag, "libbox command client connected");
    }

    public void Disconnected(string? message)
    {
        Log.Debug(LogTag, "libbox command client disconnected: " + message);
    }

    public void InitializeClashMode(IStringIterator? modeList, string? currentMode)
    {
    }

    public void SetDefaultLogLevel(int level)
    {
    }

    public void UpdateClashMode(string? newMode)
    {
    }

    public void WriteConnectionEvents(ConnectionEvents? events)
    {
    }

    public void WriteGroups(IOutboundGroupIterator? message)
    {
        if (message is null)
        {
            return;
        }
        string groupTag;
        HashSet<string> expected;
        lock (urlTestLock)
        {
            if (urlTestCompletion is null)
            {
                return;
            }
            groupTag = urlTestGroupTag;
            expected = new HashSet<string>(urlTestExpectedTags, StringComparer.OrdinalIgnoreCase);
        }

        try
        {
            while (message.HasNext)
            {
                var group = message.Next();
                if (group is null || !string.Equals(group.Tag, groupTag, StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                var items = group.Items;
                if (items is null)
                {
                    continue;
                }
                var latest = new Dictionary<string, CudyUrlTestItem>(StringComparer.OrdinalIgnoreCase);
                while (items.HasNext)
                {
                    var item = items.Next();
                    if (item?.Tag is null || !expected.Contains(item.Tag))
                    {
                        continue;
                    }
                    var status = item.URLTestDelay > 0 ? "ok" : "pending";
                    latest[item.Tag] = new CudyUrlTestItem(item.Tag, item.URLTestDelay, item.URLTestTime, status);
                }
                if (latest.Count == 0)
                {
                    continue;
                }
                TaskCompletionSource<CudyUrlTestResult>? completion = null;
                CudyUrlTestResult? result = null;
                lock (urlTestLock)
                {
                    foreach (var pair in latest)
                    {
                        urlTestLatest[pair.Key] = pair.Value;
                    }
                    var complete = expected.All(tag =>
                        urlTestLatest.TryGetValue(tag, out var item) && item.DelayMs > 0);
                    if (complete && urlTestCompletion is not null)
                    {
                        completion = urlTestCompletion;
                        result = new CudyUrlTestResult(
                            expected.Select(tag => urlTestLatest[tag]).ToList(),
                            Complete: true,
                            Error: null);
                        ClearUrlTestState();
                    }
                }
                completion?.TrySetResult(result!);
            }
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "libbox urltest group parse failed: " + ex.Message);
        }
    }

    public void WriteLogs(ILogIterator? messageList)
    {
    }

    public void WriteOutbounds(IOutboundGroupItemIterator? message)
    {
        if (message is null)
        {
            return;
        }
        HashSet<string> expected;
        lock (urlTestLock)
        {
            if (urlTestCompletion is null)
            {
                return;
            }
            expected = new HashSet<string>(urlTestExpectedTags, StringComparer.OrdinalIgnoreCase);
        }

        try
        {
            var latest = new Dictionary<string, CudyUrlTestItem>(StringComparer.OrdinalIgnoreCase);
            while (message.HasNext)
            {
                var item = message.Next();
                if (item?.Tag is null || !expected.Contains(item.Tag))
                {
                    continue;
                }
                var status = item.URLTestDelay > 0 ? "ok" : "pending";
                latest[item.Tag] = new CudyUrlTestItem(item.Tag, item.URLTestDelay, item.URLTestTime, status);
            }
            CompleteUrlTestIfReady(expected, latest);
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "libbox urltest outbounds parse failed: " + ex.Message);
        }
    }

    public void WriteStatus(StatusMessage? message)
    {
    }

    public async Task<CudyUrlTestResult> RunUrlTestAsync(
        CommandClient client,
        string groupTag,
        IReadOnlyList<string> outboundTags,
        int maxRuntimeSeconds,
        CancellationToken cancellationToken)
    {
        var expected = outboundTags
            .Where(item => !string.IsNullOrWhiteSpace(item))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToList();
        if (expected.Count == 0)
        {
            return new CudyUrlTestResult(Array.Empty<CudyUrlTestItem>(), Complete: false, Error: "no outbound tags");
        }

        TaskCompletionSource<CudyUrlTestResult> completion;
        lock (urlTestLock)
        {
            completion = new TaskCompletionSource<CudyUrlTestResult>(TaskCreationOptions.RunContinuationsAsynchronously);
            urlTestCompletion = completion;
            urlTestGroupTag = groupTag;
            urlTestExpectedTags = new HashSet<string>(expected, StringComparer.OrdinalIgnoreCase);
            urlTestLatest = new Dictionary<string, CudyUrlTestItem>(StringComparer.OrdinalIgnoreCase);
        }

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(maxRuntimeSeconds));
        using var registration = timeout.Token.Register(() =>
        {
            TaskCompletionSource<CudyUrlTestResult>? activeCompletion = null;
            CudyUrlTestResult result;
            lock (urlTestLock)
            {
                activeCompletion = urlTestCompletion;
                var latest = expected
                    .Select(tag => urlTestLatest.TryGetValue(tag, out var item)
                        ? item
                        : new CudyUrlTestItem(tag, 0, 0, "timeout"))
                    .ToList();
                result = new CudyUrlTestResult(latest, Complete: false, Error: "urltest timed out");
                ClearUrlTestState();
            }
            activeCompletion?.TrySetResult(result);
        });

        try
        {
            client.UrlTest(groupTag);
            return await completion.Task;
        }
        catch
        {
            lock (urlTestLock)
            {
                ClearUrlTestState();
            }
            throw;
        }
    }

    private void ClearUrlTestState()
    {
        urlTestCompletion = null;
        urlTestGroupTag = "";
        urlTestExpectedTags = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        urlTestLatest = new Dictionary<string, CudyUrlTestItem>(StringComparer.OrdinalIgnoreCase);
    }

    private void CompleteUrlTestIfReady(
        HashSet<string> expected,
        Dictionary<string, CudyUrlTestItem> latest)
    {
        if (latest.Count == 0)
        {
            return;
        }
        TaskCompletionSource<CudyUrlTestResult>? completion = null;
        CudyUrlTestResult? result = null;
        lock (urlTestLock)
        {
            foreach (var pair in latest)
            {
                urlTestLatest[pair.Key] = pair.Value;
            }
            var complete = expected.All(tag =>
                urlTestLatest.TryGetValue(tag, out var item) && item.DelayMs > 0);
            if (complete && urlTestCompletion is not null)
            {
                completion = urlTestCompletion;
                result = new CudyUrlTestResult(
                    expected.Select(tag => urlTestLatest[tag]).ToList(),
                    Complete: true,
                    Error: null);
                ClearUrlTestState();
            }
        }
        completion?.TrySetResult(result!);
    }
}

public sealed class CudyLibboxCommandHandler : Java.Lang.Object, ICommandServerHandler
{
    private const string LogTag = "CudyAgent";
    private readonly CudyVpnService service;

    public CudyLibboxCommandHandler(CudyVpnService service)
    {
        this.service = service;
    }

    public SystemProxyStatus? SystemProxyStatus => new()
    {
        Available = false,
        Enabled = false,
    };

    public int ConnectSSHAgent() => -1;

    public void ServiceReload()
    {
        Log.Info(LogTag, "libbox requested service reload");
    }

    public void ServiceStop()
    {
        Log.Info(LogTag, "libbox requested service stop");
        service.RequestLibboxServiceStop();
    }

    public void SetSystemProxyEnabled(bool enabled)
    {
        Log.Info(LogTag, "libbox system proxy request ignored: " + enabled);
    }

    public void TriggerNativeCrash()
    {
        Log.Warn(LogTag, "libbox native crash request ignored");
    }

    public void WriteDebugMessage(string? message)
    {
        if (!string.IsNullOrWhiteSpace(message))
        {
            Log.Debug(LogTag, "libbox: " + message);
        }
    }
}

public sealed class CudyLibboxPlatform : Java.Lang.Object, IPlatformInterface
{
    private const string LogTag = "CudyAgent";
    private readonly CudyVpnService service;
    private readonly object defaultInterfaceLock = new();
    private IInterfaceUpdateListener? defaultInterfaceListener;
    private ConnectivityManager.NetworkCallback? defaultInterfaceCallback;
    private string defaultInterfaceSignature = "";

    public CudyLibboxPlatform(CudyVpnService service)
    {
        this.service = service;
    }

    public INetworkInterfaceIterator? Interfaces => BuildNetworkInterfaces();

    public void AutoDetectInterfaceControl(int fd)
    {
        service.ProtectLibboxSocket(fd);
    }

    public void CheckPlatformShell()
    {
        throw new NotSupportedException("Platform shell is disabled.");
    }

    public void ClearDNSCache()
    {
    }

    public void CloseDefaultInterfaceMonitor(IInterfaceUpdateListener? listener)
    {
        if (listener is null)
        {
            return;
        }

        ConnectivityManager.NetworkCallback? callback = null;
        lock (defaultInterfaceLock)
        {
            if (!ReferenceEquals(defaultInterfaceListener, listener)
                && defaultInterfaceListener?.Equals(listener) != true)
            {
                return;
            }
            defaultInterfaceListener = null;
            defaultInterfaceSignature = "";
            callback = defaultInterfaceCallback;
            defaultInterfaceCallback = null;
        }

        if (callback is null)
        {
            return;
        }

        try
        {
            GetConnectivityManager()?.UnregisterNetworkCallback(callback);
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "default interface monitor close failed: " + ex.Message);
        }
    }

    public void CloseAllDefaultInterfaceMonitors()
    {
        ConnectivityManager.NetworkCallback? callback;
        lock (defaultInterfaceLock)
        {
            defaultInterfaceListener = null;
            defaultInterfaceSignature = "";
            callback = defaultInterfaceCallback;
            defaultInterfaceCallback = null;
        }

        var connectivityManager = GetConnectivityManager();
        if (connectivityManager is null || callback is null)
        {
            return;
        }
        try
        {
            connectivityManager.UnregisterNetworkCallback(callback);
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "default interface monitor cleanup failed: " + ex.Message);
        }
    }

    public void CloseNeighborMonitor(INeighborUpdateListener? listener)
    {
    }

    public ConnectionOwner? FindConnectionOwner(
        int ipProtocol,
        string? sourceAddress,
        int sourcePort,
        string? destinationAddress,
        int destinationPort)
    {
        var owner = new ConnectionOwner
        {
            UserId = 0,
            UserName = "",
            ProcessPath = "",
        };
        owner.SetAndroidPackageNames(new CudyStringIterator(Array.Empty<string>()));
        return owner;
    }

    public bool IncludeAllNetworks() => false;

    public ILocalDNSTransport? LocalDNSTransport() => null;

    public string? LookupSFTPServer() => null;

    public PlatformUser? LookupUser(string? username) => null;

    public IShellSession? OpenShellSession(
        PlatformUser? user,
        string? command,
        IStringIterator? environ,
        string? term,
        int rows,
        int cols) => null;

    public int OpenTun(ITunOptions? options)
    {
        return service.OpenLibboxTun(options);
    }

    public string? ReadSystemSSHHostKey() => null;

    public WIFIState? ReadWIFIState() => new("", "");

    public void RegisterMyInterface(string? name)
    {
    }

    public void SendNotification(IO.Nekohasekai.Libbox.Notification? notification)
    {
    }

    public void StartDefaultInterfaceMonitor(IInterfaceUpdateListener? listener)
    {
        if (listener is null)
        {
            return;
        }

        var connectivityManager = GetConnectivityManager();
        if (connectivityManager is null)
        {
            Log.Warn(LogTag, "ConnectivityManager unavailable; libbox default interface monitor disabled.");
            return;
        }

        ConnectivityManager.NetworkCallback? callbackToRegister = null;
        lock (defaultInterfaceLock)
        {
            defaultInterfaceListener = listener;
            defaultInterfaceSignature = "";
            if (defaultInterfaceCallback is null)
            {
                defaultInterfaceCallback = new CudyDefaultNetworkCallback(this);
                callbackToRegister = defaultInterfaceCallback;
            }
        }

        UpdateDefaultInterface();
        if (callbackToRegister is null)
        {
            return;
        }
        try
        {
            connectivityManager.RegisterDefaultNetworkCallback(callbackToRegister);
        }
        catch (Exception ex)
        {
            lock (defaultInterfaceLock)
            {
                if (ReferenceEquals(defaultInterfaceCallback, callbackToRegister))
                {
                    defaultInterfaceCallback = null;
                }
            }
            Log.Warn(LogTag, "default interface monitor start failed: " + ex.Message);
        }
    }

    public void StartNeighborMonitor(INeighborUpdateListener? listener)
    {
    }

    public IStringIterator? SystemCertificates() => new CudyStringIterator(Array.Empty<string>());

    public string? TailscaleHostname() => $"{Build.Manufacturer} {Build.Model}";

    public bool UnderNetworkExtension() => false;

    public bool UsePlatformAutoDetectInterfaceControl() => true;

    public bool UsePlatformShell() => false;

    public bool UseProcFS() => (int)Build.VERSION.SdkInt < 29;

    private ConnectivityManager? GetConnectivityManager()
    {
        return service.GetSystemService(Context.ConnectivityService) as ConnectivityManager;
    }

    private void UpdateDefaultInterface()
    {
        try
        {
            IInterfaceUpdateListener? listener;
            lock (defaultInterfaceLock)
            {
                listener = defaultInterfaceListener;
            }
            if (listener is null)
            {
                return;
            }

            var connectivityManager = GetConnectivityManager();
            if (connectivityManager is null)
            {
                Log.Warn(LogTag, "libbox default interface update skipped: ConnectivityManager unavailable.");
                return;
            }

            var activeNetwork = FindPhysicalNetwork(connectivityManager);
            if (activeNetwork is null)
            {
                Log.Warn(LogTag, "libbox default interface update skipped: physical network unavailable.");
                return;
            }

            UpdateDefaultInterface(listener, connectivityManager, activeNetwork);
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "libbox default interface update failed: " + ex.Message);
        }
    }

    private void UpdateDefaultInterface(
        IInterfaceUpdateListener listener,
        ConnectivityManager connectivityManager,
        Network network)
    {
        var linkProperties = connectivityManager.GetLinkProperties(network);
        var interfaceName = linkProperties?.InterfaceName;
        if (string.IsNullOrWhiteSpace(interfaceName))
        {
            Log.Warn(LogTag, "libbox default interface update skipped: interface name unavailable.");
            return;
        }

        if (IsVpnInterfaceName(interfaceName))
        {
            Log.Info(LogTag, "libbox default interface update ignored VPN interface: " + interfaceName);
            return;
        }

        var capabilities = connectivityManager.GetNetworkCapabilities(network);
        var isExpensive = capabilities?.HasCapability(NetCapability.NotMetered) == false;
        var isConstrained = capabilities?.HasCapability(NetCapability.NotRestricted) == false;
        var index = GetInterfaceIndex(interfaceName);
        var signature = $"{interfaceName}|{index}|{isExpensive}|{isConstrained}";
        lock (defaultInterfaceLock)
        {
            if (!ReferenceEquals(defaultInterfaceListener, listener)
                && defaultInterfaceListener?.Equals(listener) != true)
            {
                return;
            }
            if (string.Equals(defaultInterfaceSignature, signature, StringComparison.Ordinal))
            {
                return;
            }
            defaultInterfaceSignature = signature;
        }
        listener.UpdateDefaultInterface(interfaceName, index, isExpensive, isConstrained);
        Log.Info(LogTag, $"libbox default interface: name={interfaceName} index={index} expensive={isExpensive} constrained={isConstrained}");
    }

    private INetworkInterfaceIterator BuildNetworkInterfaces()
    {
        var items = new List<LibboxNetworkInterface>();
        var connectivityManager = GetConnectivityManager();
        if (connectivityManager is null)
        {
            return new CudyNetworkInterfaceIterator(items);
        }

        foreach (var network in connectivityManager.GetAllNetworks() ?? Array.Empty<Network>())
        {
            try
            {
                var linkProperties = connectivityManager.GetLinkProperties(network);
                var capabilities = connectivityManager.GetNetworkCapabilities(network);
                var interfaceName = linkProperties?.InterfaceName;
                if (linkProperties is null || capabilities is null || string.IsNullOrWhiteSpace(interfaceName))
                {
                    continue;
                }

                var javaInterface = Java.Net.NetworkInterface.GetByName(interfaceName);
                if (javaInterface is null)
                {
                    continue;
                }

                var addresses = javaInterface.InterfaceAddresses?
                    .Select(FormatInterfaceAddress)
                    .Where(value => !string.IsNullOrWhiteSpace(value))
                    .ToArray() ?? Array.Empty<string>();
                var dnsServers = linkProperties.DnsServers?
                    .Select(address => address.HostAddress ?? "")
                    .Where(value => !string.IsNullOrWhiteSpace(value))
                    .ToArray() ?? Array.Empty<string>();

                var flags = 0;
                if (capabilities.HasCapability(NetCapability.Internet))
                {
                    flags |= 0x1 | 0x40; // IFF_UP | IFF_RUNNING
                }
                if (javaInterface.IsLoopback)
                {
                    flags |= 0x8; // IFF_LOOPBACK
                }
                if (javaInterface.IsPointToPoint)
                {
                    flags |= 0x10; // IFF_POINTOPOINT
                }
                if (javaInterface.SupportsMulticast())
                {
                    flags |= 0x1000; // IFF_MULTICAST
                }

                items.Add(new LibboxNetworkInterface
                {
                    Name = interfaceName,
                    Index = javaInterface.Index,
                    MTU = javaInterface.MTU,
                    Addresses = new CudyStringIterator(addresses),
                    DNSServer = new CudyStringIterator(dnsServers),
                    Type = InterfaceType(capabilities),
                    Flags = flags,
                    Metered = !capabilities.HasCapability(NetCapability.NotMetered),
                });
            }
            catch (Exception ex)
            {
                Log.Warn(LogTag, "libbox interface inventory item failed: " + ex.Message);
            }
        }
        return new CudyNetworkInterfaceIterator(items);
    }

    private static int InterfaceType(NetworkCapabilities capabilities)
    {
        if (capabilities.HasTransport(TransportType.Wifi))
        {
            return Libbox.InterfaceTypeWIFI;
        }
        if (capabilities.HasTransport(TransportType.Cellular))
        {
            return Libbox.InterfaceTypeCellular;
        }
        if (capabilities.HasTransport(TransportType.Ethernet))
        {
            return Libbox.InterfaceTypeEthernet;
        }
        return Libbox.InterfaceTypeOther;
    }

    private static string FormatInterfaceAddress(Java.Net.InterfaceAddress item)
    {
        var host = item.Address?.HostAddress ?? "";
        var scope = host.IndexOf('%');
        if (scope >= 0)
        {
            host = host[..scope];
        }
        return string.IsNullOrWhiteSpace(host) ? "" : $"{host}/{item.NetworkPrefixLength}";
    }

    private static int GetInterfaceIndex(string interfaceName)
    {
        try
        {
            return Java.Net.NetworkInterface.GetByName(interfaceName)?.Index ?? 0;
        }
        catch
        {
            return 0;
        }
    }

    private static Network? FindPhysicalNetwork(ConnectivityManager connectivityManager)
    {
        var active = connectivityManager.ActiveNetwork;
        if (IsPhysicalNetwork(connectivityManager, active))
        {
            return active;
        }

        foreach (var network in connectivityManager.GetAllNetworks() ?? Array.Empty<Network>())
        {
            if (IsPhysicalNetwork(connectivityManager, network))
            {
                return network;
            }
        }

        return active;
    }

    private static bool IsPhysicalNetwork(ConnectivityManager connectivityManager, Network? network)
    {
        if (network is null)
        {
            return false;
        }

        var linkProperties = connectivityManager.GetLinkProperties(network);
        var interfaceName = linkProperties?.InterfaceName;
        if (string.IsNullOrWhiteSpace(interfaceName) || IsVpnInterfaceName(interfaceName))
        {
            return false;
        }

        var capabilities = connectivityManager.GetNetworkCapabilities(network);
        if (capabilities is null || capabilities.HasTransport(TransportType.Vpn))
        {
            return false;
        }

        return capabilities.HasCapability(NetCapability.Internet)
            || capabilities.HasTransport(TransportType.Wifi)
            || capabilities.HasTransport(TransportType.Cellular)
            || capabilities.HasTransport(TransportType.Ethernet);
    }

    private static bool IsVpnInterfaceName(string interfaceName)
    {
        return interfaceName.StartsWith("tun", StringComparison.OrdinalIgnoreCase)
            || interfaceName.StartsWith("cudy", StringComparison.OrdinalIgnoreCase)
            || interfaceName.StartsWith("wg", StringComparison.OrdinalIgnoreCase);
    }

    private sealed class CudyDefaultNetworkCallback : ConnectivityManager.NetworkCallback
    {
        private readonly CudyLibboxPlatform platform;

        public CudyDefaultNetworkCallback(CudyLibboxPlatform platform)
        {
            this.platform = platform;
        }

        public override void OnAvailable(Network network)
        {
            platform.UpdateDefaultInterface();
        }

        public override void OnCapabilitiesChanged(Network network, NetworkCapabilities networkCapabilities)
        {
            platform.UpdateDefaultInterface();
        }

        public override void OnLinkPropertiesChanged(Network network, LinkProperties linkProperties)
        {
            platform.UpdateDefaultInterface();
        }

        public override void OnLost(Network network)
        {
            platform.UpdateDefaultInterface();
        }
    }
}

public sealed class CudyNetworkInterfaceIterator : Java.Lang.Object, INetworkInterfaceIterator
{
    private readonly IReadOnlyList<LibboxNetworkInterface> values;
    private int index;

    public CudyNetworkInterfaceIterator(IReadOnlyList<LibboxNetworkInterface> values)
    {
        this.values = values;
    }

    public bool HasNext => index < values.Count;

    public LibboxNetworkInterface? Next() => HasNext ? values[index++] : null;
}

public sealed class CudyStringIterator : Java.Lang.Object, IStringIterator
{
    private readonly IReadOnlyList<string> values;
    private int index;

    public CudyStringIterator(IReadOnlyList<string> values)
    {
        this.values = values;
    }

    public bool HasNext => index < values.Count;

    public int Len() => values.Count;

    public string? Next()
    {
        return HasNext ? values[index++] : null;
    }
}
