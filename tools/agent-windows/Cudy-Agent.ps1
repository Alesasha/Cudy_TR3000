param(
    [string]$TaskName = "Cudy Managed Route Agent",
    [int]$LocalPort = 18765,
    [switch]$SmokeTest
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetUnhandledExceptionMode([System.Windows.Forms.UnhandledExceptionMode]::CatchException)

$script:actionProcess = $null
$script:actionKind = ""
$script:actionOutput = ""
$script:updateCheckProcess = $null
$script:updateCheckOutput = ""
$script:statusProcess = $null
$script:statusOutput = Join-Path $PSScriptRoot "run\ui-status.json"
$script:updateAvailable = $false
$script:latestVersion = "unavailable"
$script:currentVersion = "unknown"
$script:agentIsOn = $false
$script:badgeColor = [Drawing.Color]::FromArgb(75, 85, 99)
$script:badgeText = "CHECK"
$script:trafficText = "0.00 MB"
$script:trafficBaseline = $null
$script:detailsVisible = $false

function New-Button {
    param([string]$Text, [int]$X, [int]$Y, [int]$Width = 145)
    $button = [Windows.Forms.Button]::new()
    $button.Text = $Text
    $button.Location = [Drawing.Point]::new($X, $Y)
    $button.Size = [Drawing.Size]::new($Width, 38)
    $button.FlatStyle = [Windows.Forms.FlatStyle]::Flat
    $button.BackColor = [Drawing.Color]::White
    $button.FlatAppearance.BorderColor = [Drawing.Color]::FromArgb(203, 213, 225)
    $button.Cursor = [Windows.Forms.Cursors]::Hand
    return $button
}

function Set-DetailsText {
    param([string]$Text)
    $details.Text = $Text
    $details.SelectionStart = $details.TextLength
    $details.ScrollToCaret()
}

function Start-UiAction {
    param(
        [string]$Kind,
        [string]$Script,
        [string[]]$Arguments = @(),
        [string]$OutputPath = ""
    )
    if ($script:actionProcess -and -not $script:actionProcess.HasExited) { return $false }
    $script:actionKind = $Kind
    $script:actionOutput = $OutputPath
    if ($OutputPath) { Remove-Item -LiteralPath $OutputPath -Force -ErrorAction SilentlyContinue }
    $argumentList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$Script`"") + $Arguments
    try {
        $script:actionProcess = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList $argumentList -PassThru
        return $true
    } catch {
        Set-DetailsText $_.Exception.Message
        return $false
    }
}

function Start-StateChange {
    param([ValidateSet("On", "Off")][string]$State)
    $runDir = Join-Path $PSScriptRoot "run"
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null
    $output = Join-Path $runDir "ui-state.txt"
    if (Start-UiAction -Kind ("State" + $State) -Script (Join-Path $PSScriptRoot "Set-AgentState.ps1") `
        -Arguments @("-State", $State, "-TaskName", "`"$TaskName`"", "-OutputPath", "`"$output`"") -OutputPath $output) {
        $primaryButton.Enabled = $false
        $primaryButton.Text = if ($State -eq "On") { "Starting..." } else { "Stopping..." }
        $script:badgeColor = [Drawing.Color]::FromArgb(234, 179, 8)
        $script:badgeText = if ($State -eq "On") { "STARTING" } else { "STOPPING" }
        $statusTitle.Text = if ($State -eq "On") { "Starting the agent" } else { "Stopping safely" }
        $badge.Invalidate()
    }
}

function Start-UpdateCheck {
    if ($script:updateCheckProcess -and -not $script:updateCheckProcess.HasExited) { return }
    $runDir = Join-Path $PSScriptRoot "run"
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null
    $output = Join-Path $runDir "ui-update-status.json"
    Remove-Item -LiteralPath $output -Force -ErrorAction SilentlyContinue
    $script:updateCheckOutput = $output
    try {
        $script:updateCheckProcess = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "`"$PSScriptRoot\Get-AgentUpdateStatus.ps1`"",
            "-OutputPath", "`"$output`""
        ) -PassThru
        $checkButton.Enabled = $false
        $checkButton.Text = "Checking..."
    } catch {
        Set-DetailsText ("Update check failed: " + $_.Exception.Message)
    }
}

