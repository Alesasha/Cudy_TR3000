namespace CudyAndroidAgent;

using Android.App;
using Android.App.Job;
using Android.Content;
using Android.Util;

[Service(
    Name = "com.nashvpn.cudyagent.CudyUpdateJobService",
    Permission = "android.permission.BIND_JOB_SERVICE",
    Exported = true)]
public sealed class CudyUpdateJobService : JobService
{
    private const int PeriodicJobId = 24065;
    private const int ImmediateJobId = 24066;
    private const string LogTag = "CudyAgent";
    private CancellationTokenSource? runningCts;

    public static void Schedule(Context context, bool immediate = false)
    {
        var scheduler = context.GetSystemService(Context.JobSchedulerService) as JobScheduler;
        if (scheduler is null)
        {
            return;
        }
        if (scheduler.GetPendingJob(PeriodicJobId) is null)
        {
            var periodic = Build(context, PeriodicJobId)
                .SetPeriodic(6 * 60 * 60 * 1000L, 60 * 60 * 1000L)
                .Build();
            scheduler.Schedule(periodic);
        }
        if (immediate && scheduler.GetPendingJob(ImmediateJobId) is null)
        {
            var once = Build(context, ImmediateJobId)
                .SetMinimumLatency(30 * 1000L)
                .SetOverrideDeadline(5 * 60 * 1000L)
                .Build();
            scheduler.Schedule(once);
        }
    }

    public override bool OnStartJob(JobParameters? parameters)
    {
        runningCts?.Cancel();
        runningCts = new CancellationTokenSource(TimeSpan.FromMinutes(15));
        var cancellationToken = runningCts.Token;
        _ = Task.Run(() => RunAsync(parameters, cancellationToken));
        return true;
    }

    public override bool OnStopJob(JobParameters? parameters)
    {
        runningCts?.Cancel();
        runningCts = null;
        return true;
    }

    private async Task RunAsync(JobParameters? parameters, CancellationToken cancellationToken)
    {
        try
        {
            var result = await CudyAndroidUpdater.CheckAndDownloadAsync(
                this,
                force: parameters?.JobId == ImmediateJobId,
                cancellationToken);
            Log.Info(LogTag, $"Automatic update job: state={result.State} version={result.VersionName}");
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            Log.Warn(LogTag, "Automatic update job failed: " + ex.Message);
        }
        finally
        {
            JobFinished(parameters, wantsReschedule: false);
            runningCts?.Dispose();
            runningCts = null;
        }
    }

    private static JobInfo.Builder Build(Context context, int jobId)
    {
        var serviceClass = Java.Lang.Class.FromType(typeof(CudyUpdateJobService))
            ?? throw new InvalidOperationException("Update job service class is unavailable.");
        return new JobInfo.Builder(jobId, new ComponentName(context, serviceClass))
            .SetPersisted(true)
            .SetRequiredNetworkType(NetworkType.Unmetered);
    }
}
