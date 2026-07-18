namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.Content.PM;
using Android.Net;
using Android.OS;
using Android.Provider;
using Android.Widget;
using System.Net;
using System.Text;
using System.Text.Json;

[Activity(
    Name = "com.nashvpn.cudyagent.MainActivity",
    Label = "@string/app_name",
    MainLauncher = true,
    Exported = true)]
public class MainActivity : Activity
{
    private const int VpnPrepareRequest = 1001;
    private const int NotificationPermissionRequest = 1002;
    private const string PostNotificationsPermission = "android.permission.POST_NOTIFICATIONS";
    private const string EnrollmentBootstrapAsset = "android_enrollment_bootstrap_ed25519";
    private const string EnrollmentBootstrapHost = "95.182.91.203";
    private const string EnrollmentBootstrapUser = "cudy-enroll";
    private const string EnrollmentBootstrapHostKey = "SHA256:iyONyymHdd2Fwun5GIxKFo7eh4sooHpK1hdtLZOmGTM";
    private const uint EnrollmentBootstrapPort = 8766;
    private EditText? controlUrlInput;
    private EditText? deviceIdInput;
    private EditText? tokenInput;
    private EditText? sshHostInput;
    private EditText? sshUserInput;
    private EditText? sshHostKeyInput;
    private EditText? sshKeyInput;
    private EditText? enrollmentCodeInput;
    private EditText? defaultServerInput;
    private EditText? domainInput;
    private EditText? domainServerInput;
    private EditText? lookupInput;
    private CheckBox? autostartCheckBox;
    private TextView? statusText;
    private TextView? serviceStatusText;
    private TextView? policyStatusText;
    private TextView? probeStatusText;
    private TextView? routeStatusText;
    private TextView? transportStatusText;
    private TextView? engineStatusText;
    private TextView? permissionStatusText;
    private TextView? permissionGuideText;
    private TextView? outputText;
    private LinearLayout? diagnosticsSection;
    private LinearLayout? activationSection;
    private LinearLayout? routingSection;
    private LinearLayout? advancedSection;
    private Button? toggleDiagnosticsButton;
    private Button? toggleAdvancedButton;
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
        sshHostKeyInput = FindViewById<EditText>(Resource.Id.sshHostKeyInput);
        sshKeyInput = FindViewById<EditText>(Resource.Id.sshKeyInput);
        enrollmentCodeInput = FindViewById<EditText>(Resource.Id.enrollmentCodeInput);
        defaultServerInput = FindViewById<EditText>(Resource.Id.defaultServerInput);
        domainInput = FindViewById<EditText>(Resource.Id.domainInput);
        domainServerInput = FindViewById<EditText>(Resource.Id.domainServerInput);
        lookupInput = FindViewById<EditText>(Resource.Id.lookupInput);
        autostartCheckBox = FindViewById<CheckBox>(Resource.Id.autostartCheckBox);
        statusText = FindViewById<TextView>(Resource.Id.statusText);
        serviceStatusText = FindViewById<TextView>(Resource.Id.serviceStatusText);
        policyStatusText = FindViewById<TextView>(Resource.Id.policyStatusText);
        probeStatusText = FindViewById<TextView>(Resource.Id.probeStatusText);
        routeStatusText = FindViewById<TextView>(Resource.Id.routeStatusText);
        transportStatusText = FindViewById<TextView>(Resource.Id.transportStatusText);
        engineStatusText = FindViewById<TextView>(Resource.Id.engineStatusText);
        permissionStatusText = FindViewById<TextView>(Resource.Id.permissionStatusText);
        permissionGuideText = FindViewById<TextView>(Resource.Id.permissionGuideText);
        outputText = FindViewById<TextView>(Resource.Id.outputText);
        diagnosticsSection = FindViewById<LinearLayout>(Resource.Id.diagnosticsSection);
        activationSection = FindViewById<LinearLayout>(Resource.Id.activationSection);
        routingSection = FindViewById<LinearLayout>(Resource.Id.routingSection);
        advancedSection = FindViewById<LinearLayout>(Resource.Id.advancedSection);
        toggleDiagnosticsButton = FindViewById<Button>(Resource.Id.toggleDiagnosticsButton);
        toggleAdvancedButton = FindViewById<Button>(Resource.Id.toggleAdvancedButton);
        if (controlUrlInput is null || deviceIdInput is null || tokenInput is null
            || sshHostInput is null || sshUserInput is null || sshHostKeyInput is null || sshKeyInput is null
            || enrollmentCodeInput is null || defaultServerInput is null || domainInput is null
            || domainServerInput is null || lookupInput is null || autostartCheckBox is null
            || statusText is null || serviceStatusText is null || policyStatusText is null
            || probeStatusText is null || routeStatusText is null || transportStatusText is null
            || engineStatusText is null || permissionStatusText is null || permissionGuideText is null
            || outputText is null || diagnosticsSection is null || activationSection is null
            || routingSection is null || advancedSection is null || toggleDiagnosticsButton is null
            || toggleAdvancedButton is null)
        {
            throw new InvalidOperationException("Required layout controls are missing.");
        }