function Complete-UpdateCheck {
    if (-not $script:updateCheckProcess -or -not $script:updateCheckProcess.HasExited) { return }
    $exitCode = $script:updateCheckProcess.ExitCode
    $script:updateCheckProcess.Dispose()
    $script:updateCheckProcess = $null
    $checkButton.Enabled = $true
    $checkButton.Text = "Check updates"
    if (Test-Path -LiteralPath $script:updateCheckOutput) {
        try {
            $update = Get-Content -Raw -LiteralPath $script:updateCheckOutput | ConvertFrom-Json
            $script:currentVersion = [string]$update.current_name
            $script:latestVersion = [string]$update.latest_name
            $script:updateAvailable = [bool]$update.update_available
            $versionLabel.Text = "Installed $($script:currentVersion)  |  Latest $($script:latestVersion)"
            $updateButton.Enabled = $script:updateAvailable
            $updateButton.Text = if ($script:updateAvailable) { "Update to $($script:latestVersion)" } else { "Up to date" }
            if (-not $update.ok -and $update.error) { Set-DetailsText ("Update check failed: " + $update.error) }
        } catch {
            Set-DetailsText "Could not read the update response."
        }
    } elseif ($exitCode -ne 0) {
        Set-DetailsText "Update check failed: control-server is unavailable."
    }
}

function Start-StatusRefresh {
    if ($script:statusProcess -and -not $script:statusProcess.HasExited) { return }
    $runDir = Split-Path -Parent $script:statusOutput
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null
    Remove-Item -LiteralPath $script:statusOutput -Force -ErrorAction SilentlyContinue
    try {
        $script:statusProcess = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", "`"$PSScriptRoot\Get-AgentUiStatus.ps1`"",
            "-TaskName", "`"$TaskName`"",
            "-LocalPort", [string]$LocalPort,
            "-OutputPath", "`"$script:statusOutput`""
        ) -PassThru
    } catch {
        $statusTitle.Text = "Status check failed"
        $statusSubtitle.Text = $_.Exception.Message
    }
}

