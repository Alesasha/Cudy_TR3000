namespace CudyAndroidAgent;

using Android.App;
using Android.Content;
using Android.OS;
using Android.Widget;
using System.Text.Json;

[Activity(
    Name = "com.nashvpn.cudyagent.AdminActivity",
    Label = "Cudy Administration",
    Exported = false)]
public sealed class AdminActivity : Activity
{
    private sealed record AdminUser(
        string Id,
        string DisplayName,
        string Role,
        string DefaultServerId,
        string ClientIp,
        bool Enabled,
        bool HasLogin);

    private sealed record AdminDevice(
        string Id,
        string UserId,
        string DisplayName,
        string Platform,
        bool Enabled,
        string LastSeenAt);

    private TextView? statusText;
    private LinearLayout? loginContainer;
    private EditText? usernameInput;
    private EditText? passwordInput;
    private LinearLayout? managementContainer;
    private EditText? userIdInput;
    private EditText? userNameInput;
    private Spinner? userRoleSpinner;
    private EditText? userDefaultServerInput;
    private EditText? userClientIpInput;
    private EditText? userPasswordInput;
    private CheckBox? userEnabledCheckBox;
    private LinearLayout? usersList;
    private LinearLayout? devicesList;
    private Spinner? enrollmentUserSpinner;
    private Spinner? enrollmentPlatformSpinner;
    private EditText? enrollmentDeviceInput;
    private EditText? enrollmentNameInput;
    private EditText? enrollmentTtlInput;
    private TextView? enrollmentResultText;
    private Button? shareProvisioningButton;
    private CudyAdminSession? session;
    private readonly List<AdminUser> users = [];
    private readonly List<AdminDevice> devices = [];
    private string activationCode = "";

    protected override void OnCreate(Bundle? savedInstanceState)
    {
        base.OnCreate(savedInstanceState);
        SetContentView(Resource.Layout.activity_admin);

        statusText = RequireView<TextView>(Resource.Id.adminStatusText);
        loginContainer = RequireView<LinearLayout>(Resource.Id.adminLoginContainer);
        usernameInput = RequireView<EditText>(Resource.Id.adminUsernameInput);
        passwordInput = RequireView<EditText>(Resource.Id.adminPasswordInput);
        managementContainer = RequireView<LinearLayout>(Resource.Id.adminManagementContainer);
        userIdInput = RequireView<EditText>(Resource.Id.adminUserIdInput);
        userNameInput = RequireView<EditText>(Resource.Id.adminUserNameInput);
        userRoleSpinner = RequireView<Spinner>(Resource.Id.adminUserRoleSpinner);
        userDefaultServerInput = RequireView<EditText>(Resource.Id.adminUserDefaultServerInput);
        userClientIpInput = RequireView<EditText>(Resource.Id.adminUserClientIpInput);
        userPasswordInput = RequireView<EditText>(Resource.Id.adminUserPasswordInput);
        userEnabledCheckBox = RequireView<CheckBox>(Resource.Id.adminUserEnabledCheckBox);
        usersList = RequireView<LinearLayout>(Resource.Id.adminUsersList);
        devicesList = RequireView<LinearLayout>(Resource.Id.adminDevicesList);
        enrollmentUserSpinner = RequireView<Spinner>(Resource.Id.adminEnrollmentUserSpinner);
        enrollmentPlatformSpinner = RequireView<Spinner>(Resource.Id.adminEnrollmentPlatformSpinner);
        enrollmentDeviceInput = RequireView<EditText>(Resource.Id.adminEnrollmentDeviceInput);
        enrollmentNameInput = RequireView<EditText>(Resource.Id.adminEnrollmentNameInput);
        enrollmentTtlInput = RequireView<EditText>(Resource.Id.adminEnrollmentTtlInput);
        enrollmentResultText = RequireView<TextView>(Resource.Id.adminEnrollmentResultText);
        shareProvisioningButton = RequireView<Button>(Resource.Id.adminShareProvisioningButton);

        usernameInput.Text = "admin";
        userRoleSpinner.Adapter = StringAdapter(["user", "admin"]);
        enrollmentPlatformSpinner.Adapter = StringAdapter(["android", "windows", "linux"]);

        RequireView<Button>(Resource.Id.adminLoginButton).Click += async (_, _) => await LoginAsync();
        RequireView<Button>(Resource.Id.adminLogoutButton).Click += (_, _) => Logout();
        RequireView<Button>(Resource.Id.adminRefreshButton).Click += async (_, _) => await RefreshAsync();
        RequireView<Button>(Resource.Id.adminNewUserButton).Click += (_, _) => ResetUserEditor();
        RequireView<Button>(Resource.Id.adminSaveUserButton).Click += async (_, _) => await SaveUserAsync();
        RequireView<Button>(Resource.Id.adminCreateCodeButton).Click += async (_, _) => await CreateEnrollmentAsync();
        shareProvisioningButton.Click += (_, _) => ShareProvisioning();
    }

