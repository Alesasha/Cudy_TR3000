using System.Text.Json;

namespace CudyAndroidAgent;

public static class CudyPolicy
{
    public static string Summarize(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var deviceId = root.TryGetProperty("device", out var device)
            && device.TryGetProperty("id", out var id) ? id.GetString() : "?";
        var domainRoutes = root.TryGetProperty("domain_routes", out var domains) ? domains.GetArrayLength() : 0;
        var ipRoutes = root.TryGetProperty("ip_routes", out var ips) ? ips.GetArrayLength() : 0;
        var cleanupRoutes = root.TryGetProperty("cleanup_ip_routes", out var cleanup) ? cleanup.GetArrayLength() : 0;
        var transportPlan = CudyTransportPlan.Parse(root);
        var transportSummary = transportPlan.ToSafeSummary();
        var summary = new List<string>
        {
            $"device={deviceId}",
            $"domain_routes={domainRoutes}",
            $"ip_routes={ipRoutes}",
            $"cleanup_ip_routes={cleanupRoutes}",
            $"transport_plan={transportPlan.Count}",
        };
        if (!string.IsNullOrWhiteSpace(transportSummary))
        {
            summary.Add("transports:");
            summary.Add(transportSummary);
        }
        return string.Join("\n", summary);
    }
}
