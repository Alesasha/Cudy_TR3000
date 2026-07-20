namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.OS;
using Android.Util;

[BroadcastReceiver(
    Name = "com.nashvpn.cudyagent.BootReceiver",
    Enabled = true,
    Exported = true,
    DirectBootAware = true)]
[IntentFilter(new[]
{
    Intent.ActionLockedBootCompleted,
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
        if (action != Intent.ActionLockedBootCompleted
            && action != Intent.ActionBootCompleted
            && action != Intent.ActionUserUnlocked
            && action != Intent.ActionMyPackageReplaced
            && action != ActionTestStart)
        {
            return;
        }

        var now = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz");
        var isLockedBoot = action == Intent.ActionLockedBootCompleted;
        StoreBootMarker(context, action, now, isLockedBoot ? "locked-boot-received" : "received", "");
        if (isLockedBoot)
        {
            Log.Info(LogTag, "Boot receiver saw LOCKED_BOOT_COMPLETED; waiting for user unlock before starting agent.");
            return;
        }

        var preferences = context.GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        if (preferences?.GetBoolean("autostart_enabled", true) != true)
        {
            Log.Info(LogTag, $"Boot receiver skipped start for {action}: autostart is disabled.");
            StoreBootMarker(context, action, now, "skipped-autostart-disabled", "");
            preferences?.Edit()
                ?.PutBoolean("agent_requested_running", false)
                ?.PutString("service_status", "boot skipped: autostart disabled")
                ?.PutString("service_state", "stopped")
                ?.PutString("service_status_at", now)
                ?.Apply();
            return;
        }

        CudyRecoveryJobService.Schedule(context);
        CudyUpdateJobService.Schedule(context, immediate: action == Intent.ActionMyPackageReplaced);

        var controlUrl = preferences?.GetString("control_url", "")?.Trim() ?? "";
        var deviceId = preferences?.GetString("device_id", "")?.Trim() ?? "";
        var token = preferences?.GetString("token", "") ?? "";
        var sshHost = preferences?.GetString("ssh_host", "")?.Trim() ?? "";
        var sshUser = preferences?.GetString("ssh_user", "")?.Trim() ?? "";
        var sshHostKeySha256 = preferences?.GetString("ssh_host_key_sha256", "")?.Trim() ?? "";
        var sshKey = preferences?.GetString("ssh_key", "") ?? "";

        if (string.IsNullOrWhiteSpace(controlUrl)
            || string.IsNullOrWhiteSpace(deviceId)
            || string.IsNullOrWhiteSpace(token)
            || string.IsNullOrWhiteSpace(sshHost)
            || string.IsNullOrWhiteSpace(sshUser)
            || string.IsNullOrWhiteSpace(sshKey))
        {
            Log.Info(LogTag, $"Boot receiver skipped start for {action}: settings are incomplete.");
            StoreBootMarker(context, action, now, "skipped-settings-incomplete", "");
            preferences?.Edit()
                ?.PutString("service_status", "boot skipped: settings incomplete")
                ?.PutString("service_status_at", now)
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
        serviceIntent.PutExtra("ssh_host_key_sha256", sshHostKeySha256);
        serviceIntent.PutExtra("ssh_key", sshKey);
        serviceIntent.PutExtra("control_only", false);
        if (action == Intent.ActionBootCompleted || action == Intent.ActionUserUnlocked)
        {
            serviceIntent.PutExtra("startup_delay_seconds", 45);
        }

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
                ?.PutBoolean("agent_requested_running", true)
                ?.PutString("service_status", $"boot start requested: {action}")
                ?.PutString("service_state", "starting")
                ?.PutString("service_status_at", now)
                ?.Apply();
            StoreBootMarker(context, action, now, "start-requested", "");
            Log.Info(LogTag, $"Boot receiver requested agent start for {action}.");
        }
        catch (Exception ex)
        {
            preferences?.Edit()
                ?.PutString("service_status", $"boot start failed: {ex.Message}")
                ?.PutString("service_status_at", now)
                ?.Apply();
            StoreBootMarker(context, action, now, "start-failed", ex.Message);
            Log.Warn(LogTag, $"Boot receiver failed to start agent for {action}: {ex.Message}");
        }
    }

    private static void StoreBootMarker(Context context, string action, string at, string result, string error)
    {
        if ((int)Build.VERSION.SdkInt >= 24)
        {
            try
            {
#pragma warning disable CA1416
                var deviceContext = context.CreateDeviceProtectedStorageContext();
#pragma warning restore CA1416
                if (deviceContext is not null)
                {
                    StoreBootMarker(deviceContext.GetSharedPreferences("cudy-agent-boot", FileCreationMode.Private), action, at, result, error);
                }
            }
            catch (Exception ex)
            {
                Log.Warn(LogTag, $"Failed to store direct-boot marker for {action}: {ex.Message}");
            }
        }

        try
        {
            if ((int)Build.VERSION.SdkInt >= 24)
            {
                var userManager = context.GetSystemService(Context.UserService) as UserManager;
                if (userManager?.IsUserUnlocked != true)
                {
                    return;
                }
            }
            StoreBootMarker(context.GetSharedPreferences("cudy-agent", FileCreationMode.Private), action, at, result, error);
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, $"Failed to store credential boot marker for {action}: {ex.Message}");
        }
    }

    private static void StoreBootMarker(ISharedPreferences? preferences, string action, string at, string result, string error)
    {
        preferences?.Edit()
            ?.PutString("boot_receiver_action", action)
            ?.PutString("boot_receiver_at", at)
            ?.PutString("boot_receiver_result", result)
            ?.PutString("boot_receiver_error", error)
            ?.Apply();
    }
}
