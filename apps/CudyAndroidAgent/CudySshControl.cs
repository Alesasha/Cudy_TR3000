using Renci.SshNet;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
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

    public static void DownloadWithNewClient(
        string host,
        string user,
        string privateKey,
        string expectedHostKeySha256,
        string token,
        string path,
        string destinationPath,
        CancellationToken cancellationToken,
        Action<long, long>? progress = null,
        uint remotePort = 8765)
    {
        const int maximumAttempts = 4;
        for (var attempt = 1; attempt <= maximumAttempts; attempt++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                DownloadAttempt(
                    host,
                    user,
                    privateKey,
                    expectedHostKeySha256,
                    token,
                    path,
                    destinationPath,
                    cancellationToken,
                    progress,
                    remotePort);
                return;
            }
            catch (Exception) when (attempt < maximumAttempts && !cancellationToken.IsCancellationRequested)
            {
                Thread.Sleep(TimeSpan.FromSeconds(attempt));
            }
        }

        throw new IOException("Update download failed after all retry attempts.");
    }

    private static void DownloadAttempt(
        string host,
        string user,
        string privateKey,
        string expectedHostKeySha256,
        string token,
        string path,
        string destinationPath,
        CancellationToken cancellationToken,
        Action<long, long>? progress,
        uint remotePort)
    {
        var directory = Path.GetDirectoryName(destinationPath);
        if (!string.IsNullOrWhiteSpace(directory))
        {
            Directory.CreateDirectory(directory);
        }
        var existingLength = File.Exists(destinationPath) ? new FileInfo(destinationPath).Length : 0;
        using var client = CreateClient(host, user, privateKey, expectedHostKeySha256);
        client.Connect();
        using var forward = new ForwardedPortLocal("127.0.0.1", 0, "127.0.0.1", remotePort);
        client.AddForwardedPort(forward);
        forward.Start();
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromMinutes(12) };
            using var request = new HttpRequestMessage(
                HttpMethod.Get,
                $"http://127.0.0.1:{forward.BoundPort}{path}");
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
            if (existingLength > 0)
            {
                request.Headers.Range = new RangeHeaderValue(existingLength, null);
            }
            using var response = http.SendAsync(
                request,
                HttpCompletionOption.ResponseHeadersRead,
                cancellationToken).GetAwaiter().GetResult();
            response.EnsureSuccessStatusCode();
            var resumed = existingLength > 0 && response.StatusCode == HttpStatusCode.PartialContent;
            if (!resumed)
            {
                existingLength = 0;
            }
            var totalLength = response.Content.Headers.ContentRange?.Length
                ?? ((response.Content.Headers.ContentLength ?? 0) + existingLength);
            using var source = response.Content.ReadAsStream(cancellationToken);
            using var destination = new FileStream(
                destinationPath,
                resumed ? FileMode.Append : FileMode.Create,
                FileAccess.Write,
                FileShare.Read);
            var buffer = new byte[128 * 1024];
            var downloaded = existingLength;
            progress?.Invoke(downloaded, totalLength);
            while (true)
            {
                var count = source.ReadAsync(buffer, cancellationToken).AsTask().GetAwaiter().GetResult();
                if (count <= 0)
                {
                    break;
                }
                destination.Write(buffer, 0, count);
                downloaded += count;
                progress?.Invoke(downloaded, totalLength);
            }
            destination.Flush(flushToDisk: true);
        }
        finally
        {
            if (forward.IsStarted)
            {
                forward.Stop();
            }
            client.RemoveForwardedPort(forward);
        }
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
            Timeout = TimeSpan.FromSeconds(12),
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

    public static string RunControlRequest(
        ForwardedPortLocal forward,
        string method,
        string? token,
        string path,
        string? body)
    {
        var response = RunControlRequestDetailed(forward, method, token, null, path, body);
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
            return RunControlRequestDetailed(forward, method, token, cookie, path, body);
        }
        finally
        {
            forward.Stop();
            client.RemoveForwardedPort(forward);
        }
    }

    public static ControlResponse RunControlRequestDetailed(
        ForwardedPortLocal forward,
        string method,
        string? token,
        string? cookie,
        string path,
        string? body)
    {
        if (!forward.IsStarted)
        {
            throw new InvalidOperationException("SSH control forward is not started.");
        }
        using var handler = new SocketsHttpHandler
        {
            ConnectTimeout = TimeSpan.FromSeconds(2),
        };
        // A working VPN must not wait a full minute on a stalled control request.
        // The caller reconnects SSH once and keeps the last applied policy active.
        using var http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(20) };
        for (var attempt = 1; ; attempt++)
        {
            try
            {
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
            catch (HttpRequestException ex) when (attempt < 5 && IsLocalForwardStarting(ex))
            {
                Thread.Sleep(TimeSpan.FromMilliseconds(150 * attempt));
            }
        }
    }

    private static bool IsLocalForwardStarting(Exception error)
    {
        for (var current = error; current is not null; current = current.InnerException!)
        {
            if (current is SocketException socket
                && socket.SocketErrorCode is SocketError.ConnectionRefused or SocketError.AddressNotAvailable)
            {
                return true;
            }
            if (current.InnerException is null)
            {
                break;
            }
        }
        return false;
    }
}
