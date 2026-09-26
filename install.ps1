# Installs Claude Usage Tracker for the current user: checks the two Python
# packages, writes the icon and settings, adds Start Menu + Startup shortcuts,
# and starts the tray icon. Nothing outside this folder except the two shortcuts.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'tracker.pyw'

# Prefer the Python install manager's path: a bare `python` can be the Store stub.
$python = Join-Path $env:LOCALAPPDATA 'Python\bin\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'

& $python -c "import pystray, PIL" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing pystray and Pillow...'
    & $python -m pip install --user pystray pillow
}

& $python $script --setup
Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $here
Write-Host 'Claude Usage Tracker is running. Look for the pink icon in the tray (you may need the ^ overflow arrow).'