function Complete-StatusRefresh {
    if (-not $script:statusProcess -or -not $script:statusProcess.HasExited) { return }
    $script:statusProcess.Dispose()
    $script:statusProcess = $null
    if (-not (Test-Path -LiteralPath $script:statusOutput)) { return }
    try { $status = Get-Content -Raw -LiteralPath $script:statusOutput | ConvertFrom-Json } catch { return }
    if ($script:actionProcess -and -not $script:actionProcess.HasExited) { return }

    $script:agentIsOn = $false
    if (-not $status.task_installed) {
        $script:badgeColor = [Drawing.Color]::FromArgb(107, 114, 128)
        $script:badgeText = "SETUP"
        $statusTitle.Text = "Agent is not installed"
        $statusSubtitle.Text = "Run the installer before starting the agent."
        $primaryButton.Enabled = $false
        $primaryButton.Text = "Not installed"
    } elseif ($status.task_state -eq "Disabled") {
        $script:badgeColor = [Drawing.Color]::FromArgb(31, 41, 55)
        $script:badgeText = "OFF"
        $statusTitle.Text = "Agent is off"
        $statusSubtitle.Text = "Direct internet remains available."
        $primaryButton.Enabled = $true
        $primaryButton.Text = "Start agent"
    } elseif ($status.control_connected) {
        $script:agentIsOn = $true
        $script:badgeColor = [Drawing.Color]::FromArgb(22, 163, 74)
        $script:badgeText = "ON"
        $statusTitle.Text = "Connection is healthy"
        $statusSubtitle.Text = "Control link is connected and policy is managed."
        $primaryButton.Enabled = $true
        $primaryButton.Text = "Stop agent"
    } elseif ($status.task_state -eq "Running") {
        $script:agentIsOn = $true
        $script:badgeColor = [Drawing.Color]::FromArgb(234, 179, 8)
        $script:badgeText = "STARTING"
        $statusTitle.Text = "Connecting"
        $statusSubtitle.Text = "The first connection can take up to one minute."
        $primaryButton.Enabled = $true
        $primaryButton.Text = "Stop agent"
    } else {
        $script:badgeColor = [Drawing.Color]::FromArgb(234, 88, 12)
        $script:badgeText = "ATTENTION"
        $statusTitle.Text = "Agent needs attention"
        $statusSubtitle.Text = "The task is enabled but is not running."
        $primaryButton.Enabled = $true
        $primaryButton.Text = "Start agent"
    }

    if ($script:currentVersion -eq "unknown" -and $status.current_version) {
        $script:currentVersion = [string]$status.current_version
        $versionLabel.Text = "Installed $($script:currentVersion)  |  Latest not checked"
    }
    $traffic = [int64]$status.traffic_bytes
    if ($null -eq $script:trafficBaseline) { $script:trafficBaseline = $traffic }
    $delta = [Math]::Max(0L, $traffic - [int64]$script:trafficBaseline)
    $script:trafficText = "{0:N2} MB" -f ($delta / 1MB)
    $badge.Invalidate()
}

function Start-AgentUpdate {
    if (-not $script:updateAvailable) { return }
    if (Start-UiAction -Kind "Update" -Script (Join-Path $PSScriptRoot "Update-AgentPackage.ps1")) {
        $updateButton.Enabled = $false
        $updateButton.Text = "Updating..."
        $statusTitle.Text = "Installing update"
        $statusSubtitle.Text = "Network services may reconnect briefly."
    }
}

function Start-Diagnostics {
    $output = Join-Path $PSScriptRoot "run\ui-diagnostics.txt"
    if (Start-UiAction -Kind "Diagnostics" -Script (Join-Path $PSScriptRoot "Invoke-AgentDiagnostics.ps1") `
        -Arguments @("-OutputPath", "`"$output`"") -OutputPath $output) {
        $diagnosticsButton.Enabled = $false
        $diagnosticsButton.Text = "Running..."
        if (-not $script:detailsVisible) { Toggle-Details }
        Set-DetailsText "Collecting diagnostics..."
    }
}