        controlUrlInput.Text = preferences.GetString("control_url", "");
        deviceIdInput.Text = preferences.GetString("device_id", "");
        tokenInput.Text = preferences.GetString("token", "");
        sshHostInput.Text = preferences.GetString("ssh_host", "");
        sshUserInput.Text = preferences.GetString("ssh_user", "");
        sshHostKeyInput.Text = preferences.GetString("ssh_host_key_sha256", "");
        sshKeyInput.Text = preferences.GetString("ssh_key", "");
        if (string.IsNullOrWhiteSpace(tokenInput.Text)
            && string.Equals(deviceIdInput.Text, "isasha_X7Pro_Cudy-android", StringComparison.Ordinal))
        {
            controlUrlInput.Text = "";
            deviceIdInput.Text = "";
            sshHostInput.Text = "";
            sshUserInput.Text = "";
            sshHostKeyInput.Text = "";
            sshKeyInput.Text = "";
            preferences.Edit()
                ?.Remove("control_url")
                ?.Remove("device_id")
                ?.Remove("ssh_host")
                ?.Remove("ssh_user")
                ?.Remove("ssh_host_key_sha256")
                ?.Remove("ssh_key")
                ?.Apply();
        }
        defaultServerInput.Text = preferences.GetString("last_default_server_id", "auto");
        autostartCheckBox.Checked = preferences.GetBoolean("autostart_enabled", true);
        statusText.Text = "Ready";

