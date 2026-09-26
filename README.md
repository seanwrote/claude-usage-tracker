# Claude Usage Tracker

A small Windows tray icon that shows your Claude plan limits, both the 5-hour session and the weekly limit, for two Claude Code profiles. Switching profiles never needs a new login.

- **Tray icon**: a hot-pink tile showing the 5-hour % on top, with 5-hour and weekly bars underneath. A bar turns amber when you're using the limit faster than time is passing.
- **Left-click**: opens a popup with both profiles. Each has bars, reset times, a white "time elapsed" marker, and a **Refresh** button.
- **Right-click**: switch which profile the icon shows, refresh now, toggle start at login, or quit.
- Refreshes every 3 minutes, and every 15 minutes when the PC is idle. It also checks just after a limit resets.
- Talks only to `api.anthropic.com`. Your login token is read from Claude Code's own file and sent only in the request header. It is never logged or copied anywhere.

One Python file ([tracker.pyw](tracker.pyw)), using `pystray` and `Pillow`.

## Install

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

This installs `pystray` and `Pillow` if they're missing, writes `icon.ico` and `settings.json` into this folder, adds a Start Menu shortcut and a Startup shortcut, and starts the tracker.

Windows hides new tray icons at first. Click the `^` arrow next to the clock and drag the pink icon onto the taskbar, or turn it on under *Taskbar settings → Other system tray icons*.

## Sign in each profile (one time)

Each profile is a separate Claude Code login folder. The tracker reads the login saved in that folder, so after this one-time step you never need to sign in again.

```powershell
# Profile "Work": the default Claude Code folder (~/.claude)
claude auth login

# Profile "Personal": its own folder. Run it in a fresh PowerShell window.
$env:CLAUDE_CONFIG_DIR="$HOME\.claude-personal"; claude auth login
```

Then click **Refresh** in the popup. If a profile isn't signed in, the popup shows the exact command to run and a **Copy command** link.

Logins expire every few hours. When one is close to expiring, the tracker quietly runs Claude Code for that profile (`claude auth status`, then `claude update` if needed) so the CLI renews it. The tracker never edits login files itself.

## Settings

`settings.json` in this folder is created on the first run. Edit it, then quit and restart the tracker:

```json
{
  "profiles": [
    { "name": "Work", "config_dir": null },
    { "name": "Personal", "config_dir": "~/.claude-personal" }
  ],
  "tray_profile": "Work",
  "poll_seconds": 180,
  "idle_poll_seconds": 900
}
```

`config_dir: null` means the default `~/.claude` folder. Errors go to `tracker.log` in this folder, and no token is ever written there.

## Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

This stops the tracker and removes both shortcuts. It then asks whether to delete the extra profile login folders (for example `~/.claude-personal`) and this app folder. It never touches your main `~/.claude` login, and it leaves the shared Python packages installed.

## Notes

- This reads the same unofficial usage endpoint Claude Code uses (`/api/oauth/usage`). Anthropic may change it without notice.
- It only reads Claude Code CLI logins. The Claude desktop app stores its sign-in separately.
- Inspired by [Usage Monitor for Claude](https://github.com/jens-duttke/usage-monitor-for-claude). Not affiliated with Anthropic.
