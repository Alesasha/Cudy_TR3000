namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.Net;
using Android.OS;
using Android.Provider;
using Android.Util;
using System.Security.Cryptography;
using System.Text.Json;

public static class CudyAndroidUpdater
{
    public const string ActionInstallDownloadedUpdate = "com.nashvpn.cudyagent.INSTALL_DOWNLOADED_UPDATE";
    private const string PreferencesName = "cudy-agent";
    private const string LogTag = "CudyAgent";
    private const string UpdateChannelId = "cudy-agent-updates";
    private const int UpdateNotificationId = 24064;
    private const long MaximumApkBytes = 128L * 1024 * 1024;
    private static readonly SemaphoreSlim UpdateLock = new(1, 1);

    public static async Task<CudyUpdateResult> CheckAndDownloadAsync(
        Context context,
        bool force,
        CancellationToken cancellationToken)
    {
        if (!await UpdateLock.WaitAsync(0, cancellationToken))
        {
            return new CudyUpdateResult("busy", false, false, 0, "", "Update check is already running");
        }
        try
        {
            return await CheckAndDownloadCoreAsync(context, force, cancellationToken);
        }
        finally
        {
            UpdateLock.Release();
        }
    }

    public static bool HasDownloadedUpdate(Context context)
    {
        var preferences = context.GetSharedPreferences(PreferencesName, FileCreationMode.Private);
        var path = preferences?.GetString("update_downloaded_path", "") ?? "";
        var versionCode = preferences?.GetLong("update_downloaded_version_code", 0) ?? 0;
        return versionCode > CurrentVersionCode(context) && File.Exists(path);
    }

    public static string DownloadedVersionName(Context context) =>
        context.GetSharedPreferences(PreferencesName, FileCreationMode.Private)
            ?.GetString("update_downloaded_version_name", "") ?? "";

    public static void CleanupInstalledUpdate(Context context)
    {
        var preferences = context.GetSharedPreferences(PreferencesName, FileCreationMode.Private);
        var path = preferences?.GetString("update_downloaded_path", "") ?? "";
        if (!string.IsNullOrWhiteSpace(path))
        {
            try
            {
                File.Delete(path);
                var directory = Path.GetDirectoryName(path);
                if (!string.IsNullOrWhiteSpace(directory) && Directory.Exists(directory))
                {
                    DeleteOldUpdates(directory, "");
                }
            }
            catch (Exception ex)
            {
                Log.Warn(LogTag, "Installed update cleanup failed: " + ex.Message);
            }
        }
        preferences?.Edit()
            ?.Remove("update_downloaded_version_code")
            ?.Remove("update_downloaded_version_name")
            ?.Remove("update_downloaded_path")
            ?.Remove("update_downloaded_sha256")
            ?.PutBoolean("update_install_pending", false)
            ?.Apply();
    }

    public static void ReconcileInstalledUpdate(Context context)
    {
        var preferences = context.GetSharedPreferences(PreferencesName, FileCreationMode.Private);
        if (preferences is null)
        {
            return;
        }
        var currentVersionCode = CurrentVersionCode(context);
        var currentVersionName = CurrentVersionName(context);
        var downloadedVersionCode = preferences.GetLong("update_downloaded_version_code", 0);
        var latestVersionCode = preferences.GetLong("update_latest_version_code", 0);
        var updateStatus = preferences.GetString("update_status", "") ?? "";
        var downloadedIsInstalled = downloadedVersionCode > 0 && downloadedVersionCode <= currentVersionCode;
        var obsoleteTransientState = updateStatus is "checking"
            or "downloading"
            or "ready"
            or "awaiting-confirmation"
            or "install-requested";
        var latestIsNotNewer = latestVersionCode > 0
            && (latestVersionCode < currentVersionCode
                || (latestVersionCode == currentVersionCode && obsoleteTransientState));
        if (!downloadedIsInstalled && !latestIsNotNewer)
        {
            return;
        }

        if (downloadedIsInstalled)
        {
            CleanupInstalledUpdate(context);
        }
        var updatesDir = Path.Combine(context.FilesDir?.AbsolutePath ?? "", "updates");
        if (latestIsNotNewer && Directory.Exists(updatesDir))
        {
            try
            {
                DeleteOldUpdates(updatesDir, "");
            }
            catch (Exception ex)
            {
                Log.Warn(LogTag, "Stale update cleanup failed: " + ex.Message);
            }
        }
        preferences.Edit()
            ?.PutLong("update_latest_version_code", currentVersionCode)
            ?.PutString("update_latest_version_name", currentVersionName)
            ?.Remove("update_latest_sha256")
            ?.PutLong("update_downloaded_bytes", 0)
            ?.PutLong("update_total_bytes", 0)
            ?.PutBoolean("update_install_pending", false)
            ?.PutString("update_status", "up-to-date")
            ?.PutString("update_error", "")
            ?.Apply();
        (context.GetSystemService(Context.NotificationService) as NotificationManager)
            ?.Cancel(UpdateNotificationId);
    }