        RequireButton(Resource.Id.saveButton).Click += (_, _) => SaveSettings();
        RequireButton(Resource.Id.setupPermissionsButton).Click += (_, _) => SetupBackgroundPermissions();
        RequireButton(Resource.Id.statusButton).Click += (_, _) => RenderStoredStatus();
        RequireButton(Resource.Id.updateButton).Click += async (_, _) => await CheckUpdateAsync();
        RequireButton(Resource.Id.adminButton).Click += (_, _) =>
            StartActivity(new Intent(this, typeof(AdminActivity)));
        RequireButton(Resource.Id.enrollButton).Click += async (_, _) => await EnrollDeviceAsync();
        RequireButton(Resource.Id.loadUiButton).Click += async (_, _) => await LoadUserUiAsync();
        RequireButton(Resource.Id.saveDefaultButton).Click += async (_, _) => await SaveDefaultServerAsync();
        RequireButton(Resource.Id.saveDomainButton).Click += async (_, _) => await SaveDomainRouteAsync();
        RequireButton(Resource.Id.lookupButton).Click += async (_, _) => await LookupRouteAsync();
        RequireButton(Resource.Id.fetchButton).Click += async (_, _) => await FetchPolicyAsync();
        RequireButton(Resource.Id.checkButton).Click += async (_, _) => await CheckControlAsync();
        RequireButton(Resource.Id.prepareButton).Click += (_, _) => PrepareVpn();
        RequireButton(Resource.Id.startButton).Click += (_, _) => StartAgent(controlOnly: false);
        RequireButton(Resource.Id.stopButton).Click += (_, _) => StopAgent();
        toggleDiagnosticsButton.Click += (_, _) => ToggleSection(
            diagnosticsSection,
            toggleDiagnosticsButton,
            "Show diagnostics",
            "Hide diagnostics");
        toggleAdvancedButton.Click += (_, _) => ToggleSection(
            advancedSection,
            toggleAdvancedButton,
            "Advanced settings",
            "Hide advanced settings");
        ApplyIntentSettings(Intent);
        RenderConfiguredSections();
        RenderStoredStatus();
        RenderPermissionStatus();
        MaybePromptBackgroundPermissions();
    }

    protected override void OnNewIntent(Intent? intent)
    {
        base.OnNewIntent(intent);
        Intent = intent;
        ApplyIntentSettings(intent);
        RenderStoredStatus();
        RenderPermissionStatus();
    }

    protected override void OnResume()
    {
        base.OnResume();
        RenderStoredStatus();
        RenderPermissionStatus();
        ConfirmMiuiAutostartIfPending();
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
        RenderConfiguredSections();
        RenderStoredStatus();
    }

    private static void ToggleSection(LinearLayout section, Button button, string collapsedText, string expandedText)
    {
        var expand = section.Visibility != Android.Views.ViewStates.Visible;
        section.Visibility = expand ? Android.Views.ViewStates.Visible : Android.Views.ViewStates.Gone;
        button.Text = expand ? expandedText : collapsedText;
    }

    private void RenderConfiguredSections()
    {
        if (activationSection is null || routingSection is null || advancedSection is null
            || toggleAdvancedButton is null)
        {
            return;
        }

        var configured = !string.IsNullOrWhiteSpace(tokenInput?.Text) && HasSshSettings();
        activationSection.Visibility = configured
            ? Android.Views.ViewStates.Gone
            : Android.Views.ViewStates.Visible;
        routingSection.Visibility = configured
            ? Android.Views.ViewStates.Visible
            : Android.Views.ViewStates.Gone;
        toggleAdvancedButton.Visibility = configured
            ? Android.Views.ViewStates.Visible
            : Android.Views.ViewStates.Gone;
        if (!configured)
        {
            advancedSection.Visibility = Android.Views.ViewStates.Gone;
            toggleAdvancedButton.Text = "Advanced settings";
        }
    }

    private void SaveSettingsToPreferences()
    {
        var editor = preferences?.Edit() ?? throw new InvalidOperationException("Preferences are unavailable.");
        editor.PutString("control_url", controlUrlInput?.Text?.Trim() ?? "");
        editor.PutString("device_id", deviceIdInput?.Text?.Trim() ?? "");
        editor.PutString("token", tokenInput?.Text ?? "");
        editor.PutString("ssh_host", sshHostInput?.Text?.Trim() ?? "");
        editor.PutString("ssh_user", sshUserInput?.Text?.Trim() ?? "");
        editor.PutString("ssh_host_key_sha256", sshHostKeyInput?.Text?.Trim() ?? "");
        editor.PutString("ssh_key", sshKeyInput?.Text ?? "");
        editor.PutBoolean("autostart_enabled", autostartCheckBox?.Checked ?? true);
        editor.Apply();
    }

    private async void ApplyIntentSettings(Intent? intent)
    {
        if (intent is null)
        {
            return;
        }

        if (intent.Extras is null)
        {
            return;
        }

        var changed = false;
        changed |= ApplyExtra(intent, "control_url", controlUrlInput);
        changed |= ApplyExtra(intent, "device_id", deviceIdInput);
        changed |= ApplyExtra(intent, "token", tokenInput);
        changed |= ApplyExtra(intent, "ssh_host", sshHostInput);
        changed |= ApplyExtra(intent, "ssh_user", sshUserInput);
        changed |= ApplyExtra(intent, "ssh_host_key_sha256", sshHostKeyInput);
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

    private static string RequiredJsonString(JsonElement root, string name)
    {
        var value = root.TryGetProperty(name, out var property) ? property.GetString() ?? "" : "";
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidOperationException($"Enrollment field is missing: {name}");
        }
        return value;
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
            var json = await ControlRequestAsync("GET", "/api/agent/config", body: null, useAuth: true);
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
            var reply = await ControlRequestAsync("GET", "/healthz", body: null, useAuth: false);
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

    private async Task<string> ControlRequestAsync(string method, string path, string? body, bool useAuth)
    {
        var token = useAuth ? InputText(tokenInput) : null;
        if (HasSshSettings())
        {
            return await Task.Run(() => CudySshControl.RunCurlWithNewClient(
                InputText(sshHostInput).Trim(),
                InputText(sshUserInput).Trim(),
                InputText(sshKeyInput),
                InputText(sshHostKeyInput).Trim(),
                method,
                token,
                path,
                body));
        }

        return method == "POST"
            ? await PostHttpStringAsync(path, body ?? "", useAuth)
            : await FetchHttpStringAsync(path, useAuth);
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

    private async Task<string> PostHttpStringAsync(string path, string body, bool useAuth)
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        if (useAuth)
        {
            client.DefaultRequestHeaders.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", tokenInput!.Text);
        }
        var url = (controlUrlInput!.Text ?? "").Trim().TrimEnd('/') + path;
        using var content = new StringContent(body, Encoding.UTF8, "application/json");
        using var response = await client.PostAsync(url, content);
        response.EnsureSuccessStatusCode();
        return await response.Content.ReadAsStringAsync();
    }

    private async Task LoadUserUiAsync()
    {
        SaveSettings();
        try
        {
            var json = await ControlRequestAsync("GET", "/api/agent/bootstrap", body: null, useAuth: true);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var user = root.GetProperty("user");
            var defaultServer = user.TryGetProperty("default_server_id", out var defaultProperty)
                ? defaultProperty.GetString() ?? "auto"
                : "auto";
            defaultServerInput!.Text = defaultServer;
            preferences?.Edit()?.PutString("last_default_server_id", defaultServer)?.Apply();
            var routes = root.TryGetProperty("routes", out var routeArray) && routeArray.ValueKind == JsonValueKind.Array
                ? routeArray.GetArrayLength()
                : 0;
            statusText!.Text = "Settings loaded";
            outputText!.Text = $"user={user.GetProperty("id").GetString()}\ndefault={defaultServer}\ndomain_routes={routes}";
        }
        catch (Exception ex)
        {
            statusText!.Text = "Load settings failed";
            outputText!.Text = ex.Message;
        }
    }

    private async Task SaveDefaultServerAsync()
    {
        SaveSettings();
        var serverId = InputText(defaultServerInput).Trim();
        if (string.IsNullOrWhiteSpace(serverId))
        {
            serverId = "auto";
        }
        try
        {
            var body = JsonSerializer.Serialize(new { server_id = serverId });
            var reply = await ControlRequestAsync("POST", "/api/agent/user-default-server", body, useAuth: true);
            preferences?.Edit()?.PutString("last_default_server_id", serverId)?.Apply();
            statusText!.Text = "Default server saved";
            outputText!.Text = reply;
        }
        catch (Exception ex)
        {
            statusText!.Text = "Save default failed";
            outputText!.Text = ex.Message;
        }
    }

    private async Task SaveDomainRouteAsync()
    {
        SaveSettings();
        var domain = InputText(domainInput).Trim();
        var serverId = InputText(domainServerInput).Trim();
        if (string.IsNullOrWhiteSpace(serverId))
        {
            serverId = "auto";
        }
        try
        {
            var body = JsonSerializer.Serialize(new { domain, server_id = serverId });
            var reply = await ControlRequestAsync("POST", "/api/agent/domain-routes", body, useAuth: true);
            statusText!.Text = "Domain route saved";
            outputText!.Text = reply;
        }
        catch (Exception ex)
        {
            statusText!.Text = "Save route failed";
            outputText!.Text = ex.Message;
        }
    }

    private async Task LookupRouteAsync()
    {
        SaveSettings();
        var target = WebUtility.UrlEncode(InputText(lookupInput).Trim());
        try
        {
            var json = await ControlRequestAsync("GET", $"/api/agent/route-lookup?target={target}", body: null, useAuth: true);
            outputText!.Text = SummarizeLookup(json);
            statusText!.Text = "Route checked";
        }
        catch (Exception ex)
        {
            statusText!.Text = "Route check failed";
            outputText!.Text = ex.Message;
        }
    }

    private async Task CheckUpdateAsync()
    {
        SaveSettings();
        try
        {
            var json = await ControlRequestAsync("GET", "/api/agent/app-version?platform=android", body: null, useAuth: true);
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            var latestCode = root.TryGetProperty("version_code", out var codeProperty)
                ? codeProperty.GetInt64()
                : 0;
            var latestName = root.TryGetProperty("version_name", out var nameProperty)
                ? nameProperty.GetString() ?? ""
                : "";
            var downloadUrl = root.TryGetProperty("download_url", out var urlProperty)
                ? urlProperty.GetString() ?? ""
                : "";
            var currentCode = CurrentVersionCode();
            if (latestCode <= currentCode)
            {
                statusText!.Text = "App is up to date";
                outputText!.Text = $"current={currentCode}\nlatest={latestCode} {latestName}";
                return;
            }

            statusText!.Text = "Update available";
            outputText!.Text = $"current={currentCode}\nlatest={latestCode} {latestName}\nurl={downloadUrl}";
            if (!string.IsNullOrWhiteSpace(downloadUrl))
            {
                StartActivity(new Intent(Intent.ActionView, Uri.Parse(downloadUrl)));
            }
        }
        catch (Exception ex)
        {
            statusText!.Text = "Update check failed";
            outputText!.Text = ex.Message;
        }
    }

    private long CurrentVersionCode()
    {
        var packageManager = PackageManager ?? throw new InvalidOperationException("Package manager is unavailable.");
        var packageName = PackageName ?? throw new InvalidOperationException("Package name is unavailable.");
#pragma warning disable CA1422
        var packageInfo = packageManager.GetPackageInfo(packageName, 0)
            ?? throw new InvalidOperationException("Package info is unavailable.");
#pragma warning restore CA1422
        if ((int)Build.VERSION.SdkInt >= 28)
        {
#pragma warning disable CA1416
            return packageInfo.LongVersionCode;
#pragma warning restore CA1416
        }
#pragma warning disable CA1422
        return packageInfo.VersionCode;
#pragma warning restore CA1422
    }

    private async Task EnrollDeviceAsync()
    {
        SaveSettings();
        var code = InputText(enrollmentCodeInput).Trim();
        if (string.IsNullOrWhiteSpace(code))
        {
            statusText!.Text = "Enrollment code required";
            outputText!.Text = "Enter the one-time activation code from the administrator.";
            return;
        }

        try
        {
            var requestedDeviceId = InputText(deviceIdInput).Trim();
            var body = JsonSerializer.Serialize(new
            {
                code,
                device_id = requestedDeviceId,
                display_name = string.IsNullOrWhiteSpace(requestedDeviceId) ? "Android phone" : requestedDeviceId,
                platform = "android",
            });
            string json;
            if (HasSshSettings())
            {
                json = await ControlRequestAsync("POST", "/api/agent/enroll", body, useAuth: false);
            }
            else
            {
                var bootstrapKey = ReadEnrollmentBootstrapKey();
                json = await Task.Run(() => CudySshControl.RunCurlWithNewClient(
                    EnrollmentBootstrapHost,
                    EnrollmentBootstrapUser,
                    bootstrapKey,
                    EnrollmentBootstrapHostKey,
                    "POST",
                    null,
                    "/api/agent/enroll",
                    body,
                    EnrollmentBootstrapPort));
            }
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (!root.TryGetProperty("token", out var tokenProperty))
            {
                throw new InvalidOperationException("Enrollment response did not contain a device token.");
            }
            var token = tokenProperty.GetString() ?? "";
            var deviceId = root.TryGetProperty("device_id", out var deviceProperty)
                ? deviceProperty.GetString() ?? requestedDeviceId
                : requestedDeviceId;
            if (root.TryGetProperty("provisioning", out var provisioning))
            {
                controlUrlInput!.Text = RequiredJsonString(provisioning, "control_url");
                sshHostInput!.Text = RequiredJsonString(provisioning, "ssh_host");
                sshUserInput!.Text = RequiredJsonString(provisioning, "ssh_user");
                sshHostKeyInput!.Text = RequiredJsonString(provisioning, "ssh_host_key_sha256");
                sshKeyInput!.Text = RequiredJsonString(provisioning, "ssh_private_key");
            }
            else if (!HasSshSettings())
            {
                throw new InvalidOperationException("Enrollment response did not contain device transport settings.");
            }
            tokenInput!.Text = token;
            deviceIdInput!.Text = deviceId;
            enrollmentCodeInput!.Text = "";
            SaveSettingsToPreferences();
            RenderConfiguredSections();
            statusText!.Text = "Device activated";
            outputText!.Text = $"device={deviceId}\nuser={root.GetProperty("user_id").GetString()}";
            await LoadUserUiAsync();
        }
        catch (Exception ex)
        {
            statusText!.Text = "Activation failed";
            outputText!.Text = ex.Message;
        }
    }

    private string ReadEnrollmentBootstrapKey()
    {
        using var stream = Assets?.Open(EnrollmentBootstrapAsset)
            ?? throw new InvalidOperationException("Enrollment bootstrap key is unavailable.");
        using var reader = new StreamReader(stream, Encoding.UTF8);
        var value = reader.ReadToEnd();
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidOperationException("Enrollment bootstrap key is empty.");
        }
        return value;
    }

    private static string SummarizeLookup(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var lines = new List<string> { $"input={root.GetProperty("input").GetString()}" };
        if (root.TryGetProperty("results", out var results) && results.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in results.EnumerateArray())
            {
                var target = item.GetProperty("target").GetString();
                var state = item.GetProperty("route_state").GetString();
                var server = item.GetProperty("server_id").GetString();
                lines.Add($"{target}: {state} -> {server}");
            }
        }
        return string.Join("\n", lines);
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
        dialog.SetMessage("Cudy Agent needs VPN permission, notifications, unrestricted battery mode, and Autostart on MIUI for reliable reconnect after reboot.");
        dialog.SetPositiveButton("Setup", (_, _) => SetupBackgroundPermissions());
        dialog.SetNegativeButton("Later", (_, _) => { });
        dialog.Show();
    }

    private void RenderPermissionStatus()
    {
        if (permissionStatusText is null)
        {
            return;
        }

        var notifications = HasNotificationPermission() ? "ok" : "needs allow";
        var battery = IsIgnoringBatteryOptimizations() ? "ok" : "needs setup";
        var vpn = Android.Net.VpnService.Prepare(this) is null ? "ok" : "needs allow";
        var autostart = IsMiuiDevice()
            ? (IsMiuiAutostartConfirmed() ? "confirmed" : "needs confirmation")
            : "n/a";
        permissionStatusText.Text = $"Permissions: notifications={notifications}; battery={battery}; vpn={vpn}; autostart={autostart}";

        if (permissionGuideText is not null)
        {
            var steps = new List<string>();
            if (!HasNotificationPermission())
            {
                steps.Add("allow notifications");
            }
            if (vpn != "ok")
            {
                steps.Add("allow VPN");
            }
            if (battery != "ok")
            {
                steps.Add("allow unrestricted battery");
            }
            if (IsMiuiDevice() && !IsMiuiAutostartConfirmed())
            {
                steps.Add("enable MIUI Autostart");
            }
            permissionGuideText.Text = steps.Count == 0
                ? "Setup: standard Android permissions are ready"
                : "Setup: " + string.Join(" -> ", steps);
        }
    }

    private void SetupBackgroundPermissions()
    {
        if (!HasNotificationPermission())
        {
            RequestNotificationPermission();
            statusText!.Text = "Notification permission requested";
            outputText!.Text = "Allow notifications so Android can keep the foreground VPN service visible.";
            RenderPermissionStatus();
            return;
        }

        var prepareIntent = Android.Net.VpnService.Prepare(this);
        if (prepareIntent != null)
        {
            pendingStartAfterPrepare = null;
            StartActivityForResult(prepareIntent, VpnPrepareRequest);
            statusText!.Text = "VPN permission requested";
            outputText!.Text = "Allow VPN permission, then tap Setup permissions again for battery and Autostart.";
            RenderPermissionStatus();
            return;
        }

        if (!IsIgnoringBatteryOptimizations())
        {
            if (OpenBatteryOptimizationRequest())
            {
                statusText!.Text = "Battery permission requested";
                outputText!.Text = "Allow battery unrestricted mode, then tap Setup permissions again to open Autostart.";
                RenderPermissionStatus();
                return;
            }
        }

        if (IsMiuiDevice() && !IsMiuiAutostartConfirmed())
        {
            preferences?.Edit()?.PutBoolean("miui_autostart_confirmation_pending", true)?.Apply();
            if (OpenMiuiAutostartSettings())
            {
                statusText!.Text = "Autostart settings opened";
                outputText!.Text = "Enable Autostart for Cudy Agent, then return to the app and confirm it.";
                RenderPermissionStatus();
                return;
            }
            preferences?.Edit()?.PutBoolean("miui_autostart_confirmation_pending", false)?.Apply();
            statusText!.Text = "Autostart needs manual setup";
            outputText!.Text = "MIUI Autostart settings could not be opened. Enable Autostart for Cudy Agent in the phone settings, then run Setup permissions again.";
            RenderPermissionStatus();
            return;
        }

        statusText!.Text = "Permissions ready";
        outputText!.Text = IsMiuiDevice()
            ? "Android permissions and MIUI Autostart are confirmed."
            : "Android permissions are ready.";
        RenderPermissionStatus();
    }

    private bool IsMiuiAutostartConfirmed()
    {
        return preferences?.GetBoolean("miui_autostart_confirmed", false) == true;
    }

    private void ConfirmMiuiAutostartIfPending()
    {
        if (!IsMiuiDevice()
            || preferences?.GetBoolean("miui_autostart_confirmation_pending", false) != true)
        {
            return;
        }

        preferences.Edit()?.PutBoolean("miui_autostart_confirmation_pending", false)?.Apply();
        var dialog = new AlertDialog.Builder(this);
        dialog.SetTitle("MIUI Autostart");
        dialog.SetMessage("Did you enable Autostart for Cudy Agent?");
        dialog.SetPositiveButton("Enabled", (_, _) =>
        {
            preferences.Edit()?.PutBoolean("miui_autostart_confirmed", true)?.Apply();
            statusText!.Text = "Autostart confirmed";
            outputText!.Text = "MIUI Autostart confirmation saved.";
            RenderPermissionStatus();
        });
        dialog.SetNegativeButton("Not yet", (_, _) =>
        {
            preferences.Edit()?.PutBoolean("miui_autostart_confirmed", false)?.Apply();
            statusText!.Text = "Autostart needs setup";
            outputText!.Text = "Tap Setup permissions when you are ready to enable MIUI Autostart.";
            RenderPermissionStatus();
        });
        dialog.Show();
    }

    private bool HasNotificationPermission()
    {
        if ((int)Build.VERSION.SdkInt < 33)
        {
            return true;
        }

        return CheckSelfPermission(PostNotificationsPermission) == Permission.Granted;
    }

    private void RequestNotificationPermission()
    {
        if ((int)Build.VERSION.SdkInt < 33)
        {
            return;
        }

        RequestPermissions(new[] { PostNotificationsPermission }, NotificationPermissionRequest);
    }

    private static bool IsMiuiDevice()
    {
        var manufacturer = Build.Manufacturer ?? "";
        var brand = Build.Brand ?? "";
        return manufacturer.Contains("xiaomi", StringComparison.OrdinalIgnoreCase)
            || manufacturer.Contains("redmi", StringComparison.OrdinalIgnoreCase)
            || manufacturer.Contains("poco", StringComparison.OrdinalIgnoreCase)
            || brand.Contains("xiaomi", StringComparison.OrdinalIgnoreCase)
            || brand.Contains("redmi", StringComparison.OrdinalIgnoreCase)
            || brand.Contains("poco", StringComparison.OrdinalIgnoreCase);
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
        intent.PutExtra("ssh_host_key_sha256", sshHostKeyInput!.Text);
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

    public override void OnRequestPermissionsResult(int requestCode, string[] permissions, Permission[] grantResults)
    {
        base.OnRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != NotificationPermissionRequest)
        {
            return;
        }

        statusText!.Text = grantResults.Length > 0 && grantResults[0] == Permission.Granted
            ? "Notification permission granted"
            : "Notification permission denied";
        RenderPermissionStatus();
    }

    private void RenderStoredStatus()
    {
        if (preferences is null || serviceStatusText is null || policyStatusText is null
            || probeStatusText is null || routeStatusText is null || transportStatusText is null
            || engineStatusText is null || outputText is null)
        {
            return;
        }

        var serviceStatus = preferences.GetString("service_status", "");
        var serviceAt = preferences.GetString("service_status_at", "");
        var policySummary = preferences.GetString("last_policy_summary", "");
        var policyAt = preferences.GetString("last_policy_at", "");
        var debugProbe = preferences.GetString("debug_probe_result", "");
        var debugProbeAt = preferences.GetString("debug_probe_at", "");
        var ipRoutes = preferences.GetInt("last_ip_routes", -1);
        var cleanupRoutes = preferences.GetInt("last_cleanup_routes", -1);
        var domainRoutes = preferences.GetInt("last_domain_routes", -1);
        var transports = preferences.GetInt("last_transports", -1);
        var storedTransports = preferences.GetInt("last_stored_transports", -1);
        var engineSummary = preferences.GetString("last_engine_summary", "");
        var runtimeSummary = preferences.GetString("last_runtime_summary", "");
        var probeSummary = preferences.GetString("last_probe_summary", "");
        var controlTunnel = preferences.GetBoolean("last_control_tunnel_established", false);
        var lastError = preferences.GetString("last_control_error", "");
        var bootReceiverAction = preferences.GetString("boot_receiver_action", "");
        var bootReceiverAt = preferences.GetString("boot_receiver_at", "");
        var bootReceiverResult = preferences.GetString("boot_receiver_result", "");
        var bootReceiverError = preferences.GetString("boot_receiver_error", "");
        serviceStatusText.Text = string.IsNullOrWhiteSpace(serviceStatus)
            ? "Service: -"
            : $"Service: {serviceStatus}";
        policyStatusText.Text = string.IsNullOrWhiteSpace(policyAt)
            ? "Policy: -"
            : $"Policy: {policyAt}";
        probeStatusText.Text = string.IsNullOrWhiteSpace(debugProbeAt)
            ? (string.IsNullOrWhiteSpace(probeSummary) ? "Probe: -" : $"Probe: {probeSummary}")
            : $"Probe: {debugProbeAt}";
        routeStatusText.Text = ipRoutes < 0
            ? "Routes: -"
            : $"Routes: ip={ipRoutes} domain={Math.Max(0, domainRoutes)} cleanup={Math.Max(0, cleanupRoutes)}";
        transportStatusText.Text = transports < 0
            ? "Transports: -"
            : $"Transports: plan={transports} stored={Math.Max(0, storedTransports)}";
        engineStatusText.Text = string.IsNullOrWhiteSpace(engineSummary)
            ? "Engine: -"
            : $"Engine: {engineSummary}; control={(controlTunnel ? "ssh" : "http")}";
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
        if (!string.IsNullOrWhiteSpace(runtimeSummary))
        {
            if (lines.Count > 0)
            {
                lines.Add("");
            }
            lines.Add("runtime:");
            lines.Add(runtimeSummary);
        }
        if (!string.IsNullOrWhiteSpace(bootReceiverAction) || !string.IsNullOrWhiteSpace(bootReceiverResult))
        {
            if (lines.Count > 0)
            {
                lines.Add("");
            }
            lines.Add("boot_receiver:");
            if (!string.IsNullOrWhiteSpace(bootReceiverAt))
            {
                lines.Add($"at: {bootReceiverAt}");
            }
            if (!string.IsNullOrWhiteSpace(bootReceiverAction))
            {
                lines.Add($"action: {bootReceiverAction}");
            }
            if (!string.IsNullOrWhiteSpace(bootReceiverResult))
            {
                lines.Add($"result: {bootReceiverResult}");
            }
            if (!string.IsNullOrWhiteSpace(bootReceiverError))
            {
                lines.Add($"error: {bootReceiverError}");
            }
        }
        if (!string.IsNullOrWhiteSpace(lastError))
        {
            if (lines.Count > 0)
            {
                lines.Add("");
            }
            lines.Add("last_error:");
            lines.Add(lastError);
        }
        if (lines.Count > 0)
        {
            outputText.Text = string.Join("\n", lines);
        }
    }
}
