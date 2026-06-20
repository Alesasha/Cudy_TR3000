namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.Net;
using Android.OS;
using Android.Provider;
using Android.Widget;
using System.Text;

[Activity(
    Name = "com.nashvpn.cudyagent.MainActivity",
    Label = "@string/app_name",
    MainLauncher = true,
    Exported = true)]
public class MainActivity : Activity
{
    private const int VpnPrepareRequest = 1001;
    private EditText? controlUrlInput;
    private EditText? deviceIdInput;
    private EditText? tokenInput;
    private EditText? sshHostInput;
    private EditText? sshUserInput;
    private EditText? sshKeyInput;
    private TextView? statusText;
    private TextView? serviceStatusText;
    private TextView? policyStatusText;
    private TextView? probeStatusText;
    private TextView? outputText;
    private ISharedPreferences? preferences;
    private bool? pendingStartAfterPrepare;
    private string pendingDebugProbeUrl = "";
    private string pendingDebugProbeCandidates = "";

    protected override void OnCreate(Bundle? savedInstanceState)
    {
        base.OnCreate(savedInstanceState);

        SetContentView(Resource.Layout.activity_main);
        preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private)
            ?? throw new InvalidOperationException("Preferences are unavailable.");

        controlUrlInput = FindViewById<EditText>(Resource.Id.controlUrlInput);
        deviceIdInput = FindViewById<EditText>(Resource.Id.deviceIdInput);
        tokenInput = FindViewById<EditText>(Resource.Id.tokenInput);
        sshHostInput = FindViewById<EditText>(Resource.Id.sshHostInput);
        sshUserInput = FindViewById<EditText>(Resource.Id.sshUserInput);
        sshKeyInput = FindViewById<EditText>(Resource.Id.sshKeyInput);
        statusText = FindViewById<TextView>(Resource.Id.statusText);
        serviceStatusText = FindViewById<TextView>(Resource.Id.serviceStatusText);
        policyStatusText = FindViewById<TextView>(Resource.Id.policyStatusText);
        probeStatusText = FindViewById<TextView>(Resource.Id.probeStatusText);
        outputText = FindViewById<TextView>(Resource.Id.outputText);
        if (controlUrlInput is null || deviceIdInput is null || tokenInput is null
            || sshHostInput is null || sshUserInput is null || sshKeyInput is null
            || statusText is null || serviceStatusText is null || policyStatusText is null
            || probeStatusText is null || outputText is null)
        {
            throw new InvalidOperationException("Required layout controls are missing.");
        }

        controlUrlInput.Text = preferences.GetString("control_url", "http://127.0.0.1:18765");
        deviceIdInput.Text = preferences.GetString("device_id", "isasha_X7Pro_Cudy-android");
        tokenInput.Text = preferences.GetString("token", "");
        sshHostInput.Text = preferences.GetString("ssh_host", "95.182.91.203");
        sshUserInput.Text = preferences.GetString("ssh_user", "cudy-tunnel-windows");
        sshKeyInput.Text = preferences.GetString("ssh_key", "");
        statusText.Text = "Ready";