    private ArrayAdapter<string> StringAdapter(IReadOnlyList<string> values)
    {
        var adapter = new ArrayAdapter<string>(this, Android.Resource.Layout.SimpleSpinnerItem, values.ToArray());
        adapter.SetDropDownViewResource(Android.Resource.Layout.SimpleSpinnerDropDownItem);
        return adapter;
    }

    private T RequireView<T>(int id) where T : Android.Views.View
    {
        return FindViewById<T>(id) ?? throw new InvalidOperationException($"Required view {id} is missing.");
    }

    private async Task LoginAsync()
    {
        var password = passwordInput?.Text ?? "";
        if (string.IsNullOrWhiteSpace(usernameInput?.Text) || string.IsNullOrEmpty(password))
        {
            SetStatus("Administrator and password are required.", error: true);
            return;
        }

        SetStatus("Connecting securely...");
        passwordInput!.Text = "";
        try
        {
            session?.Dispose();
            session = null;
            var preferences = GetSharedPreferences("cudy-agent", FileCreationMode.Private)
                ?? throw new InvalidOperationException("Agent settings are unavailable.");
            session = await CudyAdminSession.ConnectAsync(
                preferences.GetString("ssh_host", "") ?? "",
                preferences.GetString("ssh_user", "") ?? "",
                preferences.GetString("ssh_key", "") ?? "",
                preferences.GetString("ssh_host_key_sha256", "") ?? "");
            await session.LoginAsync(usernameInput!.Text!.Trim(), password);
            loginContainer!.Visibility = Android.Views.ViewStates.Gone;
            managementContainer!.Visibility = Android.Views.ViewStates.Visible;
            SetStatus("Administrator session is active.");
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            session?.Dispose();
            session = null;
            loginContainer!.Visibility = Android.Views.ViewStates.Visible;
            managementContainer!.Visibility = Android.Views.ViewStates.Gone;
            var hint = ex.Message.Contains("Invalid user or password", StringComparison.OrdinalIgnoreCase)
                ? " Check the keyboard layout: Latin and Cyrillic letters can look identical."
                : "";
            SetStatus(ex.Message + hint, error: true);
        }
    }

    private void Logout()
    {
        session?.Dispose();
        session = null;
        managementContainer!.Visibility = Android.Views.ViewStates.Gone;
        loginContainer!.Visibility = Android.Views.ViewStates.Visible;
        passwordInput!.Text = "";
        SetStatus("Administrator session ended.");
    }

    private async Task RefreshAsync()
    {
        if (session is null)
        {
            SetStatus("Sign in first.", error: true);
            return;
        }

        SetStatus("Refreshing...");
        try
        {
            using var document = await session.GetAdminAsync();
            ParseState(document.RootElement);
            RenderUsers();
            RenderDevices();
            RenderEnrollmentUsers();
            SetStatus($"Loaded {users.Count} users and {devices.Count} devices.");
        }
        catch (Exception ex)
        {
            SetStatus(ex.Message, error: true);
        }
    }

    private void ParseState(JsonElement root)
    {
        users.Clear();
        devices.Clear();
        foreach (var item in root.GetProperty("users").EnumerateArray())
        {
            users.Add(new AdminUser(
                Text(item, "id"),
                Text(item, "display_name"),
                Text(item, "role"),
                Text(item, "default_server_id", "auto"),
                Text(item, "client_ip"),
                Boolean(item, "enabled"),
                Boolean(item, "has_login")));
        }
        foreach (var item in root.GetProperty("agent_devices").EnumerateArray())
        {
            devices.Add(new AdminDevice(
                Text(item, "id"),
                Text(item, "user_id"),
                Text(item, "display_name"),
                Text(item, "platform"),
                Boolean(item, "enabled"),
                Text(item, "last_seen_at")));
        }
    }