    public static CudyUpdateInstallResult BeginInstall(Activity activity)
    {
        var preferences = activity.GetSharedPreferences(PreferencesName, FileCreationMode.Private);
        var apkPath = preferences?.GetString("update_downloaded_path", "") ?? "";
        var versionCode = preferences?.GetLong("update_downloaded_version_code", 0) ?? 0;
        if (versionCode <= CurrentVersionCode(activity) || !File.Exists(apkPath))
        {
            return new CudyUpdateInstallResult(false, false, "Downloaded update is unavailable");
        }

        if ((int)Build.VERSION.SdkInt >= 26
            && activity.PackageManager?.CanRequestPackageInstalls() != true)
        {
            preferences?.Edit()?.PutBoolean("update_install_pending", true)?.Apply();
            var settings = new Intent(
                Settings.ActionManageUnknownAppSources,
                Uri.Parse("package:" + activity.PackageName));
            activity.StartActivity(settings);
            return new CudyUpdateInstallResult(false, true, "Allow Cudy Agent to install this update, then return to the app");
        }

        var installer = activity.PackageManager?.PackageInstaller
            ?? throw new InvalidOperationException("Android package installer is unavailable.");
        var parameters = new PackageInstaller.SessionParams(PackageInstallMode.FullInstall);
        parameters.SetAppPackageName(activity.PackageName);
        var sessionId = installer.CreateSession(parameters);
        using var session = installer.OpenSession(sessionId);
        using (var input = File.OpenRead(apkPath))
        using (var output = session.OpenWrite("base.apk", 0, input.Length))
        {
            input.CopyTo(output);
            session.Fsync(output);
        }

        var statusIntent = new Intent(activity, typeof(CudyUpdateInstallReceiver));
        statusIntent.SetAction(CudyUpdateInstallReceiver.ActionInstallStatus);
        var flags = PendingIntentFlags.UpdateCurrent;
        if ((int)Build.VERSION.SdkInt >= 31)
        {
            flags |= PendingIntentFlags.Mutable;
        }
        var pendingIntent = PendingIntent.GetBroadcast(activity, sessionId, statusIntent, flags)
            ?? throw new InvalidOperationException("Update status callback is unavailable.");
        preferences?.Edit()
            ?.PutBoolean("update_install_pending", false)
            ?.PutString("update_status", "install-requested")
            ?.PutString("update_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
            ?.Apply();
        session.Commit(pendingIntent.IntentSender);
        return new CudyUpdateInstallResult(true, false, "Android installer opened");
    }

    public static void NotifyUpdateReady(Context context, string versionName)
    {
        var manager = context.GetSystemService(Context.NotificationService) as NotificationManager;
        if (manager is null)
        {
            return;
        }
        if ((int)Build.VERSION.SdkInt >= 26)
        {
            var channel = new NotificationChannel(UpdateChannelId, "Cudy Agent updates", NotificationImportance.Default)
            {
                Description = "New verified Cudy Agent versions",
            };
            manager.CreateNotificationChannel(channel);
        }
        var intent = new Intent(context, typeof(MainActivity));
        intent.SetAction(ActionInstallDownloadedUpdate);
        intent.AddFlags(ActivityFlags.ClearTop | ActivityFlags.SingleTop);
        var pendingFlags = PendingIntentFlags.UpdateCurrent;
        if ((int)Build.VERSION.SdkInt >= 23)
        {
            pendingFlags |= PendingIntentFlags.Immutable;
        }
        var pending = PendingIntent.GetActivity(context, UpdateNotificationId, intent, pendingFlags);
        var builder = (int)Build.VERSION.SdkInt >= 26
            ? new Notification.Builder(context, UpdateChannelId)
            : new Notification.Builder(context);
        var notification = builder
            .SetContentTitle($"Cudy Agent {versionName} is ready")
            .SetContentText("Tap to install the verified update")
            .SetSmallIcon(Android.Resource.Drawable.StatSysDownloadDone)
            .SetAutoCancel(true)
            .SetContentIntent(pending)
            .Build();
        manager.Notify(UpdateNotificationId, notification);
    }

    private static async Task<CudyUpdateResult> CheckAndDownloadCoreAsync(
        Context context,
        bool force,
        CancellationToken cancellationToken)
    {
        ReconcileInstalledUpdate(context);
        var preferences = context.GetSharedPreferences(PreferencesName, FileCreationMode.Private)
            ?? throw new InvalidOperationException("Preferences are unavailable.");
        var nowMs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        var lastCheckMs = preferences.GetLong("update_last_check_ms", 0);
        if (!force && nowMs - lastCheckMs < TimeSpan.FromHours(6).TotalMilliseconds)
        {
            return new CudyUpdateResult("not-due", false, HasDownloadedUpdate(context), 0, DownloadedVersionName(context), "Next automatic check is not due yet");
        }
        preferences.Edit()
            ?.PutLong("update_last_check_ms", nowMs)
            ?.PutString("update_status", "checking")
            ?.PutString("update_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
            ?.Apply();

        var token = preferences.GetString("token", "") ?? "";
        var host = preferences.GetString("ssh_host", "") ?? "";
        var user = preferences.GetString("ssh_user", "") ?? "";
        var privateKey = preferences.GetString("ssh_key", "") ?? "";
        var hostKey = preferences.GetString("ssh_host_key_sha256", "") ?? "";
        if (string.IsNullOrWhiteSpace(token)
            || string.IsNullOrWhiteSpace(host)
            || string.IsNullOrWhiteSpace(user)
            || string.IsNullOrWhiteSpace(privateKey))
        {
            SaveFailure(preferences, "not-configured", "Agent activation is incomplete");
            return new CudyUpdateResult("not-configured", false, false, 0, "", "Agent activation is incomplete");
        }

        try
        {
            var manifestJson = await Task.Run(() => CudySshControl.RunCurlWithNewClient(
                host,
                user,
                privateKey,
                hostKey,
                "GET",
                token,
                "/api/agent/app-version?platform=android",
                null), cancellationToken);
            using var document = JsonDocument.Parse(manifestJson);
            var root = document.RootElement;
            var versionCode = root.TryGetProperty("version_code", out var code) ? code.GetInt64() : 0;
            var versionName = root.TryGetProperty("version_name", out var name) ? name.GetString() ?? "" : "";
            var sha256 = root.TryGetProperty("sha256", out var hash) ? hash.GetString() ?? "" : "";
            var downloadUrl = root.TryGetProperty("download_url", out var url) ? url.GetString() ?? "" : "";
            preferences.Edit()
                ?.PutLong("update_latest_version_code", versionCode)
                ?.PutString("update_latest_version_name", versionName)
                ?.PutString("update_latest_sha256", sha256)
                ?.Apply();
            if (versionCode <= CurrentVersionCode(context))
            {
                if (versionCode < CurrentVersionCode(context))
                {
                    Log.Warn(LogTag, $"Ignored stale Android update manifest: {versionName} ({versionCode})");
                }
                ReconcileInstalledUpdate(context);
                return new CudyUpdateResult(
                    "up-to-date",
                    false,
                    false,
                    CurrentVersionCode(context),
                    CurrentVersionName(context),
                    "App is up to date");
            }
            if (string.IsNullOrWhiteSpace(sha256) || sha256.Length != 64)
            {
                throw new InvalidOperationException("Update manifest has no valid SHA256");
            }
            if (!downloadUrl.StartsWith("/", StringComparison.Ordinal))
            {
                throw new InvalidOperationException("Only authenticated control-server update URLs are accepted");
            }

            var updatesDir = Path.Combine(context.FilesDir?.AbsolutePath ?? throw new InvalidOperationException("App files directory is unavailable."), "updates");
            Directory.CreateDirectory(updatesDir);
            var finalPath = Path.Combine(updatesDir, $"cudy-agent-{versionCode}.apk");
            if (!File.Exists(finalPath) || !HashMatches(finalPath, sha256))
            {
                var tempPath = finalPath + ".part";
                if (File.Exists(tempPath) && new FileInfo(tempPath).Length > MaximumApkBytes)
                {
                    File.Delete(tempPath);
                }
                preferences.Edit()
                    ?.PutString("update_status", "downloading")
                    ?.PutString("update_error", "")
                    ?.Apply();
                var lastProgressUpdate = 0L;
                void SaveProgress(long downloaded, long total)
                {
                    var now = System.Environment.TickCount64;
                    if (downloaded < total && now - lastProgressUpdate < 500)
                    {
                        return;
                    }
                    lastProgressUpdate = now;
                    preferences.Edit()
                        ?.PutLong("update_downloaded_bytes", downloaded)
                        ?.PutLong("update_total_bytes", total)
                        ?.PutString("update_status", "downloading")
                        ?.Apply();
                }
                await Task.Run(() => CudySshControl.DownloadWithNewClient(
                    host,
                    user,
                    privateKey,
                    hostKey,
                    token,
                    downloadUrl,
                    tempPath,
                    cancellationToken,
                    SaveProgress), cancellationToken);
                var length = new FileInfo(tempPath).Length;
                if (length <= 0 || length > MaximumApkBytes)
                {
                    throw new InvalidOperationException($"Downloaded APK has invalid size: {length}");
                }
                if (!HashMatches(tempPath, sha256))
                {
                    File.Delete(tempPath);
                    throw new InvalidOperationException("Downloaded APK SHA256 does not match the signed manifest");
                }
                VerifyPackage(context, tempPath, versionCode);
                File.Move(tempPath, finalPath, overwrite: true);
            }

            DeleteOldUpdates(updatesDir, finalPath);
            preferences.Edit()
                ?.PutLong("update_downloaded_version_code", versionCode)
                ?.PutString("update_downloaded_version_name", versionName)
                ?.PutString("update_downloaded_path", finalPath)
                ?.PutString("update_downloaded_sha256", sha256.ToLowerInvariant())
                ?.PutLong("update_downloaded_bytes", new FileInfo(finalPath).Length)
                ?.PutLong("update_total_bytes", new FileInfo(finalPath).Length)
                ?.PutString("update_status", "ready")
                ?.PutString("update_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
                ?.Apply();
            NotifyUpdateReady(context, versionName);
            Log.Info(LogTag, $"Verified Android update downloaded: {versionName} ({versionCode})");
            return new CudyUpdateResult("ready", true, true, versionCode, versionName, "Verified update is ready to install");
        }
        catch (Exception ex) when (ex is not OperationCanceledException || !cancellationToken.IsCancellationRequested)
        {
            SaveFailure(preferences, "failed", ex.Message);
            Log.Warn(LogTag, "Automatic update failed: " + ex.Message);
            return new CudyUpdateResult("failed", false, HasDownloadedUpdate(context), 0, "", ex.Message);
        }
    }

    private static long CurrentVersionCode(Context context)
    {
        var packageInfo = context.PackageManager?.GetPackageInfo(context.PackageName ?? "", PackageInfoFlags.Signatures)
            ?? throw new InvalidOperationException("Installed package info is unavailable.");
        return (int)Build.VERSION.SdkInt >= 28 ? packageInfo.LongVersionCode : packageInfo.VersionCode;
    }

    private static string CurrentVersionName(Context context)
    {
        var packageInfo = context.PackageManager?.GetPackageInfo(context.PackageName ?? "", 0)
            ?? throw new InvalidOperationException("Installed package info is unavailable.");
        return packageInfo.VersionName ?? CurrentVersionCode(context).ToString();
    }

    private static void VerifyPackage(Context context, string path, long expectedVersionCode)
    {
        var manager = context.PackageManager ?? throw new InvalidOperationException("Package manager is unavailable.");
        var archive = manager.GetPackageArchiveInfo(path, PackageInfoFlags.Signatures)
            ?? throw new InvalidOperationException("Downloaded file is not a readable Android package");
        var installed = manager.GetPackageInfo(context.PackageName ?? "", PackageInfoFlags.Signatures)
            ?? throw new InvalidOperationException("Installed package info is unavailable");
        var archiveCode = (int)Build.VERSION.SdkInt >= 28 ? archive.LongVersionCode : archive.VersionCode;
        if (!string.Equals(archive.PackageName, context.PackageName, StringComparison.Ordinal)
            || archiveCode != expectedVersionCode)
        {
            throw new InvalidOperationException("Downloaded APK package name or version does not match the manifest");
        }
        var archiveSignatures = SignatureHashes(archive);
        var installedSignatures = SignatureHashes(installed);
        if (archiveSignatures.Count == 0 || !archiveSignatures.SetEquals(installedSignatures))
        {
            throw new InvalidOperationException("Downloaded APK signing certificate does not match the installed app");
        }
    }

    private static HashSet<string> SignatureHashes(PackageInfo packageInfo)
    {
        var result = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var signature in packageInfo.Signatures ?? Array.Empty<Signature>())
        {
            result.Add(Convert.ToHexString(SHA256.HashData(signature.ToByteArray())));
        }
        return result;
    }

    private static bool HashMatches(string path, string expected) =>
        string.Equals(
            Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))),
            expected,
            StringComparison.OrdinalIgnoreCase);

    private static void DeleteOldUpdates(string directory, string keepPath)
    {
        foreach (var path in Directory.EnumerateFiles(directory, "*.apk"))
        {
            if (!string.Equals(path, keepPath, StringComparison.OrdinalIgnoreCase))
            {
                File.Delete(path);
            }
        }
        foreach (var path in Directory.EnumerateFiles(directory, "*.part"))
        {
            File.Delete(path);
        }
    }

    private static void SaveFailure(ISharedPreferences preferences, string state, string error)
    {
        preferences.Edit()
            ?.PutString("update_status", state)
            ?.PutString("update_error", error)
            ?.PutString("update_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
            ?.Apply();
    }
}

public sealed record CudyUpdateResult(
    string State,
    bool DownloadedNow,
    bool ReadyToInstall,
    long VersionCode,
    string VersionName,
    string Message);

public sealed record CudyUpdateInstallResult(bool Started, bool PermissionRequired, string Message);
