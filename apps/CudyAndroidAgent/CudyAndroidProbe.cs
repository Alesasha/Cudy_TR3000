using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Android.Util;

namespace CudyAndroidAgent;

public sealed class CudyAndroidProbeRunner
{
    private const string LogTag = "CudyAgent";
    private static readonly Regex IpBodyPattern = new(@"^\s*([0-9a-fA-F:.]+)\s*$", RegexOptions.Compiled);

    private readonly CudyVpnService service;
    private readonly CudyAndroidLibboxEngine engine;
    private readonly string deviceId;

    public CudyAndroidProbeRunner(CudyVpnService service, CudyAndroidLibboxEngine engine, string deviceId)
    {
        this.service = service;
        this.engine = engine;
        this.deviceId = deviceId;
    }

    public async Task<CudyProbeJobsSummary> RunAsync(
        JsonElement policyRoot,
        CudyTransportPlan transportPlan,
        CudyStoredTransport activeConfig,
        Func<string, CancellationToken, Task<string>> getControlStringAsync,
        Func<string, string, CancellationToken, Task> postControlJsonAsync,
        CancellationToken cancellationToken)
    {
        var jobsJson = await getControlStringAsync(
            "/api/agent/probe-jobs?limit=2",
            cancellationToken);
        using var jobsDoc = JsonDocument.Parse(jobsJson);
        var jobs = jobsDoc.RootElement.TryGetProperty("jobs", out var jobsValue) && jobsValue.ValueKind == JsonValueKind.Array
            ? jobsValue.EnumerateArray().Select(item => item.Clone()).ToList()
            : new List<JsonElement>();
        Log.Info(LogTag, $"Probe jobs claimed: {jobs.Count}");
        var completed = 0;
        var failed = 0;
        foreach (var job in jobs)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var jobId = GetString(job, "id") ?? "";
            if (string.IsNullOrWhiteSpace(jobId))
            {
                continue;
            }
            try
            {
                Log.Info(LogTag, $"Probe job start: {jobId}");
                var result = await RunJobAsync(policyRoot, transportPlan, job, cancellationToken);
                Log.Info(LogTag, $"Probe job result built: {jobId}");
                await postControlJsonAsync(
                    "/api/agent/probe-jobs/result",
                    JsonSerializer.Serialize(new { job_id = jobId, result }),
                    cancellationToken);
                Log.Info(LogTag, $"Probe job posted: {jobId}");
                completed++;
            }
            catch (Exception ex)
            {
                Log.Warn(LogTag, $"Probe job failed: {jobId}: {ex.Message}");
                failed++;
                var result = new
                {
                    schema_version = 1,
                    agent_version = "0.1",
                    platform = "android",
                    device_id = deviceId,
                    domain = GetString(job, "domain") ?? "",
                    url = ProbeUrl(job),
                    candidate_server_ids = CandidateIds(job),
                    winner = (object?)null,
                    checks = new[] { new { status = "failed", error = ex.Message } },
                    ok = false,
                };
                await postControlJsonAsync(
                    "/api/agent/probe-jobs/result",
                    JsonSerializer.Serialize(new { job_id = jobId, result }),
                    cancellationToken);
            }
            finally
            {
                engine.StartOrReload(activeConfig);
            }
        }
        return new CudyProbeJobsSummary(jobs.Count, completed, failed);
    }

    public async Task<object> RunDebugAsync(
        JsonElement policyRoot,
        CudyTransportPlan transportPlan,
        string url,
        IReadOnlyList<string> candidates,
        CancellationToken cancellationToken)
    {
        var domain = "";
        if (Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            domain = uri.Host;
        }

        var jobJson = JsonSerializer.Serialize(new
        {
            id = "debug-local",
            domain,
            url,
            candidate_server_ids = candidates,
            connect_timeout = 5,
            max_time = 20,
        });
        using var doc = JsonDocument.Parse(jobJson);
        return await RunJobAsync(policyRoot, transportPlan, doc.RootElement, cancellationToken);
    }

    private async Task<object> RunJobAsync(
        JsonElement policyRoot,
        CudyTransportPlan transportPlan,
        JsonElement job,
        CancellationToken cancellationToken)
    {
        var domain = (GetString(job, "domain") ?? "").Trim().ToLowerInvariant();
        var url = ProbeUrl(job);
        var candidates = CandidateIds(job);
        var connectTimeout = PositiveInt(job, "connect_timeout", 5);
        var maxTime = PositiveInt(job, "max_time", 12);
        var probeCidrs = ResolveProbeCidrs(url, domain);
        var checks = new List<Dictionary<string, object?>>();
        Dictionary<string, object?>? winner = null;
        var runnableCandidates = candidates
            .Where(serverId => transportPlan.Find(serverId) is not null)
            .ToList();
        var probePorts = runnableCandidates
            .Select((serverId, index) => new CudySingBoxLocalProbe(serverId, 19080 + index))
            .ToList();
        var probePortByServer = probePorts.ToDictionary(item => item.ServerId, item => item.ListenPort, StringComparer.OrdinalIgnoreCase);
        if (runnableCandidates.Count > 0)
        {
            var probeConfig = CudySingBoxConfig.BuildAndroidUnified(
                policyRoot,
                transportPlan,
                localProbes: probePorts);
            var stored = service.WriteTemporaryTransport(probeConfig);
            engine.StartOrReload(stored);
            await Task.Delay(500, cancellationToken);
        }

        for (var index = 0; index < candidates.Count; index++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var serverId = candidates[index];
            Log.Info(LogTag, $"Probe candidate start: job_domain={domain} server={serverId}");
            var check = new Dictionary<string, object?>
            {
                ["server_id"] = serverId,
                ["index"] = index + 1,
                ["interface"] = "cudy0",
                ["ok"] = false,
                ["probe_cidrs"] = probeCidrs,
            };
            if (transportPlan.Find(serverId) is null)
            {
                check["status"] = "no_transport";
                checks.Add(check);
                continue;
            }
            if (probeCidrs.Count == 0)
            {
                check["resolve_status"] = "resolve_failed";
            }

            if (probePortByServer.TryGetValue(serverId, out var proxyPort))
            {
                var probe = Uri.TryCreate(url, UriKind.Absolute, out var probeUri)
                    && string.Equals(probeUri.Scheme, "tcp", StringComparison.OrdinalIgnoreCase)
                    ? await TcpProbeAsync(probeUri, connectTimeout, maxTime, proxyPort, cancellationToken)
                    : await HttpProbeAsync(url, connectTimeout, maxTime, proxyPort, cancellationToken);
                foreach (var pair in probe)
                {
                    check[pair.Key] = pair.Value;
                }
                Log.Info(LogTag, $"Probe candidate done: server={serverId} ok={check["ok"]} ms={check["time_total_ms"]}");
            }
            else
            {
                check["probe_type"] = "local_mixed_proxy";
                check["time_total_ms"] = 0;
                check["error"] = "no local probe port";
            }
            check["status"] = check["ok"] is true ? "ok" : "failed";
            checks.Add(check);
            if (check["ok"] is true
                && (winner is null || Convert.ToInt32(check["time_total_ms"]) < Convert.ToInt32(winner["time_total_ms"])))
            {
                winner = check;
            }
        }

        return new
        {
            schema_version = 1,
            agent_version = "0.1",
            platform = "android",
            device_id = deviceId,
            probe_engine = "local_mixed_proxy",
            domain,
            url,
            candidate_server_ids = candidates,
            winner,
            checks,
            ok = winner is not null,
        };
    }

    private static async Task<Dictionary<string, object?>> HttpProbeAsync(
        string url,
        int connectTimeout,
        int maxTime,
        int proxyPort,
        CancellationToken cancellationToken)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(TimeSpan.FromSeconds(maxTime));
        using var handler = new SocketsHttpHandler
        {
            ConnectTimeout = TimeSpan.FromSeconds(connectTimeout),
            PooledConnectionLifetime = TimeSpan.Zero,
            UseProxy = true,
            Proxy = new WebProxy($"http://127.0.0.1:{proxyPort}"),
        };
        using var client = new HttpClient(handler)
        {
            Timeout = TimeSpan.FromSeconds(maxTime),
        };
        client.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("CudyAndroidAgent", "0.1"));
        var stopwatch = Stopwatch.StartNew();
        try
        {
            using var reply = await client.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, cts.Token);
            var bytes = 0;
            var egressIp = "";
            await using var stream = await reply.Content.ReadAsStreamAsync(cts.Token);
            var buffer = new byte[16384];
            var prefixBytes = new List<byte>();
            const int maxProbeBytes = 1024 * 1024;
            while (bytes < maxProbeBytes)
            {
                var read = await stream.ReadAsync(buffer.AsMemory(0, Math.Min(buffer.Length, maxProbeBytes - bytes)), cts.Token);
                if (read <= 0)
                {
                    break;
                }
                if (prefixBytes.Count < 256)
                {
                    prefixBytes.AddRange(buffer.Take(Math.Min(read, 256 - prefixBytes.Count)));
                }
                bytes += read;
            }
            if (prefixBytes.Count > 0)
            {
                var bodyPrefix = System.Text.Encoding.UTF8.GetString(prefixBytes.ToArray());
                var ipMatch = IpBodyPattern.Match(bodyPrefix);
                if (ipMatch.Success && IPAddress.TryParse(ipMatch.Groups[1].Value, out var parsedIp))
                {
                    egressIp = parsedIp.ToString();
                }
            }
            stopwatch.Stop();
            var elapsedSeconds = Math.Max(0.001, stopwatch.Elapsed.TotalSeconds);
            var speedMbps = Math.Round(bytes * 8.0 / elapsedSeconds / 1_000_000, 2);
            return new Dictionary<string, object?>
            {
                ["ok"] = ((int)reply.StatusCode) >= 200 && ((int)reply.StatusCode) < 400,
                ["probe_type"] = "local_mixed_proxy",
                ["local_proxy_port"] = proxyPort,
                ["http_code"] = (int)reply.StatusCode,
                ["time_total_ms"] = (int)Math.Round(stopwatch.Elapsed.TotalMilliseconds),
                ["bytes"] = bytes,
                ["speed_mbps"] = speedMbps,
                ["egress_ip"] = egressIp,
            };
        }
        catch (Exception ex) when (ex is not OperationCanceledException || !cancellationToken.IsCancellationRequested)
        {
            stopwatch.Stop();
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["probe_type"] = "local_mixed_proxy",
                ["local_proxy_port"] = proxyPort,
                ["http_code"] = null,
                ["time_total_ms"] = (int)Math.Round(stopwatch.Elapsed.TotalMilliseconds),
                ["error"] = ex.Message,
            };
        }
    }

    private static async Task<Dictionary<string, object?>> TcpProbeAsync(
        Uri target,
        int connectTimeout,
        int maxTime,
        int proxyPort,
        CancellationToken cancellationToken)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(TimeSpan.FromSeconds(maxTime));
        var stopwatch = Stopwatch.StartNew();
        try
        {
            using var proxy = new TcpClient();
            using (var connectCts = CancellationTokenSource.CreateLinkedTokenSource(cts.Token))
            {
                connectCts.CancelAfter(TimeSpan.FromSeconds(connectTimeout));
                await proxy.ConnectAsync(IPAddress.Loopback, proxyPort, connectCts.Token);
            }

            var targetPort = target.Port > 0 ? target.Port : 443;
            var authority = $"{target.Host}:{targetPort}";
            var request = Encoding.ASCII.GetBytes(
                $"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nProxy-Connection: close\r\n\r\n");
            await using var stream = proxy.GetStream();
            await stream.WriteAsync(request, cts.Token);
            await stream.FlushAsync(cts.Token);

            var response = new List<byte>();
            var buffer = new byte[512];
            while (response.Count < 8192)
            {
                var read = await stream.ReadAsync(buffer, cts.Token);
                if (read <= 0)
                {
                    break;
                }
                response.AddRange(buffer.Take(read));
                if (Encoding.ASCII.GetString(response.ToArray()).Contains("\r\n\r\n", StringComparison.Ordinal))
                {
                    break;
                }
            }

            stopwatch.Stop();
            var header = Encoding.ASCII.GetString(response.ToArray());
            var firstLine = header.Split(new[] { "\r\n" }, StringSplitOptions.None).FirstOrDefault() ?? "";
            var ok = firstLine.StartsWith("HTTP/1.1 200", StringComparison.OrdinalIgnoreCase)
                || firstLine.StartsWith("HTTP/1.0 200", StringComparison.OrdinalIgnoreCase);
            return new Dictionary<string, object?>
            {
                ["ok"] = ok,
                ["probe_type"] = "tcp_via_local_mixed_proxy",
                ["local_proxy_port"] = proxyPort,
                ["remote_ip"] = target.Host,
                ["remote_port"] = targetPort,
                ["time_total_ms"] = (int)Math.Round(stopwatch.Elapsed.TotalMilliseconds),
                ["proxy_response"] = firstLine,
                ["error"] = ok ? "" : (string.IsNullOrWhiteSpace(firstLine) ? "empty proxy response" : firstLine),
            };
        }
        catch (Exception ex) when (ex is not OperationCanceledException || !cancellationToken.IsCancellationRequested)
        {
            stopwatch.Stop();
            return new Dictionary<string, object?>
            {
                ["ok"] = false,
                ["probe_type"] = "tcp_via_local_mixed_proxy",
                ["local_proxy_port"] = proxyPort,
                ["remote_ip"] = target.Host,
                ["remote_port"] = target.Port > 0 ? target.Port : 443,
                ["time_total_ms"] = (int)Math.Round(stopwatch.Elapsed.TotalMilliseconds),
                ["error"] = ex.Message,
            };
        }
    }

    private static IReadOnlyList<string> ResolveProbeCidrs(string url, string domain)
    {
        var host = "";
        if (Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            host = uri.Host;
        }
        if (string.IsNullOrWhiteSpace(host))
        {
            host = domain;
        }
        if (string.IsNullOrWhiteSpace(host))
        {
            return Array.Empty<string>();
        }
        try
        {
            return Dns.GetHostAddresses(host)
                .Where(address => address.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork)
                .Select(address => address + "/32")
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToList();
        }
        catch
        {
            return Array.Empty<string>();
        }
    }

    private static string ProbeUrl(JsonElement job)
    {
        var explicitUrl = GetString(job, "url");
        if (!string.IsNullOrWhiteSpace(explicitUrl))
        {
            return explicitUrl;
        }
        var domain = GetString(job, "domain") ?? "";
        return string.IsNullOrWhiteSpace(domain) ? "https://ifconfig.me/ip" : $"https://{domain}/";
    }

    private static IReadOnlyList<string> CandidateIds(JsonElement job)
    {
        if (!job.TryGetProperty("candidate_server_ids", out var candidates) || candidates.ValueKind != JsonValueKind.Array)
        {
            return Array.Empty<string>();
        }
        return candidates.EnumerateArray()
            .Select(item => item.ValueKind == JsonValueKind.String ? item.GetString() : "")
            .Where(item => !string.IsNullOrWhiteSpace(item))
            .Select(item => item!)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToList();
    }

    private static int PositiveInt(JsonElement item, string propertyName, int fallback)
    {
        if (item.TryGetProperty(propertyName, out var value)
            && value.ValueKind == JsonValueKind.Number
            && value.TryGetInt32(out var result)
            && result > 0)
        {
            return result;
        }
        return fallback;
    }

    private static string? GetString(JsonElement item, string propertyName)
    {
        return item.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;
    }

    private static string OutboundTag(string serverId)
    {
        var safe = new string(serverId.Select(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_' ? ch : '-').ToArray());
        return "out-" + (string.IsNullOrWhiteSpace(safe) ? "server" : safe);
    }
}

public sealed record CudyProbeJobsSummary(int Jobs, int Completed, int Failed)
{
    public string SafeSummary() => $"probe_jobs jobs={Jobs} completed={Completed} failed={Failed}";
}
