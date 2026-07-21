namespace CudyAndroidAgent;

using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

internal sealed record CudyCriticalService(
    string Key,
    string Label,
    IReadOnlyList<string> Targets,
    string SuccessPattern,
    string FailurePattern);

internal sealed record CudyCriticalTargetResult(
    string Url,
    int HttpCode,
    bool SuccessMatched,
    bool FailureMatched,
    bool Ok,
    string Error);

internal sealed record CudyCriticalServiceResult(
    string Key,
    string Label,
    bool Ok,
    IReadOnlyList<CudyCriticalTargetResult> Targets);

internal sealed record CudyCriticalCheckResult(IReadOnlyList<CudyCriticalServiceResult> Services)
{
    public bool Ok => Services.All(item => item.Ok);
    public IReadOnlyList<string> FailedServices => Services
        .Where(item => !item.Ok)
        .Select(item => item.Label)
        .ToList();
}

internal static class CudyCriticalServiceMonitor
{
    private const int MaxBodyBytes = 262_144;
    private static readonly TimeSpan RegexTimeout = TimeSpan.FromSeconds(1);

    public static IReadOnlyList<CudyCriticalService> Parse(JsonElement policyRoot)
    {
        var services = new List<CudyCriticalService>();
        if (!policyRoot.TryGetProperty("critical_services", out var items)
            || items.ValueKind != JsonValueKind.Array)
        {
            return services;
        }

        foreach (var item in items.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object
                || (item.TryGetProperty("enabled", out var enabled)
                    && enabled.ValueKind == JsonValueKind.False))
            {
                continue;
            }
            var key = GetString(item, "service_key");
            var label = GetString(item, "label");
            var targets = new List<string>();
            if (item.TryGetProperty("targets", out var targetItems)
                && targetItems.ValueKind == JsonValueKind.Array)
            {
                foreach (var target in targetItems.EnumerateArray())
                {
                    var value = target.ValueKind == JsonValueKind.String ? target.GetString() ?? "" : "";
                    if (Uri.TryCreate(value, UriKind.Absolute, out var uri)
                        && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps))
                    {
                        targets.Add(uri.AbsoluteUri);
                    }
                }
            }
            if (targets.Count == 0)
            {
                continue;
            }
            services.Add(new CudyCriticalService(
                string.IsNullOrWhiteSpace(key) ? label : key,
                string.IsNullOrWhiteSpace(label) ? key : label,
                targets.Distinct(StringComparer.OrdinalIgnoreCase).ToList(),
                GetString(item, "success_pattern"),
                GetString(item, "failure_pattern")));
        }
        return services;
    }

    public static async Task<CudyCriticalCheckResult> CheckAsync(
        HttpClient client,
        IReadOnlyList<CudyCriticalService> services,
        CancellationToken cancellationToken)
    {
        if (services.Count == 0)
        {
            return new CudyCriticalCheckResult(Array.Empty<CudyCriticalServiceResult>());
        }
        var checks = services.Select(item => CheckServiceAsync(client, item, cancellationToken));
        return new CudyCriticalCheckResult(await Task.WhenAll(checks));
    }

    private static async Task<CudyCriticalServiceResult> CheckServiceAsync(
        HttpClient client,
        CudyCriticalService service,
        CancellationToken cancellationToken)
    {
        using var serviceTimeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        serviceTimeout.CancelAfter(TimeSpan.FromSeconds(15));
        var targets = new List<CudyCriticalTargetResult>();
        foreach (var target in service.Targets)
        {
            CudyCriticalTargetResult result;
            try
            {
                result = await CheckTargetAsync(
                    client,
                    target,
                    service.SuccessPattern,
                    service.FailurePattern,
                    serviceTimeout.Token);
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                result = new CudyCriticalTargetResult(
                    target,
                    0,
                    false,
                    false,
                    false,
                    "Service probe exceeded its 15 second budget");
            }
            targets.Add(result);
            if (result.Ok || serviceTimeout.IsCancellationRequested)
            {
                break;
            }
        }
        return new CudyCriticalServiceResult(service.Key, service.Label, targets.Any(item => item.Ok), targets);
    }

    private static async Task<CudyCriticalTargetResult> CheckTargetAsync(
        HttpClient client,
        string url,
        string successPattern,
        string failurePattern,
        CancellationToken cancellationToken)
    {
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeout.CancelAfter(TimeSpan.FromSeconds(12));
            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            request.Headers.Range = new RangeHeaderValue(0, MaxBodyBytes - 1);
            using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token);
            var body = await ReadBoundedBodyAsync(response, timeout.Token);
            var successMatched = string.IsNullOrWhiteSpace(successPattern)
                || Regex.IsMatch(body, successPattern, RegexOptions.IgnoreCase | RegexOptions.Multiline | RegexOptions.CultureInvariant, RegexTimeout);
            var failureMatched = !string.IsNullOrWhiteSpace(failurePattern)
                && Regex.IsMatch(body, failurePattern, RegexOptions.IgnoreCase | RegexOptions.Multiline | RegexOptions.CultureInvariant, RegexTimeout);
            var code = (int)response.StatusCode;
            return new CudyCriticalTargetResult(url, code, successMatched, failureMatched, code > 0 && successMatched && !failureMatched, "");
        }
        catch (Exception ex) when (ex is not OperationCanceledException || !cancellationToken.IsCancellationRequested)
        {
            return new CudyCriticalTargetResult(url, 0, false, false, false, ex.Message);
        }
    }

    private static async Task<string> ReadBoundedBodyAsync(HttpResponseMessage response, CancellationToken cancellationToken)
    {
        await using var stream = await response.Content.ReadAsStreamAsync(cancellationToken);
        using var buffer = new MemoryStream();
        var chunk = new byte[16_384];
        while (buffer.Length < MaxBodyBytes)
        {
            var remaining = Math.Min(chunk.Length, MaxBodyBytes - (int)buffer.Length);
            var read = await stream.ReadAsync(chunk.AsMemory(0, remaining), cancellationToken);
            if (read <= 0)
            {
                break;
            }
            buffer.Write(chunk, 0, read);
        }
        return Encoding.UTF8.GetString(buffer.ToArray());
    }

    private static string GetString(JsonElement item, string propertyName)
    {
        return item.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? ""
            : "";
    }
}
