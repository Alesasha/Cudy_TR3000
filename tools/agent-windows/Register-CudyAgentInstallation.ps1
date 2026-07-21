param([switch]$Remove)

$ErrorActionPreference = "Stop"
$registryPath = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\CudyAgent"

if ($Remove) {
    Remove-Item -LiteralPath $registryPath -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Cudy Agent application registration removed."
    exit 0
}

$versionName = "unknown"
$versionPath = Join-Path $PSScriptRoot "agent.version.json"
if (Test-Path -LiteralPath $versionPath) {
    try {
        $versionName = [string]((Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json).version_name)
    } catch {
    }
}

New-Item -Path $registryPath -Force | Out-Null
$uninstallScript = Join-Path $PSScriptRoot "Uninstall-CudyAgent.ps1"
$uninstallCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstallScript`""
New-ItemProperty -Path $registryPath -Name DisplayName -Value "Cudy Agent" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registryPath -Name DisplayVersion -Value $versionName -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registryPath -Name Publisher -Value "Cudy Agent" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registryPath -Name InstallLocation -Value $PSScriptRoot -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registryPath -Name UninstallString -Value $uninstallCommand -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registryPath -Name NoModify -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $registryPath -Name NoRepair -Value 1 -PropertyType DWord -Force | Out-Null
Write-Host "Cudy Agent registered in Installed apps: version $versionName."
