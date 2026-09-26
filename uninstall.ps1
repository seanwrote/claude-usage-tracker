# Removes Claude Usage Tracker: stops it, deletes its two shortcuts, and
# optionally deletes this folder and the extra Claude Code profile logins.
# Never touches your main Claude Code login (~/.claude) or the Python packages.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = 'Claude Usage Tracker'
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -like '*tracker.pyw*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host 'Stopped the tracker.'

foreach ($lnk in @((Join-Path $programs "$name.lnk"), (Join-Path $programs "Startup\$name.lnk"))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed $lnk" }
}

# Extra profile logins listed in settings.json (the default ~/.claude is never offered).
$settingsFile = Join-Path $here 'settings.json'
if (Test-Path $settingsFile) {
    $settings = Get-Content $settingsFile -Raw | ConvertFrom-Json
    foreach ($p in $settings.profiles) {
        if (-not $p.config_dir) { continue }
        $dir = $p.config_dir -replace '^~', $HOME
        if (-not (Test-Path $dir)) { continue }
        $answer = Read-Host "Also delete the '$($p.name)' Claude Code login folder $dir ? [y/N]"
        if ($answer -eq 'y') { Remove-Item $dir -Recurse -Force; Write-Host "Deleted $dir" }
    }
}

$answer = Read-Host "Delete the app folder $here ? [y/N]"
if ($answer -eq 'y') {
    Set-Location $HOME
    Remove-Item $here -Recurse -Force
    Write-Host 'App folder deleted. Uninstall complete.'
} else {
    Write-Host "Uninstalled. The folder $here was kept."
}
