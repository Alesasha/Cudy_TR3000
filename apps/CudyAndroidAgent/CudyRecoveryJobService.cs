namespace CudyAndroidAgent;

using Android.App;
using Android.App.Job;
using Android.Content;
using Android.OS;
using Android.Util;

[Service(
    Name = "com.nashvpn.cudyagent.CudyRecoveryJobService",
    Permission = "android.permission.BIND_JOB_SERVICE",
    Exported = true)]
public sealed class CudyRecoveryJobService : JobService
{
    private const int RecoveryJobIdA = 24062;
    private const int RecoveryJobIdB = 24063;
    private const string LogTag = "CudyAgent";
    private const long RecoveryDelayMilliseconds = 2 * 60 * 1000;
    private const long RecoveryDeadlineMilliseconds = 4 * 60 * 1000;
    private const long StalledControlLoopMilliseconds = 10 * 60 * 1000;

    public static void Schedule(Context context)
    {
        try
        {
            var scheduler = context.GetSystemService(Context.JobSchedulerService) as JobScheduler;
            if (scheduler is null)
            {
                return;
            }
            var pendingA = scheduler.GetPendingJob(RecoveryJobIdA);
            var pendingB = scheduler.GetPendingJob(RecoveryJobIdB);
            if (pendingB is not null || pendingA is not null && !pendingA.IsPeriodic)
            {
                return;
            }
            if (pendingA?.IsPeriodic == true)
            {
                scheduler.Cancel(RecoveryJobIdA);
            }
            ScheduleJob(context, scheduler, RecoveryJobIdA);
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "Failed to schedule recovery job: " + ex.Message);
        }
    }

    public override bool OnStartJob(JobParameters? parameters)
    {
        ScheduleNextJob(parameters?.JobId ?? RecoveryJobIdA);

        var preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private);
        if (preferences?.GetBoolean("agent_requested_running", false) != true)
        {
            return false;
        }

        if (CudyVpnService.IsRunning)
        {
            RecoverStalledProcess(preferences);
            return false;
        }

        var now = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz");
        try
        {
            var intent = new Intent(this, typeof(CudyVpnService));
            intent.SetAction(CudyVpnService.ActionStart);
            if ((int)Build.VERSION.SdkInt >= 26)
            {
#pragma warning disable CA1416
                StartForegroundService(intent);
#pragma warning restore CA1416
            }
            else
            {
                StartService(intent);
            }
            preferences.Edit()
                ?.PutString("recovery_job_at", now)
                ?.PutString("recovery_job_result", "start-requested")
                ?.PutString("service_state", "restarting")
                ?.PutString("service_status", "recovery job requested restart")
                ?.PutString("service_status_at", now)
                ?.Apply();
            Log.Info(LogTag, "Recovery job requested agent restart.");
        }
        catch (Exception ex)
        {
            preferences?.Edit()
                ?.PutString("recovery_job_at", now)
                ?.PutString("recovery_job_result", "start-failed: " + ex.Message)
                ?.Apply();
            Log.Warn(LogTag, "Recovery job failed to restart agent: " + ex.Message);
        }
        return false;
    }

    public override bool OnStopJob(JobParameters? parameters) => false;

    private void RecoverStalledProcess(ISharedPreferences preferences)
    {
        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var heartbeatMs = preferences.GetLong("control_loop_heartbeat_ms", 0);
        if (heartbeatMs <= 0)
        {
            preferences.Edit()
                ?.PutLong("control_loop_heartbeat_ms", nowMs)
                ?.PutString("control_loop_stage", "recovery-baseline")
                ?.Apply();
            return;
        }
        var ageMs = nowMs - heartbeatMs;
        if (ageMs < StalledControlLoopMilliseconds)
        {
            return;
        }

        var now = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz");
        var stage = preferences.GetString("control_loop_stage", "unknown") ?? "unknown";
        preferences.Edit()
            ?.PutString("recovery_job_at", now)
            ?.PutString("recovery_job_result", $"stalled-process-restart: stage={stage} age_seconds={ageMs / 1000}")
            ?.PutString("service_state", "restarting")
            ?.PutString("service_status", "control loop stalled; process restart requested")
            ?.PutString("service_status_at", now)
            ?.Commit();
        Log.Error(LogTag, $"Control loop stalled at {stage} for {ageMs / 1000}s; restarting process.");
        Android.OS.Process.KillProcess(Android.OS.Process.MyPid());
    }

    private void ScheduleNextJob(int currentJobId)
    {
        try
        {
            var scheduler = GetSystemService(JobSchedulerService) as JobScheduler;
            if (scheduler is null)
            {
                return;
            }
            var nextJobId = currentJobId == RecoveryJobIdA ? RecoveryJobIdB : RecoveryJobIdA;
            if (scheduler.GetPendingJob(nextJobId) is null)
            {
                ScheduleJob(this, scheduler, nextJobId);
            }
        }
        catch (Exception ex)
        {
            Log.Warn(LogTag, "Failed to schedule next recovery job: " + ex.Message);
        }
    }

    private static void ScheduleJob(Context context, JobScheduler scheduler, int jobId)
    {
        var serviceClass = Java.Lang.Class.FromType(typeof(CudyRecoveryJobService))
            ?? throw new InvalidOperationException("Recovery job service class is unavailable.");
        var component = new ComponentName(context, serviceClass);
        var builder = new JobInfo.Builder(jobId, component)
            ?? throw new InvalidOperationException("Recovery job builder is unavailable.");
        builder.SetPersisted(true);
        builder.SetRequiredNetworkType(NetworkType.Any);
        builder.SetMinimumLatency(RecoveryDelayMilliseconds);
        builder.SetOverrideDeadline(RecoveryDeadlineMilliseconds);
        var job = builder.Build()
            ?? throw new InvalidOperationException("Recovery job could not be built.");
        scheduler.Schedule(job);
    }
}
