using Renci.SshNet;
using System.Text;

namespace CudyAndroidAgent;

public static class CudySshControl
{
    private const string RemoteControlUrl = "http://127.0.0.1:8765";

    public static string RunCurlWithNewClient(
        string host,
        string user,
        string privateKey,
        string method,
        string? token,
        string path,
        string? body)
    {
        using var client = CreateClient(host, user, privateKey);
        client.Connect();
        return RunCurl(client, method, token, path, body);
    }

    public static SshClient CreateClient(string host, string user, string privateKey)
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
        client.HostKeyReceived += (_, args) => args.CanTrust = true;
        return client;
    }

    public static string RunCurl(
        SshClient client,
        string method,
        string? token,
        string path,
        string? body)
    {
        if (!client.IsConnected)
        {
            throw new InvalidOperationException("SSH control client is not connected.");
        }

        var url = RemoteControlUrl + path;
        var commandText = method == "POST"
            ? "printf %s " + ShellQuote(Convert.ToBase64String(Encoding.UTF8.GetBytes(body ?? "")))
                + " | base64 -d | curl -fsS -m 30 -X POST"
                + AuthHeader(token)
                + " -H " + ShellQuote("Content-Type: application/json")
                + " --data-binary @- " + ShellQuote(url)
            : "curl -fsS -m 30"
                + AuthHeader(token)
                + " " + ShellQuote(url);

        using var command = client.CreateCommand(commandText);
        command.CommandTimeout = TimeSpan.FromSeconds(45);
        var result = command.Execute();
        if (command.ExitStatus != 0)
        {
            throw new InvalidOperationException(
                $"ssh curl failed exit={command.ExitStatus}: {command.Error.Trim()}");
        }
        return result;
    }

    private static string AuthHeader(string? token)
    {
        return string.IsNullOrWhiteSpace(token)
            ? ""
            : " -H " + ShellQuote("Authorization: Bearer " + token);
    }

    private static string ShellQuote(string value)
    {
        return "'" + value.Replace("'", "'\"'\"'") + "'";
    }
}
