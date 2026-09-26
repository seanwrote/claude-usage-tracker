"""
Claude Usage Tracker
====================

Windows tray icon showing Claude plan limits (5-hour and weekly) for several
Claude Code profiles. Left-click opens a popup with every profile; right-click
switches which profile the icon shows, refreshes, or quits.

Network: only https://api.anthropic.com/api/oauth/usage.
Credentials: read from each profile's Claude Code .credentials.json and used
only in the Authorization header. Never logged, never written.

Run with pythonw (no console). `python tracker.pyw --setup` writes the icon,
the default settings and the Start Menu / Startup shortcuts.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont

APP_NAME = 'Claude Usage Tracker'
APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / 'settings.json'
LOG_FILE = APP_DIR / 'tracker.log'
ICON_FILE = APP_DIR / 'icon.ico'
PROGRAMS_DIR = Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
STARTUP_LNK = PROGRAMS_DIR / 'Startup' / f'{APP_NAME}.lnk'
START_MENU_LNK = PROGRAMS_DIR / f'{APP_NAME}.lnk'

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
FALLBACK_UA = 'claude-code/2.1.270'

DEFAULT_SETTINGS = {
    'profiles': [
        {'name': 'Work', 'config_dir': None},
        {'name': 'Personal', 'config_dir': '~/.claude-personal'},
    ],
    'tray_profile': 'Work',
    'poll_seconds': 180,
    'idle_poll_seconds': 900,
}

# (API field, label, window length in seconds)
WINDOWS = (('five_hour', '5-hour', 5 * 3600), ('seven_day', 'Weekly', 7 * 86400))

IDLE_AFTER = 300          # seconds without input before polling slows down
RENEW_AHEAD = 300         # renew a token this many seconds before it expires
RENEW_COOLDOWN = 600      # at most one renewal attempt per profile per 10 min
MAX_BACKOFF = 900         # ceiling for rate-limit backoff
# Claude Code commands that make the CLI renew an expiring login.
# `claude update` is the one Usage Monitor for Claude relies on; `auth status`
# is tried first because it cannot install anything.
RENEW_COMMANDS = (['auth', 'status'], ['update'])

ACCENT = '#FF2D87'        # hot pink: icon tile and normal bar fill
WARN = '#FFC21A'          # amber: usage is ahead of the clock
BG = '#16121D'
PANEL = '#221B2C'
TRACK = '#352B42'
BORDER = '#FF2D87'
FG = '#F3EEF7'
DIM = '#9A8FA8'
GREY_TILE = '#6B6475'

BAR_WIDTH = 300           # popup bar width in 96-dpi pixels; sets the popup width

NO_WINDOW = subprocess.CREATE_NO_WINDOW


# Helpers


def log(message: str) -> None:
    """Append one line to tracker.log, truncating it once it passes 256 KB."""
    try:
        mode = 'w' if LOG_FILE.exists() and LOG_FILE.stat().st_size > 256_000 else 'a'
        with LOG_FILE.open(mode, encoding='utf-8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n')
    except OSError:
        pass


def load_settings() -> dict:
    """Read settings.json over the defaults. Writes the defaults on first run."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if not SETTINGS_FILE.exists():
        save_settings(settings)
        return settings
    try:
        user = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        if isinstance(user, dict):
            settings.update(user)
    except (OSError, ValueError) as e:
        # Leave a broken file alone so it can be fixed by hand.
        log(f'settings.json unreadable, using defaults: {e}')
    return settings


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
    except OSError as e:
        log(f'could not write settings.json: {e}')


def find_claude() -> str | None:
    """Locate the Claude Code CLI; a .ps1 shim is swapped for its .cmd sibling."""
    found = shutil.which('claude')
    if found:
        path = Path(found)
        if path.suffix.lower() == '.ps1':
            for ext in ('.cmd', '.exe'):
                if path.with_suffix(ext).is_file():
                    return str(path.with_suffix(ext))
        return found
    npm = Path(os.environ.get('APPDATA', '')) / 'npm' / 'claude.cmd'
    return str(npm) if npm.is_file() else None


