namespace CudyAndroidAgent;

public sealed record CudyStoredTransport(
    string ServerId,
    string InterfaceName,
    string TransportType,
    string ConfigPath);

public static class CudyTransportStore
{
    public static IReadOnlyList<CudyStoredTransport> WriteAll(
        string appFilesPath,
        IEnumerable<CudyPreparedTransport> transports)
    {
        var transportDir = Path.Combine(appFilesPath, "transports");
        Directory.CreateDirectory(transportDir);
        var result = new List<CudyStoredTransport>();
        var desiredPaths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var transport in transports)
        {
            var fileName = SafeFileName(transport.InterfaceName) + ".json";
            var path = Path.Combine(transportDir, fileName);
            desiredPaths.Add(path);
            WriteUtf8Atomic(path, transport.ConfigJson);
            result.Add(new CudyStoredTransport(
                transport.ServerId,
                transport.InterfaceName,
                transport.TransportType,
                path));
        }
        foreach (var stalePath in Directory.EnumerateFiles(transportDir, "*.json"))
        {
            if (!desiredPaths.Contains(stalePath))
            {
                File.Delete(stalePath);
            }
        }
        return result;
    }

    private static string SafeFileName(string value)
    {
        var chars = value.Select(ch =>
            char.IsLetterOrDigit(ch) || ch is '-' or '_' or '.' ? ch : '_').ToArray();
        var result = new string(chars).Trim('.', '_');
        return string.IsNullOrWhiteSpace(result) ? "transport" : result;
    }

    private static void WriteUtf8Atomic(string path, string content)
    {
        var tempPath = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(tempPath, content, System.Text.Encoding.UTF8);
            File.Move(tempPath, path, overwrite: true);
        }
        finally
        {
            if (File.Exists(tempPath))
            {
                File.Delete(tempPath);
            }
        }
    }
}
