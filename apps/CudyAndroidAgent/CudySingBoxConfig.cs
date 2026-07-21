using System.Security.Cryptography;
using System.Net;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace CudyAndroidAgent;

public sealed record CudyPreparedTransport(
    string ServerId,
    string InterfaceName,
    string TransportType,
    string ConfigJson);

public sealed record CudySingBoxRouteOverride(
    string ServerId,
    IReadOnlyList<string> IpCidrs,
    IReadOnlyList<string> DomainSuffixes);

public sealed record CudySingBoxUrlTest(
    string Tag,
    IReadOnlyList<string> ServerIds,
    string Url);

public sealed record CudySingBoxLocalProbe(
    string ServerId,
    int ListenPort);

public static class CudySingBoxConfig
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
    };

    public static IReadOnlyList<CudyPreparedTransport> BuildAll(CudyTransportPlan plan)
    {
        var result = new List<CudyPreparedTransport>();
        foreach (var entry in plan.Entries)
        {
            result.Add(Build(entry));
        }
        return result;
    }

    public static CudyPreparedTransport Build(CudyTransportEntry entry)
    {
        var config = entry.TransportType switch
        {
            "http-proxy-tun" => BuildHttpProxyConfig(entry),
            "vless-reality-tun" => BuildVlessRealityConfig(entry),
            "sing-box-json" => CloneConfigObject(entry),
            _ => throw new NotSupportedException(
                $"Unsupported transport type '{entry.TransportType}' for {entry.ServerId}."),
        };

        return new CudyPreparedTransport(
            entry.ServerId,
            entry.InterfaceName,
            entry.TransportType,
            config.ToJsonString(JsonOptions));
    }

    public static CudyPreparedTransport BuildAndroidUnified(
        JsonElement root,
        CudyTransportPlan plan,
        IReadOnlyList<CudySingBoxRouteOverride>? routeOverrides = null,
        CudySingBoxUrlTest? urlTest = null,
        IReadOnlyList<CudySingBoxLocalProbe>? localProbes = null)
    {
        var outbounds = new JsonArray
        {
            new JsonObject { ["type"] = "direct", ["tag"] = "direct" },
            new JsonObject { ["type"] = "block", ["tag"] = "block" },
        };
        var directCidrs = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
        var directDomains = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
        var outboundTags = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["direct"] = "direct",
            ["block"] = "block",
        };

        foreach (var entry in plan.Entries)
        {
            var tag = OutboundTag(entry.ServerId);
            try
            {
                var outbound = BuildProxyOutbound(entry, tag, directCidrs, directDomains);
                outbounds.Add(outbound);
                outboundTags[entry.ServerId] = tag;
            }
            catch (Exception ex) when (ex is InvalidOperationException or NotSupportedException)
            {
                // Keep the rest of the VPN usable while the control plane selects a supported fallback.
                Android.Util.Log.Warn(
                    "CudyAgent",
                    $"Transport {entry.ServerId} is unavailable on Android and will be blocked: {ex.Message}");
                outboundTags[entry.ServerId] = "block";
            }
        }
        AddUrlTestOutbound(urlTest, outbounds, outboundTags);

        var inbounds = new JsonArray
        {
            new JsonObject
            {
                ["type"] = "tun",
                ["tag"] = "cudy-tun",
                ["interface_name"] = "cudy0",
                ["address"] = new JsonArray { "172.40.0.1/30" },
                ["dns_mode"] = "hijack",
                ["mtu"] = 1400,
                ["auto_route"] = true,
                ["strict_route"] = false,
                ["stack"] = "gvisor",
                ["exclude_package"] = AndroidVpnBypassPackages(root),
            },
        };
        AddLocalProbeInbounds(localProbes, inbounds);

        var rules = new JsonArray
        {
            new JsonObject { ["action"] = "sniff" },
            new JsonObject
            {
                ["protocol"] = "dns",
                ["action"] = "hijack-dns",
            },
        };
        AddLocalProbeRules(localProbes, rules, outboundTags);
        if (directCidrs.Count > 0)
        {
            rules.Add(new JsonObject
            {
                ["ip_cidr"] = new JsonArray(directCidrs.Select(item => JsonValue.Create(item)).ToArray<JsonNode?>()),
                ["outbound"] = "direct",
            });
        }
        if (directDomains.Count > 0)
        {
            rules.Add(new JsonObject
            {
                ["domain"] = new JsonArray(directDomains.Select(item => JsonValue.Create(item)).ToArray<JsonNode?>()),
                ["outbound"] = "direct",
            });
        }

        AddRouteOverrides(routeOverrides, rules, outboundTags);
        AddPolicyRules(root, "ip_routes", rules, outboundTags);
        AddPolicyRules(root, "domain_routes", rules, outboundTags);

        var tunneledDomains = CollectTunneledDomainSuffixes(root, routeOverrides, outboundTags);
        var dnsServers = new JsonArray
        {
            new JsonObject
            {
                ["type"] = "udp",
                ["tag"] = "cloudflare",
                ["server"] = "1.1.1.1",
                ["server_port"] = 53,
            },
            new JsonObject
            {
                ["type"] = "fakeip",
                ["tag"] = "fakeip",
                ["inet4_range"] = "198.18.0.0/15",
            },
        };
        var dnsRules = new JsonArray();
        if (tunneledDomains.Count > 0)
        {
            dnsRules.Add(new JsonObject
            {
                ["domain_suffix"] = new JsonArray(
                    tunneledDomains.Select(item => JsonValue.Create(item)).ToArray<JsonNode?>()),
                ["action"] = "route",
                ["server"] = "fakeip",
            });
        }

        var config = new JsonObject
        {
            ["log"] = new JsonObject
            {
                ["level"] = "info",
                ["timestamp"] = true,
            },
            ["dns"] = new JsonObject
            {
                ["servers"] = dnsServers,
                ["rules"] = dnsRules,
                ["final"] = "cloudflare",
                ["strategy"] = "ipv4_only",
                ["reverse_mapping"] = true,
            },
            ["inbounds"] = inbounds,
            ["outbounds"] = outbounds,
            ["route"] = new JsonObject
            {
                ["auto_detect_interface"] = true,
                ["rules"] = rules,
                ["final"] = "direct",
            },
        };

        return new CudyPreparedTransport(
            "android-unified",
            "cudy0",
            "sing-box-unified",
            config.ToJsonString(JsonOptions));
    }

    private static JsonArray AndroidVpnBypassPackages(JsonElement root)
    {
        var packages = new SortedSet<string>(StringComparer.Ordinal)
        {
            "com.nashvpn.cudyagent",
        };
        if (root.TryGetProperty("platform_settings", out var platformSettings)
            && platformSettings.ValueKind == JsonValueKind.Object
            && platformSettings.TryGetProperty("android", out var android)
            && android.ValueKind == JsonValueKind.Object
            && android.TryGetProperty("vpn_bypass_packages", out var configured)
            && configured.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in configured.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.String)
                {
                    continue;
                }
                var packageName = (item.GetString() ?? "").Trim();
                if (packageName.Length is > 2 and <= 255
                    && packageName.Contains('.', StringComparison.Ordinal)
                    && packageName.All(ch => char.IsLetterOrDigit(ch) || ch is '.' or '_'))
                {
                    packages.Add(packageName);
                }
            }
        }
        return new JsonArray(packages.Select(item => JsonValue.Create(item)).ToArray<JsonNode?>());
    }

    private static SortedSet<string> CollectTunneledDomainSuffixes(
        JsonElement root,
        IReadOnlyList<CudySingBoxRouteOverride>? routeOverrides,
        IReadOnlyDictionary<string, string> outboundTags)
    {
        var result = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
        if (routeOverrides is not null)
        {
            foreach (var route in routeOverrides)
            {
                if (!outboundTags.TryGetValue(route.ServerId, out var outbound)
                    || string.Equals(outbound, "direct", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                foreach (var domain in route.DomainSuffixes)
                {
                    var normalized = NormalizeDomain(domain);
                    if (!string.IsNullOrWhiteSpace(normalized))
                    {
                        result.Add(normalized);
                    }
                }
            }
        }

        if (!root.TryGetProperty("domain_routes", out var routes) || routes.ValueKind != JsonValueKind.Array)
        {
            return result;
        }
        foreach (var route in routes.EnumerateArray())
        {
            if (route.ValueKind != JsonValueKind.Object)
            {
                continue;
            }
            var serverId = OptionalString(route, "server_id") ?? "";
            if (!outboundTags.TryGetValue(serverId, out var outbound)
                || string.Equals(outbound, "direct", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }
            var domain = NormalizeDomain(OptionalString(route, "domain"));
            if (!string.IsNullOrWhiteSpace(domain))
            {
                result.Add(domain);
            }
        }
        return result;
    }

    private static JsonObject BuildHttpProxyConfig(CudyTransportEntry entry)
    {
        var source = RequiredConfig(entry);
        var host = RequiredString(source, "server", entry);
        var port = RequiredInt(source, "server_port", entry);
        var proxyType = OptionalString(source, "proxy_type") ?? "http";
        return BaseTunConfig(
            entry,
            tunAddressBase: 41,
            proxyOutbound: BuildHttpProxyOutbound(proxyType, "proxy-out", host, port),
            directHost: host);
    }

    private static JsonObject BuildVlessRealityConfig(CudyTransportEntry entry)
    {
        var source = RequiredConfig(entry);
        var host = RequiredString(source, "server", entry);
        var tls = RequiredObject(source, "tls", entry);
        var reality = RequiredObject(tls, "reality", entry);
        var outbound = BuildVlessRealityOutbound(entry, source, tls, reality, "proxy-out", host);
        var flow = OptionalString(source, "flow");
        if (!string.IsNullOrWhiteSpace(flow))
        {
            outbound["flow"] = flow;
        }

        return BaseTunConfig(entry, tunAddressBase: 43, proxyOutbound: outbound, directHost: host);
    }

    private static JsonObject BuildProxyOutbound(
        CudyTransportEntry entry,
        string tag,
        ISet<string> directCidrs,
        ISet<string> directDomains)
    {
        if (entry.TransportType == "sing-box-json")
        {
            var outbound = ExtractFirstProxyOutbound(entry);
            outbound["tag"] = tag;
            AddDirectEndpointRules(outbound, directCidrs, directDomains);
            return outbound;
        }

        var source = RequiredConfig(entry);
        var host = RequiredString(source, "server", entry);
        AddDirectHostRule(host, directCidrs, directDomains);
        return entry.TransportType switch
        {
            "http-proxy-tun" => BuildHttpProxyOutbound(
                OptionalString(source, "proxy_type") ?? "http",
                tag,
                host,
                RequiredInt(source, "server_port", entry)),
            "vless-reality-tun" => BuildVlessRealityOutbound(
                entry,
                source,
                RequiredObject(source, "tls", entry),
                RequiredObject(RequiredObject(source, "tls", entry), "reality", entry),
                tag,
                host),
            _ => throw new NotSupportedException(
                $"Unsupported transport type '{entry.TransportType}' for {entry.ServerId}."),
        };
    }

    private static void AddLocalProbeInbounds(
        IReadOnlyList<CudySingBoxLocalProbe>? localProbes,
        JsonArray inbounds)
    {
        if (localProbes is null)
        {
            return;
        }
        foreach (var probe in localProbes.Where(item => item.ListenPort > 0))
        {
            inbounds.Add(new JsonObject
            {
                ["type"] = "mixed",
                ["tag"] = LocalProbeInboundTag(probe.ServerId),
                ["listen"] = "127.0.0.1",
                ["listen_port"] = probe.ListenPort,
            });
        }
    }

    private static void AddLocalProbeRules(
        IReadOnlyList<CudySingBoxLocalProbe>? localProbes,
        JsonArray rules,
        IReadOnlyDictionary<string, string> outboundTags)
    {
        if (localProbes is null)
        {
            return;
        }
        foreach (var probe in localProbes)
        {
            if (!outboundTags.TryGetValue(probe.ServerId, out var outbound))
            {
                continue;
            }
            rules.Add(new JsonObject
            {
                ["inbound"] = new JsonArray { LocalProbeInboundTag(probe.ServerId) },
                ["outbound"] = outbound,
            });
        }
    }

    private static string LocalProbeInboundTag(string serverId)
    {
        var safe = new string(serverId.Select(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_' ? ch : '-').ToArray());
        return "probe-in-" + (string.IsNullOrWhiteSpace(safe) ? "server" : safe);
    }

    private static void AddUrlTestOutbound(
        CudySingBoxUrlTest? urlTest,
        JsonArray outbounds,
        IReadOnlyDictionary<string, string> outboundTags)
    {
        if (urlTest is null || urlTest.ServerIds.Count == 0)
        {
            return;
        }
        var tags = urlTest.ServerIds
            .Where(outboundTags.ContainsKey)
            .Select(serverId => JsonValue.Create(outboundTags[serverId]))
            .ToArray<JsonNode?>();
        if (tags.Length == 0)
        {
            return;
        }
        outbounds.Add(new JsonObject
        {
            ["type"] = "urltest",
            ["tag"] = urlTest.Tag,
            ["outbounds"] = new JsonArray(tags),
            ["url"] = string.IsNullOrWhiteSpace(urlTest.Url) ? "https://www.gstatic.com/generate_204" : urlTest.Url,
            ["interval"] = "1m",
            ["tolerance"] = 1,
        });
    }

    private static JsonObject BuildHttpProxyOutbound(string proxyType, string tag, string host, int port)
    {
        return new JsonObject
        {
            ["type"] = proxyType,
            ["tag"] = tag,
            ["server"] = host,
            ["server_port"] = port,
        };
    }

    private static JsonObject BuildVlessRealityOutbound(
        CudyTransportEntry entry,
        JsonElement source,
        JsonElement tls,
        JsonElement reality,
        string tag,
        string host)
    {
        var outbound = new JsonObject
        {
            ["type"] = "vless",
            ["tag"] = tag,
            ["server"] = host,
            ["server_port"] = RequiredInt(source, "server_port", entry),
            ["uuid"] = RequiredString(source, "uuid", entry),
            ["tls"] = new JsonObject
            {
                ["enabled"] = true,
                ["server_name"] = RequiredString(tls, "server_name", entry),
                ["utls"] = new JsonObject
                {
                    ["enabled"] = true,
                    ["fingerprint"] = "chrome",
                },
                ["reality"] = new JsonObject
                {
                    ["enabled"] = true,
                    ["public_key"] = RequiredString(reality, "public_key", entry),
                    ["short_id"] = OptionalString(reality, "short_id") ?? "",
                },
            },
        };
        var flow = OptionalString(source, "flow");
        if (!string.IsNullOrWhiteSpace(flow))
        {
            outbound["flow"] = flow;
        }
        return outbound;
    }

    private static JsonObject ExtractFirstProxyOutbound(CudyTransportEntry entry)
    {
        var config = CloneConfigObject(entry);
        if (!config.TryGetPropertyValue("outbounds", out var outboundsNode)
            || outboundsNode is not JsonArray outbounds)
        {
            throw new InvalidOperationException($"Transport {entry.ServerId} sing-box config has no outbounds.");
        }
        foreach (var outboundNode in outbounds)
        {
            if (outboundNode is not JsonObject outbound)
            {
                continue;
            }
            var type = outbound["type"]?.GetValue<string>() ?? "";
            if (!string.Equals(type, "direct", StringComparison.OrdinalIgnoreCase)
                && !string.Equals(type, "block", StringComparison.OrdinalIgnoreCase)
                && !string.Equals(type, "dns", StringComparison.OrdinalIgnoreCase))
            {
                return JsonNode.Parse(outbound.ToJsonString())?.AsObject()
                    ?? throw new InvalidOperationException($"Invalid outbound in {entry.ServerId}.");
            }
        }
        throw new InvalidOperationException($"Transport {entry.ServerId} sing-box config has no proxy outbound.");
    }

    private static void AddPolicyRules(
        JsonElement root,
        string propertyName,
        JsonArray rules,
        IReadOnlyDictionary<string, string> outboundTags)
    {
        if (!root.TryGetProperty(propertyName, out var routes) || routes.ValueKind != JsonValueKind.Array)
        {
            return;
        }

        foreach (var route in routes.EnumerateArray())
        {
            if (route.ValueKind != JsonValueKind.Object)
            {
                continue;
            }
            var serverId = OptionalString(route, "server_id") ?? "";
            if (string.IsNullOrWhiteSpace(serverId) || !outboundTags.TryGetValue(serverId, out var outbound))
            {
                continue;
            }

            if (propertyName == "ip_routes")
            {
                var cidr = OptionalString(route, "target_cidr");
                if (!string.IsNullOrWhiteSpace(cidr))
                {
                    rules.Add(new JsonObject
                    {
                        ["ip_cidr"] = new JsonArray { cidr },
                        ["outbound"] = outbound,
                    });
                }
            }
            else if (propertyName == "domain_routes")
            {
                var domain = NormalizeDomain(OptionalString(route, "domain"));
                if (!string.IsNullOrWhiteSpace(domain))
                {
                    rules.Add(new JsonObject
                    {
                        ["domain_suffix"] = new JsonArray { domain },
                        ["outbound"] = outbound,
                    });
                }
            }
        }
    }

    private static void AddRouteOverrides(
        IReadOnlyList<CudySingBoxRouteOverride>? routeOverrides,
        JsonArray rules,
        IReadOnlyDictionary<string, string> outboundTags)
    {
        if (routeOverrides is null)
        {
            return;
        }
        foreach (var route in routeOverrides)
        {
            if (!outboundTags.TryGetValue(route.ServerId, out var outbound))
            {
                continue;
            }
            if (route.IpCidrs.Count > 0)
            {
                rules.Add(new JsonObject
                {
                    ["ip_cidr"] = new JsonArray(route.IpCidrs.Select(item => JsonValue.Create(item)).ToArray<JsonNode?>()),
                    ["outbound"] = outbound,
                });
            }
            if (route.DomainSuffixes.Count > 0)
            {
                rules.Add(new JsonObject
                {
                    ["domain_suffix"] = new JsonArray(route.DomainSuffixes.Select(item => JsonValue.Create(item)).ToArray<JsonNode?>()),
                    ["outbound"] = outbound,
                });
            }
        }
    }

    private static void AddDirectEndpointRules(
        JsonObject outbound,
        ISet<string> directCidrs,
        ISet<string> directDomains)
    {
        var server = outbound["server"]?.GetValue<string>();
        AddDirectHostRule(server, directCidrs, directDomains);
    }

    private static void AddDirectHostRule(
        string? host,
        ISet<string> directCidrs,
        ISet<string> directDomains)
    {
        if (string.IsNullOrWhiteSpace(host))
        {
            return;
        }
        if (IPAddress.TryParse(host, out var address))
        {
            if (address.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork)
            {
                directCidrs.Add(address + "/32");
            }
            else
            {
                directCidrs.Add(address + "/128");
            }
            return;
        }
        directDomains.Add(host.Trim().TrimEnd('.').ToLowerInvariant());
    }

    private static string OutboundTag(string serverId)
    {
        var safe = new string(serverId.Select(ch => char.IsLetterOrDigit(ch) || ch is '-' or '_' ? ch : '-').ToArray());
        return "out-" + (string.IsNullOrWhiteSpace(safe) ? "server" : safe);
    }

    private static string? NormalizeDomain(string? domain)
    {
        return string.IsNullOrWhiteSpace(domain)
            ? null
            : domain.Trim().Trim('.').ToLowerInvariant();
    }

    private static JsonObject CloneConfigObject(CudyTransportEntry entry)
    {
        var source = RequiredConfig(entry);
        return JsonNode.Parse(source.GetRawText())?.AsObject()
            ?? throw new InvalidOperationException($"Invalid sing-box config for {entry.ServerId}.");
    }

    private static JsonObject BaseTunConfig(
        CudyTransportEntry entry,
        int tunAddressBase,
        JsonObject proxyOutbound,
        string directHost)
    {
        return new JsonObject
        {
            ["log"] = new JsonObject
            {
                ["level"] = "info",
                ["timestamp"] = true,
            },
            ["inbounds"] = new JsonArray
            {
                new JsonObject
                {
                    ["type"] = "tun",
                    ["tag"] = entry.InterfaceName + "-tun",
                    ["interface_name"] = entry.InterfaceName,
                    ["address"] = new JsonArray { TunAddress(entry.InterfaceName, tunAddressBase) },
                    ["mtu"] = 1400,
                    ["auto_route"] = false,
                    ["strict_route"] = false,
                    ["stack"] = "gvisor",
                },
            },
            ["outbounds"] = new JsonArray
            {
                proxyOutbound,
                new JsonObject { ["type"] = "direct", ["tag"] = "direct" },
                new JsonObject { ["type"] = "block", ["tag"] = "block" },
            },
            ["route"] = new JsonObject
            {
                ["auto_detect_interface"] = false,
                ["rules"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["ip_cidr"] = new JsonArray { directHost + "/32" },
                        ["outbound"] = "direct",
                    },
                },
                ["final"] = "proxy-out",
            },
        };
    }

    private static JsonElement RequiredConfig(CudyTransportEntry entry)
    {
        if (!entry.Raw.TryGetProperty("config", out var config) || config.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException($"Transport {entry.ServerId} has no object config.");
        }
        return config;
    }

    private static JsonElement RequiredObject(JsonElement source, string propertyName, CudyTransportEntry entry)
    {
        if (source.TryGetProperty(propertyName, out var value) && value.ValueKind == JsonValueKind.Object)
        {
            return value;
        }
        throw new InvalidOperationException($"Transport {entry.ServerId} config has no object '{propertyName}'.");
    }

    private static string RequiredString(JsonElement source, string propertyName, CudyTransportEntry entry)
    {
        var value = OptionalString(source, propertyName);
        return !string.IsNullOrWhiteSpace(value)
            ? value
            : throw new InvalidOperationException($"Transport {entry.ServerId} config has no '{propertyName}'.");
    }

    private static string? OptionalString(JsonElement source, string propertyName)
    {
        if (!source.TryGetProperty(propertyName, out var value))
        {
            return null;
        }
        return value.ValueKind switch
        {
            JsonValueKind.String => value.GetString(),
            JsonValueKind.Number => value.GetRawText(),
            _ => null,
        };
    }

    private static int RequiredInt(JsonElement source, string propertyName, CudyTransportEntry entry)
    {
        if (!source.TryGetProperty(propertyName, out var value))
        {
            throw new InvalidOperationException($"Transport {entry.ServerId} config has no '{propertyName}'.");
        }
        if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number))
        {
            return number;
        }
        if (value.ValueKind == JsonValueKind.String && int.TryParse(value.GetString(), out number))
        {
            return number;
        }
        throw new InvalidOperationException($"Transport {entry.ServerId} config has invalid '{propertyName}'.");
    }

    private static string TunAddress(string name, int secondOctet)
    {
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(name));
        var value = (hash[0] * 256) + hash[1];
        var thirdOctet = 2 + (value % 238);
        return $"172.{secondOctet}.{thirdOctet}.1/30";
    }
}
