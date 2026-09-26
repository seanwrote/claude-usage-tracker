# Removes Claude Usage Tracker: stops it, deletes its two shortcuts, and
# optionally deletes this folder and the extra account login folders it made.
# Never touches your main logins (~/.claude, ~/.codex) or the Python packages.

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

# Extra account login folders listed in settings.json. Only ~/.claude-* and
# ~/.codex-* folders are offered; the main ~/.claude and ~/.codex never are.
$settingsFile = Join-Path $here 'settings.json'
if (Test-Path $settingsFile) {
    $settings = Get-Content $settingsFile -Raw | ConvertFrom-Json
    $entries = @($settings.accounts) + @($settings.profiles) | Where-Object { $_ }
    foreach ($a in $entries) {
        $dir = if ($a.dir) { $a.dir } else { $a.config_dir }
        if (-not $dir) { continue }
        $full = [IO.Path]::GetFullPath(($dir -replace '^~', $HOME))
        $leaf = Split-Path $full -Leaf
        $isOurs = ((Split-Path $full -Parent) -eq $HOME) -and ($leaf -like '.claude-*' -or $leaf -like '.codex-*')
        if (-not $isOurs -or -not (Test-Path $full)) { continue }
        $answer = Read-Host "Also sign out '$($a.name)' by deleting its login folder $full ? [y/N]"
        if ($answer -eq 'y') { Remove-Item $full -Recurse -Force; Write-Host "Deleted $full" }
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
Read-Host 'Press Enter to close'
