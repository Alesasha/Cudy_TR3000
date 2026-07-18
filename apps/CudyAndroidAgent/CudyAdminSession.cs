using Renci.SshNet;
using System.Text.Json;

namespace CudyAndroidAgent;

public sealed class CudyAdminSession : IDisposable
{
    private readonly SshClient? ownedSshClient;
    private string sessionCookie = "";

    private CudyAdminSession(SshClient? ownedSshClient)
    {
        this.ownedSshClient = ownedSshClient;
    }

    public static async Task<CudyAdminSession> ConnectAsync(
        string host,
        string user,
        string privateKey,
        string expectedHostKeySha256)
    {
        if (CudyVpnService.HasSharedControl)
        {
            return new CudyAdminSession(null);
        }
        if (string.IsNullOrWhiteSpace(host) || string.IsNullOrWhiteSpace(user)
            || string.IsNullOrWhiteSpace(privateKey))
        {
            throw new InvalidOperationException("Activate this Android device before opening Administration.");
        }

        var ssh = CudySshControl.CreateClient(host, user, privateKey, expectedHostKeySha256);
        try
        {
            await Task.Run(ssh.Connect);
            return new CudyAdminSession(ssh);
        }
        catch
        {
            ssh.Dispose();
            throw;
        }
    }

    public async Task LoginAsync(string username, string password)
    {
        using var document = await SendAsync(
            HttpMethod.Post,
            "/api/login",
            new { username, password });
        var role = document.RootElement.GetProperty("user").GetProperty("role").GetString() ?? "";
        if (!string.Equals(role, "admin", StringComparison.Ordinal))
        {
            throw new InvalidOperationException("This account does not have the admin role.");
        }
    }

    public Task<JsonDocument> GetAdminAsync() => SendAsync(HttpMethod.Get, "/api/admin");

    public Task<JsonDocument> PostAsync(string path, object payload) =>
        SendAsync(HttpMethod.Post, path, payload);

    public Task<JsonDocument> DeleteAsync(string path) => SendAsync(HttpMethod.Delete, path);

    private async Task<JsonDocument> SendAsync(HttpMethod method, string path, object? payload = null)
    {
        var body = payload is null ? null : JsonSerializer.Serialize(payload);
        CudySshControl.ControlResponse response;
        if (ownedSshClient is null)
        {
            response = await CudyVpnService.RunSharedControlRequestAsync(
                method.Method,
                sessionCookie,
                path,
                body);
        }
        else
        {
            response = await Task.Run(() => CudySshControl.RunControlRequestDetailed(
                ownedSshClient,
                method.Method,
                null,
                sessionCookie,
                path,
                body));
        }

        if (!string.IsNullOrWhiteSpace(response.SetCookie))
        {
            sessionCookie = response.SetCookie.Split(';', 2)[0].Trim();
        }
        if (response.StatusCode is < 200 or >= 300)
        {
            var message = response.Body.Trim();
            try
            {
                using var errorDocument = JsonDocument.Parse(response.Body);
                message = errorDocument.RootElement.TryGetProperty("error", out var error)
                    ? error.GetString() ?? message
                    : message;
            }
            catch (JsonException)
            {
                // Preserve non-JSON server errors.
            }
            throw new InvalidOperationException($"HTTP {response.StatusCode}: {message}");
        }
        return JsonDocument.Parse(response.Body);
    }

    public void Dispose()
    {
        if (ownedSshClient is null)
        {
            return;
        }
        try
        {
            ownedSshClient.Disconnect();
        }
        catch
        {
            // Best-effort session cleanup.
        }
        ownedSshClient.Dispose();
    }
}