    private static string Text(JsonElement item, string name, string fallback = "")
    {
        if (!item.TryGetProperty(name, out var value) || value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
        {
            return fallback;
        }
        return value.ValueKind == JsonValueKind.String ? value.GetString() ?? fallback : value.ToString();
    }

    private static bool Boolean(JsonElement item, string name)
    {
        if (!item.TryGetProperty(name, out var value))
        {
            return false;
        }
        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.Number => value.TryGetInt32(out var number) && number != 0,
            JsonValueKind.String => value.GetString() is "1" or "true" or "yes",
            _ => false,
        };
    }

    private void RenderUsers()
    {
        usersList!.RemoveAllViews();
        foreach (var user in users)
        {
            var row = NewItemContainer();
            row.AddView(NewSummary(
                $"{user.DisplayName} ({user.Id})\n{user.Role} | {(user.Enabled ? "enabled" : "disabled")} | "
                + $"default {user.DefaultServerId} | login {(user.HasLogin ? "yes" : "no")}"));
            var actions = NewActions();
            actions.AddView(NewActionButton("Edit", () => EditUser(user)));
            actions.AddView(NewActionButton("Delete", async () => await DeleteUserAsync(user)));
            row.AddView(actions);
            usersList.AddView(row);
        }
    }

    private void RenderDevices()
    {
        devicesList!.RemoveAllViews();
        foreach (var device in devices)
        {
            var row = NewItemContainer();
            var lastSeen = string.IsNullOrWhiteSpace(device.LastSeenAt) ? "never" : device.LastSeenAt;
            row.AddView(NewSummary(
                $"{device.DisplayName} ({device.Id})\n{device.UserId} | {device.Platform} | "
                + $"{(device.Enabled ? "enabled" : "disabled")} | seen {lastSeen}"));
            var actions = NewActions();
            actions.AddView(NewActionButton("Edit", async () => await EditDeviceAsync(device)));
            actions.AddView(NewActionButton(device.Enabled ? "Disable" : "Enable", async () =>
                await SetDeviceEnabledAsync(device, !device.Enabled)));
            actions.AddView(NewActionButton("Delete", async () => await DeleteDeviceAsync(device)));
            row.AddView(actions);
            devicesList.AddView(row);
        }
    }

