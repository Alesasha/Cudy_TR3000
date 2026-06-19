using Android.Util;
using Libbox = IO.Nekohasekai.Libbox.Libbox;

namespace CudyAndroidAgent;

public sealed record CudySingBoxRuntimeStatus(
    bool Available,
    string Version,
    bool ConfigChecked,
    string Error)
{
    public string SafeSummary()
    {
        if (!Available)
        {
            return string.IsNullOrWhiteSpace(Error) ? "libbox=missing" : $"libbox=missing error={Error}";
        }
        if (!string.IsNullOrWhiteSpace(Error))
        {
            return $"libbox={Version} config=error";
        }
        var check = ConfigChecked ? "config=ok" : "config=not-checked";
        return $"libbox={Version} {check}";
    }
}

public static class CudySingBoxRuntime
{
    private const string LogTag = "CudyAgent";

    public static CudySingBoxRuntimeStatus Probe(IReadOnlyList<CudyStoredTransport> transports)
    {
        try
        {
            Libbox.Touch();
            var version = Libbox.Version() ?? "";
            var checkedConfig = false;
            var first = transports.FirstOrDefault();
            if (first is not null)
            {
                try
                {
                    CheckConfig(first.ConfigPath);
                    checkedConfig = true;
                }
                catch (Exception ex)
                {
                    return new CudySingBoxRuntimeStatus(
                        Available: true,
                        Version: string.IsNullOrWhiteSpace(version) ? "unknown" : version,
                        ConfigChecked: false,
                        Error: ex.Message);
                }
            }
            return new CudySingBoxRuntimeStatus(
                Available: true,
                Version: string.IsNullOrWhiteSpace(version) ? "unknown" : version,
                ConfigChecked: checkedConfig,
                Error: "");
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "sing-box runtime probe failed: " + ex.Message);
            return new CudySingBoxRuntimeStatus(false, "", false, ex.Message);
        }
    }

    private static void CheckConfig(string configPath)
    {
        var configJson = File.ReadAllText(configPath);
        Libbox.CheckConfig(configJson);
    }
}
