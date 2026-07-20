namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.Util;

[BroadcastReceiver(
    Name = "com.nashvpn.cudyagent.CudyUpdateInstallReceiver",
    Enabled = true,
    Exported = false)]
[IntentFilter(new[] { ActionInstallStatus })]
public sealed class CudyUpdateInstallReceiver : BroadcastReceiver
{
    public const string ActionInstallStatus = "com.nashvpn.cudyagent.UPDATE_INSTALL_STATUS";
    private const string LogTag = "CudyAgent";

    public override void OnReceive(Context? context, Intent? intent)
    {
        if (context is null || intent?.Action != ActionInstallStatus)
        {
            return;
        }
        var status = (PackageInstallStatus)intent.GetIntExtra(
            PackageInstaller.ExtraStatus,
            (int)PackageInstallStatus.Failure);
        var message = intent.GetStringExtra(PackageInstaller.ExtraStatusMessage) ?? "";
        var preferences = context.GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        if (status == PackageInstallStatus.PendingUserAction)
        {
#pragma warning disable CA1422
            var confirmation = intent.GetParcelableExtra(Intent.ExtraIntent) as Intent;
#pragma warning restore CA1422
            if (confirmation is not null)
            {
                confirmation.AddFlags(ActivityFlags.NewTask);
                context.StartActivity(confirmation);
                preferences?.Edit()?.PutString("update_status", "awaiting-confirmation")?.Apply();
                return;
            }
        }
        if (status == PackageInstallStatus.Success)
        {
            preferences?.Edit()
                ?.PutString("update_status", "installed")
                ?.PutString("update_error", "")
                ?.Apply();
            Log.Info(LogTag, "Android update installed successfully.");
            return;
        }
        preferences?.Edit()
            ?.PutString("update_status", "install-failed")
            ?.PutString("update_error", string.IsNullOrWhiteSpace(message) ? $"installer status={status}" : message)
            ?.Apply();
        Log.Warn(LogTag, $"Android update installation failed: status={status} {message}");
    }
}
