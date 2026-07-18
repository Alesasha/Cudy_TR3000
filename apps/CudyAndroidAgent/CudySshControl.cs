using Renci.SshNet;
using System.Net.Http.Headers;
using System.Text;

namespace CudyAndroidAgent;

public static class CudySshControl
{
    public sealed record ControlResponse(int StatusCode, string Body, string SetCookie);

    public static string RunCurlWithNewClient(
        string host,
        string user,
        string privateKey,
        string expectedHostKeySha256,
        string method,
        string? token,
        string path,
        string? body,
        uint remotePort = 8765)
    {
        using var client = CreateClient(host, user, privateKey, expectedHostKeySha256);
        client.Connect();
        return RunControlRequest(client, method, token, path, body, remotePort);
    }

    public static SshClient CreateClient(
        string host,
        string user,
        string privateKey,
        string expectedHostKeySha256 = "")
    {
        var keyBytes = Encoding.UTF8.GetBytes(privateKey.Replace("\r\n", "\n").Trim() + "\n");
        using var keyStream = new MemoryStream(keyBytes);
        var keyFile = new PrivateKeyFile(keyStream);
        var auth = new PrivateKeyAuthenticationMethod(user, keyFile);
        var connection = new ConnectionInfo(host, 22, user, auth)
        {
            Timeout = TimeSpan.FromSeconds(60),
        };
        var client = new SshClient(connection);
        client.HostKeyReceived += (_, args) =>
        {
            args.CanTrust = string.IsNullOrWhiteSpace(expectedHostKeySha256)
                || string.Equals(
                    NormalizeHostKeySha256(args.FingerPrintSHA256),
                    NormalizeHostKeySha256(expectedHostKeySha256),
                    StringComparison.Ordinal);
        };
        return client;
    }

    private static string NormalizeHostKeySha256(string value)
    {
        const string prefix = "SHA256:";
        var normalized = value.Trim();
        if (normalized.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            normalized = normalized[prefix.Length..];
        }
        return normalized.TrimEnd('=');
    }

    public static string RunControlRequest(
        SshClient client,
        string method,
        string? token,
        string path,
        string? body,
        uint remotePort = 8765)
    {
        var response = RunControlRequestDetailed(client, method, token, null, path, body, remotePort);
        if (response.StatusCode is < 200 or >= 300)
        {
            throw new InvalidOperationException(
                $"control request failed http={response.StatusCode}: {response.Body.Trim()}");
        }
        return response.Body;
    }

    public static ControlResponse RunControlRequestDetailed(
        SshClient client,
        string method,
        string? token,
        string? cookie,
        string path,
        string? body,
        uint remotePort = 8765)
    {
        if (!client.IsConnected)
        {
            throw new InvalidOperationException("SSH control client is not connected.");
        }

        using var forward = new ForwardedPortLocal("127.0.0.1", 0, "127.0.0.1", remotePort);
        client.AddForwardedPort(forward);
        forward.Start();
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(45) };
            using var request = new HttpRequestMessage(
                new HttpMethod(method),
                $"http://127.0.0.1:{forward.BoundPort}{path}");
            if (!string.IsNullOrWhiteSpace(token))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
            }
            if (!string.IsNullOrWhiteSpace(cookie))
            {
                request.Headers.TryAddWithoutValidation("Cookie", cookie);
            }
            if (body is not null)
            {
                request.Content = new StringContent(body, Encoding.UTF8, "application/json");
            }
            using var response = http.SendAsync(request).GetAwaiter().GetResult();
            var result = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            var setCookie = response.Headers.TryGetValues("Set-Cookie", out var cookieValues)
                ? cookieValues.FirstOrDefault() ?? ""
                : "";
            return new ControlResponse((int)response.StatusCode, result, setCookie);
        }
        finally
        {
            forward.Stop();
            client.RemoveForwardedPort(forward);
        }
    }
}
