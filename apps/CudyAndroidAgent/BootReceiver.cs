namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.OS;
using Android.Util;

[BroadcastReceiver(
    Name = "com.nashvpn.cudyagent.BootReceiver",
    Enabled = true,
    Exported = true)]
[IntentFilter(new[]
{
    Intent.ActionBootCompleted,
    Intent.ActionUserUnlocked,
    Intent.ActionMyPackageReplaced,
    BootReceiver.ActionTestStart
})]
public sealed class BootReceiver : BroadcastReceiver
{
    private const string LogTag = "CudyAgent";
    public const string ActionTestStart = "com.nashvpn.cudyagent.TEST_BOOT_START";

    public override void OnReceive(Context? context, Intent? intent)
    {
        if (context is null)
        {
            return;
        }

        var action = intent?.Action ?? "";
        if (action != Intent.ActionBootCompleted
            && action != Intent.ActionUserUnlocked
            && action != Intent.ActionMyPackageReplaced
            && action != ActionTestStart)
        {
            return;
        }

        var preferences = context.GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        var controlUrl = preferences?.GetString("control_url", "")?.Trim() ?? "";
        var deviceId = preferences?.GetString("device_id", "")?.Trim() ?? "";
        var token = preferences?.GetString("token", "") ?? "";
        var sshHost = preferences?.GetString("ssh_host", "")?.Trim() ?? "";
        var sshUser = preferences?.GetString("ssh_user", "")?.Trim() ?? "";
        var sshKey = preferences?.GetString("ssh_key", "") ?? "";

        if (string.IsNullOrWhiteSpace(controlUrl)
            || string.IsNullOrWhiteSpace(deviceId)
            || string.IsNullOrWhiteSpace(token)
            || string.IsNullOrWhiteSpace(sshHost)
            || string.IsNullOrWhiteSpace(sshUser)
            || string.IsNullOrWhiteSpace(sshKey))
        {
            Log.Info(LogTag, $"Boot receiver skipped start for {action}: settings are incomplete.");
            preferences?.Edit()
                ?.PutString("service_status", "boot skipped: settings incomplete")
                ?.PutString("service_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
                ?.Apply();
            return;
        }

        var serviceIntent = new Intent(context, typeof(CudyVpnService));
        serviceIntent.SetAction(CudyVpnService.ActionStart);
        serviceIntent.PutExtra("control_url", controlUrl);
        serviceIntent.PutExtra("device_id", deviceId);
        serviceIntent.PutExtra("token", token);
        serviceIntent.PutExtra("ssh_host", sshHost);
        serviceIntent.PutExtra("ssh_user", sshUser);
        serviceIntent.PutExtra("ssh_key", sshKey);
        serviceIntent.PutExtra("control_only", false);

        try
        {
            if ((int)Build.VERSION.SdkInt >= 26)
            {
#pragma warning disable CA1416
                context.StartForegroundService(serviceIntent);
#pragma warning restore CA1416
            }
            else
            {
                context.StartService(serviceIntent);
            }
            preferences?.Edit()
                ?.PutString("service_status", $"boot start requested: {action}")
                ?.PutString("service_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
                ?.Apply();
            Log.Info(LogTag, $"Boot receiver requested agent start for {action}.");
        }
        catch (Exception ex)
        {
            preferences?.Edit()
                ?.PutString("service_status", $"boot start failed: {ex.Message}")
                ?.PutString("service_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
                ?.Apply();
            Log.Warn(LogTag, $"Boot receiver failed to start agent for {action}: {ex.Message}");
        }
    }
}
