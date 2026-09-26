# Installs Claude Usage Tracker for the current user: checks the two Python
# packages, writes the icon and settings, adds Start Menu, Startup and in-folder
# shortcuts, and starts the tray icon. Nothing outside this folder except the
# Start Menu and Startup shortcuts.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'tracker.pyw'

# Find Python 3.11+: the install manager's path, then the py launcher, then
# `python` on PATH. The Microsoft Store stub (WindowsApps) is skipped.
$candidates = @(Join-Path $env:LOCALAPPDATA 'Python\bin\python.exe')
if (Get-Command py -ErrorAction SilentlyContinue) {
    $candidates += (& py -3 -c "import sys; print(sys.executable)" 2>$null)
}
$candidates += (Get-Command python -ErrorAction SilentlyContinue).Source
$python = $null
foreach ($c in $candidates) {
    if (-not $c -or -not (Test-Path $c) -or $c -like '*\WindowsApps\*') { continue }
    & $c -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $c; break }
}
if (-not $python) {
    Write-Host 'Python 3.11 or newer is needed. Install it from https://www.python.org/downloads/ and run this again.' -ForegroundColor Yellow
    exit 1
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'

if (-not (Get-Command claude -ErrorAction SilentlyContinue) -and -not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Host 'Note: neither Claude Code (claude) nor Codex (codex) was found. Install one to have accounts to track.' -ForegroundColor Yellow
}

& $python -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('pystray') and u.find_spec('PIL') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing pystray and Pillow...'
    & $python -m pip install --user pystray pillow
    if ($LASTEXITCODE -ne 0) { Write-Host 'pip install failed.' -ForegroundColor Yellow; exit 1 }
}

& $python $script --setup
if ($LASTEXITCODE -ne 0) { Write-Host 'Setup failed; see tracker.log.' -ForegroundColor Yellow; exit 1 }
Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $here
Write-Host 'Claude Usage Tracker is running. Look for the pink icon in the tray (you may need the ^ overflow arrow).'