function Complete-UiAction {
    if (-not $script:actionProcess -or -not $script:actionProcess.HasExited) { return }
    $kind = $script:actionKind
    $exitCode = $script:actionProcess.ExitCode
    $output = $script:actionOutput
    $script:actionProcess.Dispose()
    $script:actionProcess = $null
    $script:actionKind = ""
    $script:actionOutput = ""

    if ($kind -like "State*") {
        $primaryButton.Enabled = $true
        if ($output -and (Test-Path -LiteralPath $output)) { Set-DetailsText (Get-Content -Raw -LiteralPath $output) }
    } elseif ($kind -eq "Diagnostics") {
        $diagnosticsButton.Enabled = $true
        $diagnosticsButton.Text = "Diagnostics"
        if (Test-Path -LiteralPath $output) { Set-DetailsText (Get-Content -Raw -LiteralPath $output) }
    } elseif ($kind -eq "Update") {
        if ($exitCode -eq 0) {
            Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @(
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSScriptRoot\Restart-AgentUi.ps1`""
            ) | Out-Null
            $form.Close()
        } else {
            $updateButton.Enabled = $true
            $updateButton.Text = "Retry update"
            Set-DetailsText "Update failed. Run Diagnostics for details."
        }
    }
}

function Toggle-Details {
    $script:detailsVisible = -not $script:detailsVisible
    $details.Visible = $script:detailsVisible
    $copyButton.Visible = $script:detailsVisible
    $form.ClientSize = if ($script:detailsVisible) { [Drawing.Size]::new(700, 570) } else { [Drawing.Size]::new(700, 335) }
    $detailsButton.Text = if ($script:detailsVisible) { "Hide details" } else { "Show details" }
}

function Refresh-AgentStatus {
    Complete-UiAction
    Complete-UpdateCheck
    Complete-StatusRefresh
    Start-StatusRefresh
}

$form = [Windows.Forms.Form]::new()
$form.Text = "Cudy Agent"
$form.ClientSize = [Drawing.Size]::new(700, 335)
$form.MinimumSize = [Drawing.Size]::new(716, 374)
$form.MaximizeBox = $false
$form.StartPosition = [Windows.Forms.FormStartPosition]::CenterScreen
$form.BackColor = [Drawing.Color]::FromArgb(248, 250, 252)
$form.Font = [Drawing.Font]::new("Segoe UI", 10)
if ($SmokeTest) {
    $form.ShowInTaskbar = $false
    $form.Opacity = 0
}

[System.Windows.Forms.Application]::add_ThreadException({
    param($sender, $eventArgs)
    $runDir = Join-Path $PSScriptRoot "run"
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null
    $eventArgs.Exception.ToString() | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $runDir "ui-crash.log")
    if ($SmokeTest) { $form.Close() }
})

$title = [Windows.Forms.Label]::new()
$title.Text = "Cudy Agent"
$title.Font = [Drawing.Font]::new("Segoe UI Semibold", 20)
$title.AutoSize = $true
$title.Location = [Drawing.Point]::new(24, 18)
$form.Controls.Add($title)

$badge = [Windows.Forms.Panel]::new()
$badge.Location = [Drawing.Point]::new(28, 72)
$badge.Size = [Drawing.Size]::new(132, 132)
$badge.Add_Paint({
    param($sender, $eventArgs)
    $graphics = $eventArgs.Graphics
    $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $brush = [Drawing.SolidBrush]::new($script:badgeColor)
    $statusFont = [Drawing.Font]::new("Segoe UI Semibold", 15)
    $trafficFont = [Drawing.Font]::new("Segoe UI", 10)
    $captionFont = [Drawing.Font]::new("Segoe UI", 8)
    $format = [Drawing.StringFormat]::new()
    try {
        $format.Alignment = [Drawing.StringAlignment]::Center
        $format.LineAlignment = [Drawing.StringAlignment]::Center
        $graphics.FillEllipse($brush, 1, 1, 128, 128)
        $graphics.DrawString($script:badgeText, $statusFont, [Drawing.Brushes]::White, [Drawing.RectangleF]::new(8, 25, 114, 42), $format)
        $graphics.DrawString($script:trafficText, $trafficFont, [Drawing.Brushes]::White, [Drawing.RectangleF]::new(8, 66, 114, 30), $format)
        $graphics.DrawString("this session", $captionFont, [Drawing.Brushes]::White, [Drawing.RectangleF]::new(8, 92, 114, 20), $format)
    } finally {
        $brush.Dispose()
        $statusFont.Dispose()
        $trafficFont.Dispose()
        $captionFont.Dispose()
        $format.Dispose()
    }
})
$form.Controls.Add($badge)

$statusTitle = [Windows.Forms.Label]::new()
$statusTitle.Text = "Checking status"
$statusTitle.Font = [Drawing.Font]::new("Segoe UI Semibold", 17)
$statusTitle.AutoSize = $true
$statusTitle.Location = [Drawing.Point]::new(185, 79)
$form.Controls.Add($statusTitle)

$statusSubtitle = [Windows.Forms.Label]::new()
$statusSubtitle.Text = "Please wait."
$statusSubtitle.ForeColor = [Drawing.Color]::FromArgb(71, 85, 105)
$statusSubtitle.AutoSize = $true
$statusSubtitle.MaximumSize = [Drawing.Size]::new(475, 50)
$statusSubtitle.Location = [Drawing.Point]::new(188, 119)
$form.Controls.Add($statusSubtitle)

$primaryButton = New-Button -Text "Checking..." -X 188 -Y 164 -Width 160
$primaryButton.BackColor = [Drawing.Color]::FromArgb(37, 99, 235)
$primaryButton.ForeColor = [Drawing.Color]::White
$primaryButton.FlatAppearance.BorderSize = 0
$primaryButton.Enabled = $false
$primaryButton.Add_Click({ if ($script:agentIsOn) { Start-StateChange -State Off } else { Start-StateChange -State On } })
$form.Controls.Add($primaryButton)

$diagnosticsButton = New-Button -Text "Diagnostics" -X 358 -Y 164 -Width 135
$diagnosticsButton.Add_Click({ Start-Diagnostics })
$form.Controls.Add($diagnosticsButton)

$settingsButton = New-Button -Text "Routing settings" -X 503 -Y 164 -Width 160
$settingsButton.Add_Click({ Start-Process "http://127.0.0.1:$LocalPort/" })
$form.Controls.Add($settingsButton)

$versionLabel = [Windows.Forms.Label]::new()
$versionLabel.Text = "Installed unknown  |  Latest not checked"
$versionLabel.AutoSize = $true
$versionLabel.ForeColor = [Drawing.Color]::FromArgb(71, 85, 105)
$versionLabel.Location = [Drawing.Point]::new(28, 232)
$form.Controls.Add($versionLabel)

$checkButton = New-Button -Text "Check updates" -X 360 -Y 220 -Width 145
$checkButton.Add_Click({ Start-UpdateCheck })
$form.Controls.Add($checkButton)

$updateButton = New-Button -Text "Up to date" -X 515 -Y 220 -Width 148
$updateButton.Enabled = $false
$updateButton.Add_Click({ Start-AgentUpdate })
$form.Controls.Add($updateButton)

$detailsButton = New-Button -Text "Show details" -X 28 -Y 277 -Width 132
$detailsButton.Add_Click({ Toggle-Details })
$form.Controls.Add($detailsButton)

$hint = [Windows.Forms.Label]::new()
$hint.Text = "Closing this window does not stop the agent."
$hint.AutoSize = $true
$hint.ForeColor = [Drawing.Color]::FromArgb(100, 116, 139)
$hint.Location = [Drawing.Point]::new(183, 288)
$form.Controls.Add($hint)

$details = [Windows.Forms.RichTextBox]::new()
$details.Location = [Drawing.Point]::new(28, 330)
$details.Size = [Drawing.Size]::new(635, 190)
$details.ReadOnly = $true
$details.WordWrap = $false
$details.Font = [Drawing.Font]::new("Consolas", 9)
$details.BackColor = [Drawing.Color]::White
$details.Visible = $false
$form.Controls.Add($details)

$copyButton = New-Button -Text "Copy report" -X 518 -Y 528 -Width 145
$copyButton.Visible = $false
$copyButton.Add_Click({ if ($details.Text) { [Windows.Forms.Clipboard]::SetText($details.Text) } })
$form.Controls.Add($copyButton)

$timer = [Windows.Forms.Timer]::new()
$timer.Interval = 5000
$timer.Add_Tick({ Refresh-AgentStatus })
$form.Add_Shown({
    if (-not $SmokeTest) {
        $form.Visible = $true
        $form.WindowState = [Windows.Forms.FormWindowState]::Normal
        $form.Activate()
    }
    Refresh-AgentStatus
    if ($SmokeTest) {
        $form.Close()
        return
    }
    $timer.Start()
    Start-UpdateCheck
})
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })

[Windows.Forms.Application]::Run($form)
