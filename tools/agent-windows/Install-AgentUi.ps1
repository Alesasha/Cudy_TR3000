param([switch]$Remove, [switch]$StartNow)

$ErrorActionPreference = "Stop"
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath("Desktop")
$programs = [Environment]::GetFolderPath("Programs")
$folder = Join-Path $programs "Cudy Agent"
$links = @(
    (Join-Path $desktop "Cudy Agent.lnk"),
    (Join-Path $folder "Cudy Agent.lnk")
)

if ($Remove) {
    foreach ($link in $links) { Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $folder -Force -ErrorAction SilentlyContinue
    Write-Host "Cudy Agent UI shortcuts removed."
    exit 0
}

New-Item -ItemType Directory -Force -Path $folder | Out-Null
$ui = Join-Path $PSScriptRoot "Cudy-Agent.ps1"
foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = "powershell.exe"
    $shortcut.Arguments = "-WindowStyle Hidden -STA -NoProfile -ExecutionPolicy Bypass -File `"$ui`""
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = "Cudy managed network agent"
    $shortcut.IconLocation = "$env:SystemRoot\System32\networkux.dll,0"
    $shortcut.Save()
}
Write-Host "Cudy Agent UI shortcuts installed."
if ($StartNow) { & $ui }
