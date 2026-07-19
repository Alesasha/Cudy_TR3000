namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.Runtime;
using Android.Util;

[Application]
public sealed class CudyApplication : Application
{
    private const string LogTag = "CudyAgent";

    public CudyApplication(IntPtr handle, JniHandleOwnership transfer)
        : base(handle, transfer)
    {
    }

    public override void OnCreate()
    {
        base.OnCreate();
        StoreProcessMarker("process_started", "");
        CudyRecoveryJobService.Schedule(this);
        AndroidEnvironment.UnhandledExceptionRaiser += (_, args) =>
            StoreCrash("android_unhandled", args.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            StoreCrash("dotnet_unhandled", args.ExceptionObject as Exception);
        TaskScheduler.UnobservedTaskException += (_, args) =>
            StoreCrash("task_unobserved", args.Exception);
    }

    private void StoreCrash(string source, Exception? exception)
    {
        var detail = exception is null
            ? "unknown exception"
            : $"{exception.GetType().Name}: {exception.Message}";
        Log.Error(LogTag, $"{source}: {detail}");
        StoreProcessMarker(source, detail);
    }

    private void StoreProcessMarker(string action, string detail)
    {
        try
        {
            var preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private);
            var count = (preferences?.GetInt("process_start_count", 0) ?? 0)
                + (action == "process_started" ? 1 : 0);
            preferences?.Edit()
                ?.PutInt("process_start_count", count)
                ?.PutString("process_last_action", action)
                ?.PutString("process_last_detail", detail)
                ?.PutString("process_last_action_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
                ?.Apply();
        }
        catch (Exception ex)
        {
            Log.Error(LogTag, "Failed to store process marker: " + ex.Message);
        }
    }
}