CLAUDE_CLI = find_claude()


def run_claude(args: list[str], env: dict, timeout: int = 90) -> subprocess.CompletedProcess | None:
    if not CLAUDE_CLI:
        return None
    try:
        return subprocess.run(
            [CLAUDE_CLI, *args], env=env, capture_output=True, text=True, encoding='utf-8',
            errors='replace', stdin=subprocess.DEVNULL, timeout=timeout, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f'claude {" ".join(args)} failed: {e}')
        return None


def user_agent() -> str:
    proc = run_claude(['--version'], os.environ.copy(), timeout=20)
    version = proc.stdout.strip().split(' ')[0] if proc and proc.returncode == 0 else ''
    return f'claude-code/{version}' if version[:1].isdigit() else FALLBACK_UA


def http_get_usage(token: str, ua: str) -> tuple[int, dict | None, int | None]:
    """Return (status, body, retry_after). Status 0 means no response."""
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': f'Bearer {token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'Content-Type': 'application/json',
        'User-Agent': ua,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.load(resp), None
    except urllib.error.HTTPError as e:
        retry = e.headers.get('Retry-After')
        return e.code, None, int(retry) if retry and retry.isdigit() else None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None, None


def parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_usage(body: dict) -> dict:
    limits = {}
    for field, _label, _length in WINDOWS:
        entry = body.get(field) if isinstance(body, dict) else None
        if not isinstance(entry, dict):
            limits[field] = None
            continue
        pct = entry.get('utilization')
        valid = isinstance(pct, (int, float)) and not isinstance(pct, bool)
        limits[field] = {'pct': float(pct) if valid else None, 'resets': parse_time(entry.get('resets_at'))}
    return limits


def elapsed_fraction(limit: dict | None, length: int, now: datetime) -> float | None:
    """How much of the window has passed, or None when the window has not started."""
    if not limit or not limit['resets']:
        return None
    left = (limit['resets'] - now).total_seconds()
    return min(max(1 - left / length, 0.0), 1.0)


def is_ahead(limit: dict | None, length: int, now: datetime) -> bool:
    elapsed = elapsed_fraction(limit, length, now)
    return bool(limit and limit['pct'] is not None and elapsed is not None and limit['pct'] / 100 > elapsed)


def fmt_left(seconds: float) -> str:
    s = max(int(seconds), 0)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {s // 60}m'
    return f'{s // 60}m'


def fmt_reset(resets: datetime | None, now: datetime) -> str:
    if not resets:
        return 'not started'
    left = (resets - now).total_seconds()
    local = resets.astimezone()
    clock = local.strftime('%H:%M') if left < 86400 else local.strftime('%a %H:%M')
    return f'resets {clock} · in {fmt_left(left)}'


def fmt_pct(limit: dict | None) -> str:
    return f'{round(limit["pct"])}%' if limit and limit['pct'] is not None else '–'


def idle_seconds() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000


def work_area() -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def pythonw_path() -> str:
    # The install manager's stable alias survives Python upgrades; a bare
    # `pythonw` on PATH can be the Microsoft Store stub.
    alias = Path(os.environ.get('LOCALAPPDATA', '')) / 'Python' / 'bin' / 'pythonw.exe'
    return str(alias) if alias.exists() else str(Path(sys.executable).with_name('pythonw.exe'))


def make_shortcut(lnk: Path) -> None:
    """Create a .lnk that starts the tracker with pythonw."""
    script = (
        '$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CUT_LNK);'
        '$s.TargetPath=$env:CUT_TARGET;$s.Arguments=$env:CUT_ARGS;$s.WorkingDirectory=$env:CUT_DIR;'
        '$s.IconLocation=$env:CUT_ICON;$s.Description=$env:CUT_DESC;$s.Save()'
    )
    env = os.environ | {
        'CUT_LNK': str(lnk), 'CUT_TARGET': pythonw_path(), 'CUT_ARGS': f'"{Path(__file__).resolve()}"',
        'CUT_DIR': str(APP_DIR), 'CUT_ICON': str(ICON_FILE), 'CUT_DESC': APP_NAME,
    }
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                   env=env, creationflags=NO_WINDOW, timeout=30, check=False)


# Icon


def _font(px: int) -> ImageFont.ImageFont:
    for name in ('segoeuib.ttf', 'arialbd.ttf'):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def render_icon(size: int, pct5: float | None = None, ahead5: bool = False,
                pct7: float | None = None, ahead7: bool = False, state: str = 'ok') -> Image.Image:
    """Pink tile with the 5-hour percentage on top and 5-hour / weekly bars below."""
    s = 256
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=56, fill=ACCENT if state == 'ok' else GREY_TILE)

    if state != 'ok':
        font = _font(190)
        d.text((s / 2, s / 2), '!', font=font, fill='white', anchor='mm')
        return img.resize((size, size), Image.LANCZOS)

    text = str(round(pct5)) if pct5 is not None else '–'
    px = 150
    font = _font(px)
    while px > 60 and (d.textlength(text, font=font) > 224):
        px -= 6
        font = _font(px)
    d.text((s / 2, 82), text, font=font, fill='white', anchor='mm')

    for top, pct, ahead in ((166, pct5, ahead5), (210, pct7, ahead7)):
        left, right, bottom = 26, s - 26, top + 30
        d.rounded_rectangle((left, top, right, bottom), radius=12, fill='#A8124F')
        if pct:
            fill_right = left + (right - left) * min(pct, 100) / 100
            if fill_right - left >= 24:
                d.rounded_rectangle((left, top, fill_right, bottom), radius=12, fill=WARN if ahead else 'white')
            else:
                d.rectangle((left + 4, top + 4, max(fill_right, left + 8), bottom - 4), fill=WARN if ahead else 'white')
    return img.resize((size, size), Image.LANCZOS)


