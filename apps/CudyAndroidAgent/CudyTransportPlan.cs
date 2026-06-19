using System.Text.Json;

namespace CudyAndroidAgent;

public sealed record CudyTransportEntry(
    string ServerId,
    string TransportType,
    string InterfaceName,
    JsonElement Raw)
{
    public bool HasConfig =>
        Raw.TryGetProperty("config", out _)
        || Raw.TryGetProperty("sing_box_config", out _)
        || Raw.TryGetProperty("singbox_config", out _);

    public bool HasCredentials =>
        Raw.TryGetProperty("credentials", out _)
        || Raw.TryGetProperty("auth", out _)
        || Raw.TryGetProperty("password", out _)
        || Raw.TryGetProperty("token", out _);
}

public sealed class CudyTransportPlan
{
    public CudyTransportPlan(IReadOnlyList<CudyTransportEntry> entries)
    {
        Entries = entries;
    }

    public IReadOnlyList<CudyTransportEntry> Entries { get; }

    public int Count => Entries.Count;

    public static CudyTransportPlan Parse(JsonElement root)
    {
        if (!root.TryGetProperty("transport_plan", out var plan))
        {
            return new CudyTransportPlan(Array.Empty<CudyTransportEntry>());
        }

        var entries = new List<CudyTransportEntry>();
        if (plan.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in plan.EnumerateArray())
            {
                AddEntry(entries, item);
            }
        }
        else if (plan.ValueKind == JsonValueKind.Object)
        {
            foreach (var property in plan.EnumerateObject())
            {
                AddEntry(entries, property.Value, fallbackServerId: property.Name);
            }
        }

        return new CudyTransportPlan(entries);
    }

    public CudyTransportEntry? Find(string serverId)
    {
        return Entries.FirstOrDefault(
            item => string.Equals(item.ServerId, serverId, StringComparison.OrdinalIgnoreCase));
    }

    public string ToSafeSummary(int maxItems = 8)
    {
        if (Entries.Count == 0)
        {
            return "";
        }

        var lines = new List<string>();
        foreach (var entry in Entries.Take(maxItems))
        {
            var flags = new List<string>();
            if (entry.HasConfig)
            {
                flags.Add("config");
            }
            if (entry.HasCredentials)
            {
                flags.Add("credentials");
            }
            var suffix = flags.Count > 0 ? " " + string.Join(",", flags) : "";
            lines.Add($"  {entry.ServerId}: {entry.TransportType} iface={entry.InterfaceName}{suffix}");
        }
        if (Entries.Count > maxItems)
        {
            lines.Add($"  ... +{Entries.Count - maxItems} more");
        }
        return string.Join("\n", lines);
    }

    private static void AddEntry(
        ICollection<CudyTransportEntry> entries,
        JsonElement item,
        string fallbackServerId = "?")
    {
        if (item.ValueKind != JsonValueKind.Object)
        {
            return;
        }

        var serverId = GetString(item, "server_id")
            ?? GetString(item, "id")
            ?? fallbackServerId;
        var transportType = GetString(item, "transport_type")
            ?? GetString(item, "type")
            ?? "?";
        var interfaceName = GetString(item, "interface_name")
            ?? GetString(item, "iface")
            ?? serverId;
        entries.Add(new CudyTransportEntry(serverId, transportType, interfaceName, item.Clone()));
    }

    private static string? GetString(JsonElement item, string propertyName)
    {
        return item.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;
    }
}