        RequireButton(Resource.Id.saveButton).Click += (_, _) => SaveSettings();
        RequireButton(Resource.Id.setupPermissionsButton).Click += (_, _) => SetupBackgroundPermissions();
        RequireButton(Resource.Id.fetchButton).Click += async (_, _) => await FetchPolicyAsync();
        RequireButton(Resource.Id.checkButton).Click += async (_, _) => await CheckControlAsync();
        RequireButton(Resource.Id.prepareButton).Click += (_, _) => PrepareVpn();
        RequireButton(Resource.Id.startButton).Click += (_, _) => StartAgent(controlOnly: false);
        RequireButton(Resource.Id.stopButton).Click += (_, _) => StopAgent();
        ApplyIntentSettings(Intent);
        RenderStoredStatus();
        MaybePromptBackgroundPermissions();
    }

    protected override void OnNewIntent(Intent? intent)
    {
        base.OnNewIntent(intent);
        Intent = intent;
        ApplyIntentSettings(intent);
        RenderStoredStatus();
    }

    protected override void OnResume()
    {
        base.OnResume();
        RenderStoredStatus();
    }

    private Button RequireButton(int resourceId)
    {
        return FindViewById<Button>(resourceId) ?? throw new InvalidOperationException($"Button {resourceId} is missing.");
    }

    private void SaveSettings()
    {
        SaveSettingsToPreferences();
        if (statusText is not null)
        {
            statusText.Text = "Settings saved";
        }
        RenderStoredStatus();
    }

    private void SaveSettingsToPreferences()
    {
        var editor = preferences?.Edit() ?? throw new InvalidOperationException("Preferences are unavailable.");
        editor.PutString("control_url", controlUrlInput?.Text?.Trim() ?? "");
        editor.PutString("device_id", deviceIdInput?.Text?.Trim() ?? "");
        editor.PutString("token", tokenInput?.Text ?? "");
        editor.PutString("ssh_host", sshHostInput?.Text?.Trim() ?? "");
        editor.PutString("ssh_user", sshUserInput?.Text?.Trim() ?? "");
        editor.PutString("ssh_key", sshKeyInput?.Text ?? "");
        editor.Apply();
    }

    private async void ApplyIntentSettings(Intent? intent)
    {
        if (intent?.Extras is null)
        {
            return;
        }

        var changed = false;
        changed |= ApplyExtra(intent, "control_url", controlUrlInput);
        changed |= ApplyExtra(intent, "device_id", deviceIdInput);
        changed |= ApplyExtra(intent, "token", tokenInput);
        changed |= ApplyExtra(intent, "ssh_host", sshHostInput);
        changed |= ApplyExtra(intent, "ssh_user", sshUserInput);
        changed |= ApplyBase64Extra(intent, "ssh_key_b64", sshKeyInput);
        pendingDebugProbeUrl = intent.GetStringExtra("debug_probe_url") ?? "";
        pendingDebugProbeCandidates = intent.GetStringExtra("debug_probe_candidates") ?? "";
        if (changed)
        {
            SaveSettingsToPreferences();
            statusText!.Text = "Settings imported";
        }

        if (intent.GetBooleanExtra("fetch_policy", false))
        {
            await FetchPolicyAsync();
        }

        if (intent.GetBooleanExtra("start_agent", false))
        {
            StartAgent(intent.GetBooleanExtra("control_only", false));
        }
    }

    private static bool ApplyExtra(Intent intent, string key, EditText? target)
    {
        var value = intent.GetStringExtra(key);
        if (target is null || value is null)
        {
            return false;
        }
        target.Text = value;
        return true;
    }

    private static bool ApplyBase64Extra(Intent intent, string key, EditText? target)
    {
        var value = intent.GetStringExtra(key);
        if (target is null || string.IsNullOrWhiteSpace(value))
        {
            return false;
        }
        target.Text = Encoding.UTF8.GetString(Convert.FromBase64String(value));
        return true;
    }

    private async Task FetchPolicyAsync()
    {
        SaveSettings();
        try
        {
            var json = HasSshSettings()
                ? await Task.Run(() => CudySshControl.RunCurlWithNewClient(
                    InputText(sshHostInput).Trim(),
                    InputText(sshUserInput).Trim(),
                    InputText(sshKeyInput),
                    "GET",
                    InputText(tokenInput),
                    "/api/agent/config",
                    body: null))
                : await FetchHttpStringAsync("/api/agent/config", useAuth: true);
            var summary = CudyPolicy.Summarize(json);
            preferences?.Edit()
                ?.PutString("last_policy_summary", summary)
                ?.PutString("last_policy_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
                ?.Apply();
            outputText!.Text = summary;
            statusText!.Text = "Policy fetched";
        }
        catch (Exception ex)
        {
            statusText!.Text = "Fetch failed";
            outputText!.Text = ex.Message;
        }
        RenderStoredStatus();
    }

    private async Task CheckControlAsync()
    {
        SaveSettings();
        try
        {
            var reply = HasSshSettings()
                ? await Task.Run(() => CudySshControl.RunCurlWithNewClient(
                    InputText(sshHostInput).Trim(),
                    InputText(sshUserInput).Trim(),
                    InputText(sshKeyInput),
                    "GET",
                    token: null,
                    "/healthz",
                    body: null))
                : await FetchHttpStringAsync("/healthz", useAuth: false);
            statusText!.Text = "Control reachable";
            outputText!.Text = reply.Trim();
        }
        catch (Exception ex)
        {
            statusText!.Text = "Control check failed";
            outputText!.Text = ex.Message;
        }
    }

    private bool HasSshSettings()
    {
        return !string.IsNullOrWhiteSpace(InputText(sshHostInput))
            && !string.IsNullOrWhiteSpace(InputText(sshUserInput))
            && !string.IsNullOrWhiteSpace(InputText(sshKeyInput));
    }

    private async Task<string> FetchHttpStringAsync(string path, bool useAuth)
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        if (useAuth)
        {
            client.DefaultRequestHeaders.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", tokenInput!.Text);
        }
        var url = (controlUrlInput!.Text ?? "").Trim().TrimEnd('/') + path;
        return await client.GetStringAsync(url);
    }

    private static string InputText(EditText? input)
    {
        return input?.Text ?? "";
    }

    private void MaybePromptBackgroundPermissions()
    {
        if (preferences?.GetBoolean("background_permissions_prompt_shown", false) == true)
        {
            return;
        }

        preferences?.Edit()?.PutBoolean("background_permissions_prompt_shown", true)?.Apply();
        var dialog = new AlertDialog.Builder(this);
        dialog.SetTitle("Background permissions");
        dialog.SetMessage("For automatic reconnect after phone reboot, allow battery unrestricted mode and enable Autostart for Cudy Agent.");
        dialog.SetPositiveButton("Setup", (_, _) => SetupBackgroundPermissions());
        dialog.SetNegativeButton("Later", (_, _) => { });
        dialog.Show();
    }

    private void SetupBackgroundPermissions()
    {
        if (!IsIgnoringBatteryOptimizations())
        {
            if (OpenBatteryOptimizationRequest())
            {
                statusText!.Text = "Battery permission requested";
                outputText!.Text = "Allow battery unrestricted mode, then tap Setup permissions again to open Autostart.";
                return;
            }
        }

        if (OpenMiuiAutostartSettings())
        {
            statusText!.Text = "Autostart settings opened";
            outputText!.Text = "Enable Autostart for Cudy Agent. If this screen is not available, use app settings and set Battery saver to No restrictions.";
            return;
        }

        OpenApplicationSettings();
        statusText!.Text = "Application settings opened";
        outputText!.Text = "Set Battery saver to No restrictions and enable Autostart if your Android build exposes it.";
    }

    private bool IsIgnoringBatteryOptimizations()
    {
        if ((int)Build.VERSION.SdkInt < 23)
        {
            return true;
        }

        var powerManager = GetSystemService(PowerService) as PowerManager;
        return powerManager?.IsIgnoringBatteryOptimizations(PackageName) ?? false;
    }

    private bool OpenBatteryOptimizationRequest()
    {
        if ((int)Build.VERSION.SdkInt < 23)
        {
            return false;
        }

        try
        {
            var intent = new Intent(Settings.ActionRequestIgnoreBatteryOptimizations);
            intent.SetData(Uri.Parse($"package:{PackageName}"));
            StartActivity(intent);
            return true;
        }
        catch (Exception)
        {
            try
            {
                StartActivity(new Intent(Settings.ActionIgnoreBatteryOptimizationSettings));
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }
    }

    private bool OpenMiuiAutostartSettings()
    {
        try
        {
            var intent = new Intent();
            intent.SetComponent(new ComponentName(
                "com.miui.securitycenter",
                "com.miui.permcenter.autostart.AutoStartManagementActivity"));
            StartActivity(intent);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private void OpenApplicationSettings()
    {
        var intent = new Intent(Settings.ActionApplicationDetailsSettings);
        intent.SetData(Uri.Parse($"package:{PackageName}"));
        StartActivity(intent);
    }

    private void PrepareVpn()
    {
        var intent = Android.Net.VpnService.Prepare(this);
        if (intent != null)
        {
            StartActivityForResult(intent, VpnPrepareRequest);
            return;
        }
        statusText!.Text = "VPN permission already granted";
    }

    private void StartAgent(bool controlOnly)
    {
        SaveSettings();
        if (!controlOnly)
        {
            var prepareIntent = Android.Net.VpnService.Prepare(this);
            if (prepareIntent != null)
            {
                pendingStartAfterPrepare = controlOnly;
                preferences?.Edit()
                    ?.PutString("service_status", "waiting for Android VPN permission")
                    ?.PutString("service_status_at", DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz"))
                    ?.Apply();
                StartActivityForResult(prepareIntent, VpnPrepareRequest);
                statusText!.Text = "VPN permission required";
                RenderStoredStatus();
                return;
            }
        }

        var intent = new Intent(this, typeof(CudyVpnService));
        intent.SetAction(CudyVpnService.ActionStart);
        intent.PutExtra("control_url", controlUrlInput!.Text);
        intent.PutExtra("device_id", deviceIdInput!.Text);
        intent.PutExtra("token", tokenInput!.Text);
        intent.PutExtra("ssh_host", sshHostInput!.Text);
        intent.PutExtra("ssh_user", sshUserInput!.Text);
        intent.PutExtra("ssh_key", sshKeyInput!.Text);
        intent.PutExtra("control_only", controlOnly);
        intent.PutExtra("debug_probe_url", pendingDebugProbeUrl);
        intent.PutExtra("debug_probe_candidates", pendingDebugProbeCandidates);
        pendingDebugProbeUrl = "";
        pendingDebugProbeCandidates = "";
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
        statusText!.Text = "Agent start requested";
        RenderStoredStatus();
    }

    private void StopAgent()
    {
        var intent = new Intent(this, typeof(CudyVpnService));
        intent.SetAction(CudyVpnService.ActionStop);
        StartService(intent);
        statusText!.Text = "Agent stop requested";
        RenderStoredStatus();
    }

    protected override void OnActivityResult(int requestCode, Result resultCode, Intent? data)
    {
        base.OnActivityResult(requestCode, resultCode, data);
        if (requestCode == VpnPrepareRequest)
        {
            var pendingStart = pendingStartAfterPrepare;
            pendingStartAfterPrepare = null;
            if (resultCode == Result.Ok)
            {
                statusText!.Text = "VPN permission granted";
                if (pendingStart.HasValue)
                {
                    StartAgent(pendingStart.Value);
                }
                return;
            }

            statusText!.Text = "VPN permission denied";
        }
    }

    private void RenderStoredStatus()
    {
        if (preferences is null || serviceStatusText is null || policyStatusText is null
            || probeStatusText is null || outputText is null)
        {
            return;
        }

        var serviceStatus = preferences.GetString("service_status", "");
        var serviceAt = preferences.GetString("service_status_at", "");
        var policySummary = preferences.GetString("last_policy_summary", "");
        var policyAt = preferences.GetString("last_policy_at", "");
        var debugProbe = preferences.GetString("debug_probe_result", "");
        var debugProbeAt = preferences.GetString("debug_probe_at", "");
        serviceStatusText.Text = string.IsNullOrWhiteSpace(serviceStatus)
            ? "Service: -"
            : $"Service: {serviceStatus}";
        policyStatusText.Text = string.IsNullOrWhiteSpace(policyAt)
            ? "Policy: -"
            : $"Policy: {policyAt}";
        probeStatusText.Text = string.IsNullOrWhiteSpace(debugProbeAt)
            ? "Probe: -"
            : $"Probe: {debugProbeAt}";
        var lines = new List<string>();
        if (!string.IsNullOrWhiteSpace(serviceStatus))
        {
            lines.Add($"service: {serviceStatus}");
            if (!string.IsNullOrWhiteSpace(serviceAt))
            {
                lines.Add($"service_at: {serviceAt}");
            }
        }
        if (!string.IsNullOrWhiteSpace(policySummary))
        {
            if (lines.Count > 0)
            {
                lines.Add("");
            }
            lines.Add("last_policy:");
            if (!string.IsNullOrWhiteSpace(policyAt))
            {
                lines.Add($"policy_at: {policyAt}");
            }
            lines.Add(policySummary);
        }
        if (!string.IsNullOrWhiteSpace(debugProbe))
        {
            if (lines.Count > 0)
            {
                lines.Add("");
            }
            lines.Add("last_probe:");
            if (!string.IsNullOrWhiteSpace(debugProbeAt))
            {
                lines.Add($"probe_at: {debugProbeAt}");
            }
            lines.Add(debugProbe);
        }
        if (lines.Count > 0)
        {
            outputText.Text = string.Join("\n", lines);
        }
    }
}