def write_icon_file() -> None:
    render_icon(256, 40, False, 25, False).save(ICON_FILE, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])


# Profiles


class Profile:
    """One Claude Code login, identified by its config directory."""

    def __init__(self, name: str, config_dir: str | None):
        self.name = name
        self.custom_dir = Path(os.path.expanduser(config_dir)) if config_dir else None
        self.dir = self.custom_dir or Path.home() / '.claude'
        self.result: dict = {'status': 'loading'}
        self.backoff = 0
        self.backoff_until = 0.0
        self.last_renew = 0.0

    def env(self) -> dict:
        env = os.environ.copy()
        if self.custom_dir:
            env['CLAUDE_CONFIG_DIR'] = str(self.custom_dir)
        else:
            env.pop('CLAUDE_CONFIG_DIR', None)
        return env

    def login_command(self) -> str:
        if not self.custom_dir:
            return 'claude auth login'
        try:
            shown = '$HOME\\' + str(self.custom_dir.relative_to(Path.home()))
        except ValueError:
            shown = str(self.custom_dir)
        return f'$env:CLAUDE_CONFIG_DIR="{shown}"; claude auth login'

    def read_oauth(self) -> dict | None:
        try:
            data = json.loads((self.dir / '.credentials.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None  # also covers a read racing the CLI rewriting the file
        oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
        return oauth if isinstance(oauth, dict) and oauth.get('accessToken') else None

    @staticmethod
    def expiring(oauth: dict, ahead: int = RENEW_AHEAD) -> bool:
        expires = oauth.get('expiresAt')
        if not isinstance(expires, (int, float)):
            return False
        return expires / 1000 <= time.time() + ahead

    def renew(self) -> bool:
        """Have the Claude CLI renew this profile's login. True if the token changed."""
        if time.time() - self.last_renew < RENEW_COOLDOWN:
            return False
        self.last_renew = time.time()
        before = (self.read_oauth() or {}).get('accessToken')
        for args in RENEW_COMMANDS:
            proc = run_claude(args, self.env())
            if args == ['auth', 'status'] and proc and '"loggedIn": false' in proc.stdout:
                log(f'{self.name}: Claude Code reports this profile is signed out')
                return False
            oauth = self.read_oauth()
            if oauth and oauth.get('accessToken') != before and not self.expiring(oauth):
                log(f'{self.name}: login renewed via `claude {" ".join(args)}`')
                return True
        log(f'{self.name}: login renewal produced no new token')
        return False

    def fetch(self, ua: str, force: bool) -> None:
        now = time.time()
        if not force and now < self.backoff_until:
            return
        oauth = self.read_oauth()
        if oauth is None:
            self.result = {'status': 'signed_out'}
            return
        if self.expiring(oauth):
            self.renew()
            oauth = self.read_oauth()
            # A token that is still expired after a renewal attempt means the
            # CLI is signed out; the usage API answers those with 429, not 401.
            if oauth is None or self.expiring(oauth, ahead=0):
                self.result = {'status': 'signed_out'}
                return

        status, body, retry_after = http_get_usage(oauth['accessToken'], ua)
        if status == 401 and self.renew():
            oauth = self.read_oauth() or oauth
            status, body, retry_after = http_get_usage(oauth['accessToken'], ua)

        plan = str(oauth.get('subscriptionType') or '').title()
        if status == 200 and isinstance(body, dict):
            self.backoff = 0
            self.backoff_until = 0.0
            self.result = {'status': 'ok', 'limits': parse_usage(body), 'plan': plan, 'at': now}
            return
        if status == 401:
            self.result = {'status': 'signed_out'}
            return

        if status == 429:
            self.backoff = retry_after or min(max(self.backoff * 2, 60), MAX_BACKOFF)
            note = f'Rate limited · retrying in {fmt_left(self.backoff)}'
        else:
            self.backoff = 60
            note = 'Offline · retrying' if status == 0 else f'Server error {status} · retrying'
        self.backoff_until = now + self.backoff
        log(f'{self.name}: usage request failed with status {status}')
        if self.result.get('status') == 'ok':
            self.result = {**self.result, 'note': note}  # keep the last good numbers
        else:
            self.result = {'status': 'error', 'message': note, 'plan': plan}


# Popup


class Popup:
    def __init__(self, app: 'App', root: tk.Tk):
        self.app = app
        self.root = root
        self.scale = root.winfo_fpixels('1i') / 96
        self.visible = False
        self.hidden_at = 0.0

        win = self.win = tk.Toplevel(root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=BG, highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)
        win.bind('<Escape>', lambda _e: self.hide())
        win.bind('<FocusOut>', lambda _e: root.after(150, self._hide_if_unfocused))
        self.body = tk.Frame(win, bg=BG)
        self.body.pack(fill='both', expand=True, padx=self.px(14), pady=self.px(12))
        self._tick()

    def px(self, value: float) -> int:
        return round(value * self.scale)

    # Visibility

    def toggle(self) -> None:
        if self.visible:
            self.hide()
        elif time.time() - self.hidden_at > 0.4:  # the tray click itself just closed it
            self.show()

    def show(self) -> None:
        self.render()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.visible = True

    def hide(self) -> None:
        self.win.withdraw()
        self.visible = False
        self.hidden_at = time.time()

    def _hide_if_unfocused(self) -> None:
        if self.visible and self.win.focus_displayof() is None:
            self.hide()

    def _tick(self) -> None:
        if self.visible:
            self.render()  # keep countdowns current
        self.root.after(30_000, self._tick)

    # Drawing

    def render(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        now = datetime.now(timezone.utc)

        header = tk.Frame(self.body, bg=BG)
        header.pack(fill='x')
        tk.Label(header, text='■', fg=ACCENT, bg=BG, font=('Segoe UI', 11)).pack(side='left')
        tk.Label(header, text='Claude usage', fg=FG, bg=BG, font=('Segoe UI Semibold', 11)).pack(side='left', padx=(self.px(4), 0))
        self._link(header, '✕', self.hide, size=11).pack(side='right')
        self._link(header, '↻ Refresh', self.app.refresh, size=9).pack(side='right', padx=(0, self.px(12)))

        for profile in self.app.profiles:
            self._profile_card(profile, now)

        footer = 'Refreshing…' if self.app.refreshing else self._footer_text()
        tk.Label(self.body, text=footer, fg=DIM, bg=BG, font=('Segoe UI', 8)).pack(anchor='w', pady=(self.px(8), 0))
        self._place()

    def _footer_text(self) -> str:
        if not self.app.last_poll:
            return 'Loading…'
        updated = datetime.fromtimestamp(self.app.last_poll)
        nxt = datetime.fromtimestamp(self.app.next_poll)
        return f'Updated {updated:%H:%M} · next check {nxt:%H:%M}'

    def _profile_card(self, profile: Profile, now: datetime) -> None:
        card = tk.Frame(self.body, bg=PANEL, padx=self.px(12), pady=self.px(10))
        card.pack(fill='x', pady=(self.px(10), 0))
        result = profile.result

        top = tk.Frame(card, bg=PANEL)
        top.pack(fill='x')
        tk.Label(top, text=profile.name, fg=FG, bg=PANEL, font=('Segoe UI Semibold', 10)).pack(side='left')
        if profile.name == self.app.tray_name:
            tk.Label(top, text=' in tray ', fg=BG, bg=ACCENT, font=('Segoe UI Semibold', 7)).pack(side='left', padx=(self.px(6), 0))
        else:
            self._link(top, 'show in tray', lambda n=profile.name: self.app.set_tray(n), size=8, bg=PANEL, fg=DIM).pack(side='left', padx=(self.px(6), 0))
        if result.get('plan'):
            tk.Label(top, text=result['plan'], fg=DIM, bg=PANEL, font=('Segoe UI', 8)).pack(side='right')

        status = result['status']
        if status == 'loading':
            tk.Label(card, text='Loading…', fg=DIM, bg=PANEL, font=('Segoe UI', 9)).pack(anchor='w', pady=(self.px(6), 0))
        elif status == 'signed_out':
            self._signed_out(card, profile)
        elif status == 'error':
            tk.Label(card, text=result['message'], fg=WARN, bg=PANEL, font=('Segoe UI', 9)).pack(anchor='w', pady=(self.px(6), 0))
        else:
            for field, label, length in WINDOWS:
                self._bar(card, label, result['limits'].get(field), length, now)
            if result.get('note'):
                tk.Label(card, text=result['note'], fg=WARN, bg=PANEL, font=('Segoe UI', 8)).pack(anchor='w', pady=(self.px(6), 0))

    def _bar(self, parent: tk.Frame, label: str, limit: dict | None, length: int, now: datetime) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', pady=(self.px(8), 0))
        tk.Label(row, text=label, fg=FG, bg=PANEL, font=('Segoe UI', 9)).pack(side='left')
        ahead = is_ahead(limit, length, now)
        tk.Label(row, text=fmt_pct(limit), fg=WARN if ahead else FG, bg=PANEL, font=('Segoe UI Semibold', 9)).pack(side='right')

        width, height, pad = self.px(BAR_WIDTH), self.px(12), self.px(2)
        canvas = tk.Canvas(parent, width=width, height=height, bg=PANEL, highlightthickness=0)
        canvas.pack(anchor='w', pady=(self.px(3), 0))
        canvas.create_rectangle(0, pad, width, height - pad, fill=TRACK, width=0)
        if limit and limit['pct']:
            canvas.create_rectangle(0, pad, width * min(limit['pct'], 100) / 100, height - pad,
                                    fill=WARN if ahead else ACCENT, width=0)
        elapsed = elapsed_fraction(limit, length, now)
        if elapsed is not None:
            x = max(1, min(width - 1, round(width * elapsed)))
            canvas.create_line(x, 0, x, height, fill='white', width=max(1, self.px(2)))

        resets = limit['resets'] if limit else None
        tk.Label(parent, text=fmt_reset(resets, now), fg=DIM, bg=PANEL, font=('Segoe UI', 8)).pack(anchor='w')

    def _signed_out(self, card: tk.Frame, profile: Profile) -> None:
        tk.Label(card, text='Not signed in. Run this in PowerShell once, then refresh:', fg=DIM, bg=PANEL,
                 font=('Segoe UI', 9), justify='left', wraplength=self.px(BAR_WIDTH)).pack(anchor='w', pady=(self.px(6), 0))
        cmd = profile.login_command()
        tk.Label(card, text=cmd, fg=FG, bg=BG, font=('Consolas', 8), justify='left', anchor='w',
                 wraplength=self.px(BAR_WIDTH - 12), padx=self.px(6), pady=self.px(4)).pack(fill='x', pady=(self.px(4), 0))
        self._link(card, 'Copy command', lambda: self._copy(cmd), size=8, bg=PANEL).pack(anchor='w', pady=(self.px(4), 0))

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _link(self, parent, text, command, size=9, bg=BG, fg=ACCENT) -> tk.Label:
        link = tk.Label(parent, text=text, fg=fg, bg=bg, cursor='hand2', font=('Segoe UI Semibold', size))
        link.bind('<Button-1>', lambda _e: command())
        return link

    def _place(self) -> None:
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        _left, _top, right, bottom = work_area()
        margin = self.px(12)
        self.win.geometry(f'{w}x{h}+{right - w - margin}+{bottom - h - margin}')


# App


class App:
    def __init__(self):
        self.settings = load_settings()
        self.profiles = [Profile(str(p.get('name', f'Profile {i + 1}')), p.get('config_dir'))
                         for i, p in enumerate(self.settings.get('profiles') or DEFAULT_SETTINGS['profiles'])]
        names = [p.name for p in self.profiles]
        self.tray_name = self.settings.get('tray_profile') if self.settings.get('tray_profile') in names else names[0]
        self.ua = FALLBACK_UA
        self.refreshing = False
        self.last_poll = 0.0
        self.next_poll = 0.0
        self.stopping = False
        self.force = threading.Event()
        self.ui = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = Popup(self, self.root)
        self.icon = pystray.Icon('claude-usage-tracker', render_icon(64, state='ok'), APP_NAME, self._menu())

    # Tray

    def _menu(self) -> pystray.Menu:
        items = [
            pystray.MenuItem('Show usage', self._on_show, default=True),
            pystray.MenuItem('Refresh now', self._on_refresh),
            pystray.Menu.SEPARATOR,
        ]
        for profile in self.profiles:
            items.append(pystray.MenuItem(f'Tray shows: {profile.name}', self._tray_action(profile.name),
                                          checked=self._tray_checked(profile.name), radio=True))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Start at login', self._on_startup, checked=lambda _item: STARTUP_LNK.exists()),
            pystray.MenuItem('Quit', self._on_quit),
        ]
        return pystray.Menu(*items)

    def _tray_action(self, name: str):
        def action(_icon, _item):
            self.set_tray(name)
        return action

    def _tray_checked(self, name: str):
        def checked(_item):
            return self.tray_name == name
        return checked

    def _on_show(self, _icon, _item):
        self.ui.put('toggle')

    def _on_refresh(self, _icon, _item):
        self.refresh()

    def _on_startup(self, _icon, _item):
        if STARTUP_LNK.exists():
            STARTUP_LNK.unlink(missing_ok=True)
        else:
            if not ICON_FILE.exists():
                write_icon_file()
            make_shortcut(STARTUP_LNK)

    def _on_quit(self, icon, _item):
        self.stopping = True
        self.force.set()
        icon.stop()
        self.ui.put('quit')

    def set_tray(self, name: str) -> None:
        self.tray_name = name
        self.settings['tray_profile'] = name
        save_settings(self.settings)
        self._update_tray()
        self.ui.put('render')

    def refresh(self) -> None:
        self.force.set()

    def _update_tray(self) -> None:
        profile = next(p for p in self.profiles if p.name == self.tray_name)
        result = profile.result
        if result['status'] == 'ok':
            now = datetime.now(timezone.utc)
            l5, l7 = result['limits'].get('five_hour'), result['limits'].get('seven_day')
            self.icon.icon = render_icon(
                64, l5['pct'] if l5 else None, is_ahead(l5, WINDOWS[0][2], now),
                l7['pct'] if l7 else None, is_ahead(l7, WINDOWS[1][2], now))
            left5 = f' ({fmt_left((l5["resets"] - now).total_seconds())})' if l5 and l5['resets'] else ''
            left7 = f' ({fmt_left((l7["resets"] - now).total_seconds())})' if l7 and l7['resets'] else ''
            title = f'{profile.name} · 5h {fmt_pct(l5)}{left5} · wk {fmt_pct(l7)}{left7}'
            if result.get('note'):
                title += f'\n{result["note"]}'
        elif result['status'] == 'loading':
            self.icon.icon = render_icon(64, state='ok')
            title = f'{profile.name} · loading…'
        else:
            self.icon.icon = render_icon(64, state='error')
            title = f'{profile.name} · ' + ('not signed in' if result['status'] == 'signed_out' else result['message'])
        self.icon.title = title[:127]

    # Polling

    def _interval(self) -> float:
        base = self.settings.get('poll_seconds', 180)
        if idle_seconds() > IDLE_AFTER:
            base = self.settings.get('idle_poll_seconds', 900)
        # Wake just after the next reset so a refilled window shows up promptly.
        now = datetime.now(timezone.utc)
        for profile in self.profiles:
            for limit in (profile.result.get('limits') or {}).values():
                if limit and limit['resets']:
                    until = (limit['resets'] - now).total_seconds() + 5
                    if 0 < until < base:
                        base = until
        return max(float(base), 30.0)

    def _worker(self) -> None:
        self.ua = user_agent()
        while True:
            forced = self.force.wait(timeout=15)
            if self.stopping:
                return
            self.force.clear()
            if not forced and time.time() < self.next_poll:
                continue
            self.refreshing = True
            self.ui.put('render')
            for profile in self.profiles:
                try:
                    profile.fetch(self.ua, force=forced)
                except Exception as e:  # keep the tray alive whatever happens
                    log(f'{profile.name}: unexpected error {type(e).__name__}: {e}')
                    profile.result = {'status': 'error', 'message': 'Unexpected error, see tracker.log'}
            self.last_poll = time.time()
            self.next_poll = self.last_poll + self._interval()
            self.refreshing = False
            self._update_tray()
            self.ui.put('render')

    # UI thread

    def _pump(self) -> None:
        try:
            while True:
                message = self.ui.get_nowait()
                if message == 'quit':
                    self.root.destroy()
                    return
                if message == 'toggle':
                    self.popup.toggle()
                elif message == 'render' and self.popup.visible:
                    self.popup.render()
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def run(self) -> None:
        self.icon.run_detached()
        threading.Thread(target=self._worker, daemon=True).start()
        self.force.set()
        self.root.after(100, self._pump)
        self.root.mainloop()


def setup() -> None:
    """Write icon, settings and shortcuts. Used by install.ps1."""
    write_icon_file()
    load_settings()
    make_shortcut(START_MENU_LNK)
    make_shortcut(STARTUP_LNK)
    print(f'Icon, settings and shortcuts written. Settings: {SETTINGS_FILE}')


def main() -> None:
    if '--setup' in sys.argv:
        setup()
        return
    # One tray icon only: a second launch exits quietly.
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW(None, False, 'Local\\ClaudeUsageTracker')
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        App().run()
    except Exception as e:
        log(f'fatal: {type(e).__name__}: {e}')
        raise


if __name__ == '__main__':
    main()