    private LinearLayout NewItemContainer()
    {
        return new LinearLayout(this)
        {
            Orientation = Orientation.Vertical,
            LayoutParameters = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MatchParent,
                LinearLayout.LayoutParams.WrapContent)
            {
                TopMargin = Dp(10),
            },
        };
    }

    private TextView NewSummary(string text)
    {
        return new TextView(this)
        {
            Text = text,
            TextSize = 14,
        };
    }

    private LinearLayout NewActions()
    {
        return new LinearLayout(this)
        {
            Orientation = Orientation.Horizontal,
            LayoutParameters = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MatchParent,
                LinearLayout.LayoutParams.WrapContent),
        };
    }

    private Button NewActionButton(string label, Action action)
    {
        var button = new Button(this)
        {
            Text = label,
            LayoutParameters = new LinearLayout.LayoutParams(0, Dp(48), 1),
        };
        button.Click += (_, _) => action();
        return button;
    }

    private void EditUser(AdminUser user)
    {
        userIdInput!.Text = user.Id;
        userIdInput.Enabled = false;
        userNameInput!.Text = user.DisplayName;
        userRoleSpinner!.SetSelection(user.Role == "admin" ? 1 : 0);
        userDefaultServerInput!.Text = user.DefaultServerId;
        userClientIpInput!.Text = user.ClientIp;
        userPasswordInput!.Text = "";
        userEnabledCheckBox!.Checked = user.Enabled;
        SetStatus($"Editing {user.Id}. A blank password keeps the current password.");
    }

    private void ResetUserEditor()
    {
        userIdInput!.Enabled = true;
        userIdInput.Text = "";
        userNameInput!.Text = "";
        userRoleSpinner!.SetSelection(0);
        userDefaultServerInput!.Text = "auto";
        userClientIpInput!.Text = "";
        userPasswordInput!.Text = "";
        userEnabledCheckBox!.Checked = true;
        SetStatus("New user. Leave password and client IP empty for an agent-only account.");
    }

    private async Task SaveUserAsync()
    {
        if (session is null)
        {
            return;
        }
        var id = userIdInput!.Text!.Trim();
        var password = userPasswordInput!.Text ?? "";
        if (id.Length < 2)
        {
            SetStatus("User ID must contain at least two characters.", error: true);
            return;
        }
        if (password.Length is > 0 and < 8)
        {
            SetStatus("Password must contain at least eight characters.", error: true);
            return;
        }

        SetStatus("Saving user...");
        try
        {
            using var result = await session.PostAsync("/api/admin/users", new
            {
                id,
                display_name = string.IsNullOrWhiteSpace(userNameInput!.Text) ? id : userNameInput.Text.Trim(),
                role = userRoleSpinner!.SelectedItem?.ToString() ?? "user",
                default_server_id = string.IsNullOrWhiteSpace(userDefaultServerInput!.Text)
                    ? "auto"
                    : userDefaultServerInput.Text.Trim(),
                client_ip = userClientIpInput!.Text?.Trim() ?? "",
                password,
                enabled = userEnabledCheckBox!.Checked,
                agent_only = string.IsNullOrWhiteSpace(password) && string.IsNullOrWhiteSpace(userClientIpInput.Text),
            });
            ResetUserEditor();
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            SetStatus(ex.Message, error: true);
        }
    }

    private async Task DeleteUserAsync(AdminUser user)
    {
        if (session is null || !await ConfirmAsync("Delete user", $"Delete {user.Id} and all associated devices?"))
        {
            return;
        }
        try
        {
            using var result = await session.DeleteAsync(
                $"/api/admin/users?id={System.Uri.EscapeDataString(user.Id)}");
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            SetStatus(ex.Message, error: true);
        }
    }

    private async Task SetDeviceEnabledAsync(AdminDevice device, bool enabled)
    {
        if (session is null)
        {
            return;
        }
        try
        {
            using var result = await session.PostAsync("/api/admin/agent-devices", new { id = device.Id, enabled });
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            SetStatus(ex.Message, error: true);
        }
    }

    private async Task EditDeviceAsync(AdminDevice device)
    {
        if (session is null)
        {
            return;
        }

        var userSpinner = new Spinner(this)
        {
            Adapter = StringAdapter(users.Select(item => item.Id).ToList()),
        };
        userSpinner.SetSelection(Math.Max(0, users.FindIndex(item => item.Id == device.UserId)));
        var nameInput = NewDialogInput("Name", device.DisplayName);
        var platformInput = NewDialogInput("Platform", device.Platform);
        var enabledInput = new CheckBox(this) { Text = "Enabled", Checked = device.Enabled };
        var panel = new LinearLayout(this) { Orientation = Orientation.Vertical };
        panel.SetPadding(Dp(20), Dp(8), Dp(20), Dp(8));
        panel.AddView(NewDialogLabel($"Device ID: {device.Id}"));
        panel.AddView(NewDialogLabel("User"));
        panel.AddView(userSpinner);
        panel.AddView(nameInput);
        panel.AddView(platformInput);
        panel.AddView(enabledInput);

        var completion = new TaskCompletionSource<bool>();
        var dialogBuilder = new AlertDialog.Builder(this);
        dialogBuilder.SetTitle("Edit device");
        dialogBuilder.SetView(panel);
        dialogBuilder.SetNegativeButton("Cancel", (_, _) => completion.TrySetResult(false));
        dialogBuilder.SetPositiveButton("Save", (_, _) => completion.TrySetResult(true));
        var dialog = dialogBuilder.Create()
            ?? throw new InvalidOperationException("Cannot create device editor.");
        dialog.CancelEvent += (_, _) => completion.TrySetResult(false);
        dialog.Show();
        if (!await completion.Task)
        {
            return;
        }

        SetStatus("Saving device...");
        try
        {
            using var result = await session.PostAsync("/api/admin/agent-devices", new
            {
                id = device.Id,
                user_id = userSpinner.SelectedItem?.ToString() ?? device.UserId,
                display_name = string.IsNullOrWhiteSpace(nameInput.Text) ? device.Id : nameInput.Text.Trim(),
                platform = string.IsNullOrWhiteSpace(platformInput.Text) ? "other" : platformInput.Text.Trim(),
                enabled = enabledInput.Checked,
            });
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            SetStatus(ex.Message, error: true);
        }
    }

    private EditText NewDialogInput(string hint, string value)
    {
        var input = new EditText(this)
        {
            Hint = hint,
            Text = value,
        };
        input.SetSingleLine(true);
        return input;
    }

    private TextView NewDialogLabel(string text)
    {
        return new TextView(this)
        {
            Text = text,
            TextSize = 14,
        };
    }

    private async Task DeleteDeviceAsync(AdminDevice device)
    {
        if (session is null || !await ConfirmAsync("Delete device", $"Permanently delete {device.Id}?"))
        {
            return;
        }
        try
        {
            using var result = await session.DeleteAsync(
                $"/api/admin/agent-devices?id={System.Uri.EscapeDataString(device.Id)}&hard=1");
            await RefreshAsync();
        }
        catch (Exception ex)
        {
            SetStatus(ex.Message, error: true);
        }
    }

    private void RenderEnrollmentUsers()
    {
        var userIds = users.Where(item => item.Enabled).Select(item => item.Id).ToList();
        enrollmentUserSpinner!.Adapter = StringAdapter(userIds);
    }

    private async Task CreateEnrollmentAsync()
    {
        if (session is null)
        {
            return;
        }
        var userId = enrollmentUserSpinner!.SelectedItem?.ToString() ?? "";
        var platform = enrollmentPlatformSpinner!.SelectedItem?.ToString() ?? "android";
        var deviceId = enrollmentDeviceInput!.Text!.Trim();
        if (string.IsNullOrWhiteSpace(userId) || string.IsNullOrWhiteSpace(deviceId))
        {
            SetStatus("Select a user and enter a device ID.", error: true);
            return;
        }
        if (!int.TryParse(enrollmentTtlInput!.Text, out var ttlHours))
        {
            ttlHours = 24;
        }

        SetStatus("Creating one-time setup...");
        try
        {
            using var document = await session.PostAsync("/api/admin/enrollment-codes", new
            {
                user_id = userId,
                device_id = deviceId,
                display_name = string.IsNullOrWhiteSpace(enrollmentNameInput!.Text)
                    ? deviceId
                    : enrollmentNameInput.Text.Trim(),
                platform,
                ttl_hours = Math.Clamp(ttlHours, 1, 168),
            });
            var root = document.RootElement;
            var code = Text(root, "code");
            var expires = Text(root, "expires_at");
            activationCode = code;
            enrollmentResultText!.Text = $"Code: {code}\nExpires: {expires}";
            shareProvisioningButton!.Enabled = !string.IsNullOrWhiteSpace(activationCode);
            SetStatus("One-time activation code created. Share it only with the intended user.");
        }
        catch (Exception ex)
        {
            SetStatus(ex.Message, error: true);
        }
    }

    private void ShareProvisioning()
    {
        if (string.IsNullOrWhiteSpace(activationCode))
        {
            return;
        }
        var share = new Intent(Intent.ActionSend);
        share.SetType("text/plain");
        share.PutExtra(Intent.ExtraText, activationCode);
        StartActivity(Intent.CreateChooser(share, "Share activation code"));
    }

    private async Task<bool> ConfirmAsync(string title, string message)
    {
        var completion = new TaskCompletionSource<bool>();
        var builder = new AlertDialog.Builder(this);
        builder.SetTitle(title);
        builder.SetMessage(message);
        builder.SetNegativeButton("Cancel", (_, _) => completion.TrySetResult(false));
        builder.SetPositiveButton("Delete", (_, _) => completion.TrySetResult(true));
        var dialog = builder.Create() ?? throw new InvalidOperationException("Confirmation dialog is unavailable.");
        dialog.CancelEvent += (_, _) => completion.TrySetResult(false);
        dialog.Show();
        return await completion.Task;
    }

    private void SetStatus(string message, bool error = false)
    {
        statusText!.Text = message;
        statusText.SetTextColor(error ? Android.Graphics.Color.Rgb(183, 28, 28) : Android.Graphics.Color.Rgb(25, 118, 55));
    }

    private int Dp(int value) => (int)(value * Resources!.DisplayMetrics!.Density + 0.5f);

    protected override void OnDestroy()
    {
        session?.Dispose();
        session = null;
        base.OnDestroy();
    }
}
