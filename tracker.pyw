"""
Claude Usage Tracker
====================

Windows tray icon showing plan usage limits for Claude Code and ChatGPT
(Codex) accounts. Left-click opens a popup with every account; right-click
switches which account the icon shows, adds or removes accounts, refreshes,
uninstalls or quits.

Network: Claude limits come from https://api.anthropic.com/api/oauth/usage.
ChatGPT limits come from the local `codex app-server`, which talks to OpenAI
itself; this app never reads Codex credentials.
Claude credentials are read from each account's .credentials.json and used
only in the Authorization header. Never logged, never written.

Run with pythonw (no console). `python tracker.pyw --setup` writes the icon,
the default settings and the Start Menu / Startup shortcuts.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import re
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
from tkinter import messagebox

import pystray
from PIL import Image, ImageDraw, ImageFont

APP_NAME = 'Claude Usage Tracker'
APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / 'settings.json'
LOG_FILE = APP_DIR / 'tracker.log'
ICON_FILE = APP_DIR / 'icon.ico'
UNINSTALL_SCRIPT = APP_DIR / 'uninstall.ps1'
PROGRAMS_DIR = Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
STARTUP_LNK = PROGRAMS_DIR / 'Startup' / f'{APP_NAME}.lnk'
START_MENU_LNK = PROGRAMS_DIR / f'{APP_NAME}.lnk'
FOLDER_LNK = APP_DIR / f'{APP_NAME}.lnk'  # double-clickable launcher next to the script

USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
FALLBACK_UA = 'claude-code/2.1.270'

DEFAULT_SETTINGS = {
    'accounts': [{'name': 'Claude', 'kind': 'claude', 'dir': None}],
    'tray_account': 'Claude',
    'poll_seconds': 180,
    'idle_poll_seconds': 900,
}

IDLE_AFTER = 300          # seconds without input before polling slows down
RENEW_AHEAD = 300         # renew a Claude token this many seconds before it expires
RENEW_COOLDOWN = 600      # at most one renewal attempt per account per 10 min
MAX_BACKOFF = 900         # ceiling for rate-limit backoff
CODEX_TIMEOUT = 25        # seconds to wait for each codex app-server reply
SCROLL_AFTER = 4          # the popup scrolls once there are more accounts than this
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


def find_cli(name: str) -> str | None:
    """Locate an npm-installed CLI; a .ps1 shim is swapped for its .cmd sibling."""
    found = shutil.which(name)
    if found:
        path = Path(found)
        if path.suffix.lower() == '.ps1':
            for ext in ('.cmd', '.exe'):
                if path.with_suffix(ext).is_file():
                    return str(path.with_suffix(ext))
        return found
    npm = Path(os.environ.get('APPDATA', '')) / 'npm' / f'{name}.cmd'
    return str(npm) if npm.is_file() else None


CLAUDE_CLI = find_cli('claude')
CODEX_CLI = find_cli('codex')
CLAUDE_UA = FALLBACK_UA


def load_settings() -> dict:
    """Read settings.json over the defaults, migrating the old `profiles` layout."""
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if not SETTINGS_FILE.exists():
        if (Path.home() / '.codex' / 'auth.json').exists():
            settings['accounts'].append({'name': 'ChatGPT', 'kind': 'chatgpt', 'dir': None})
        save_settings(settings)
        return settings
    try:
        user = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        # Leave a broken file alone so it can be fixed by hand.
        log(f'settings.json unreadable, using defaults: {e}')
        return settings
    if not isinstance(user, dict):
        return settings
    if 'accounts' not in user and isinstance(user.get('profiles'), list):
        user['accounts'] = [{'name': p.get('name'), 'kind': 'claude', 'dir': p.get('config_dir')}
                            for p in user.pop('profiles') if isinstance(p, dict)]
        if (Path.home() / '.codex' / 'auth.json').exists():
            user['accounts'].append({'name': 'ChatGPT', 'kind': 'chatgpt', 'dir': None})
        if 'tray_profile' in user:
            user['tray_account'] = user.pop('tray_profile')
        settings.update(user)
        save_settings(settings)
        return settings
    settings.update(user)
    return settings


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n', encoding='utf-8')
    except OSError as e:
        log(f'could not write settings.json: {e}')


def run_cli(cli: str | None, args: list[str], env: dict, timeout: int = 90) -> subprocess.CompletedProcess | None:
    if not cli:
        return None
    try:
        return subprocess.run(
            [cli, *args], env=env, capture_output=True, text=True, encoding='utf-8',
            errors='replace', stdin=subprocess.DEVNULL, timeout=timeout, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f'{Path(cli).stem} {" ".join(args)} failed: {e}')
        return None


def claude_user_agent() -> str:
    proc = run_cli(CLAUDE_CLI, ['--version'], os.environ.copy(), timeout=20)
    version = proc.stdout.strip().split(' ')[0] if proc and proc.returncode == 0 else ''
    return f'claude-code/{version}' if version[:1].isdigit() else FALLBACK_UA


def http_get_usage(token: str) -> tuple[int, dict | None, int | None]:
    """Return (status, body, retry_after). Status 0 means no response."""
    req = urllib.request.Request(USAGE_URL, headers={
        'Authorization': f'Bearer {token}',
        'anthropic-beta': 'oauth-2025-04-20',
        'Content-Type': 'application/json',
        'User-Agent': CLAUDE_UA,
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


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_claude_usage(body: dict) -> list[dict]:
    """Claude's usage response as [{label, pct, resets, length}] for the 5-hour and weekly windows."""
    limits = []
    for field, label, length in (('five_hour', '5-hour', 5 * 3600), ('seven_day', 'Weekly', 7 * 86400)):
        entry = body.get(field) if isinstance(body, dict) else None
        entry = entry if isinstance(entry, dict) else {}
        pct = entry.get('utilization')
        limits.append({'label': label, 'pct': float(pct) if is_number(pct) else None,
                       'resets': parse_time(entry.get('resets_at')), 'length': length})
    return limits


def window_label(minutes, fallback: str) -> str:
    if not is_number(minutes) or minutes <= 0:
        return fallback
    minutes = int(minutes)
    if minutes == 7 * 1440:
        return 'Weekly'
    if minutes % 1440 == 0:
        return f'{minutes // 1440}-day'
    if minutes % 60 == 0:
        return f'{minutes // 60}-hour'
    return fallback


def parse_codex_limits(payload: dict | None) -> list[dict]:
    """Codex's rate-limit snapshot as [{label, pct, resets, length}] (primary, then secondary)."""
    if not isinstance(payload, dict):
        return []
    by_id = payload.get('rateLimitsByLimitId')
    bucket = by_id.get('codex') if isinstance(by_id, dict) else None
    if not isinstance(bucket, dict):
        bucket = payload.get('rateLimits')
    if not isinstance(bucket, dict):
        return []
    limits = []
    for position in ('primary', 'secondary'):
        window = bucket.get(position)
        if not isinstance(window, dict):
            continue
        used, minutes, resets = window.get('usedPercent'), window.get('windowDurationMins'), window.get('resetsAt')
        limits.append({
            'label': window_label(minutes, position.title()),
            'pct': float(used) if is_number(used) else None,
            'resets': datetime.fromtimestamp(resets, tz=timezone.utc) if is_number(resets) else None,
            'length': int(minutes) * 60 if is_number(minutes) and minutes > 0 else None,
        })
    return limits


def elapsed_fraction(limit: dict | None, now: datetime) -> float | None:
    """How much of the window has passed, or None when the window has not started."""
    if not limit or not limit['resets'] or not limit['length']:
        return None
    left = (limit['resets'] - now).total_seconds()
    return min(max(1 - left / limit['length'], 0.0), 1.0)


def is_ahead(limit: dict | None, now: datetime) -> bool:
    elapsed = elapsed_fraction(limit, now)
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


def short_label(label: str) -> str:
    return {'5-hour': '5h', 'Weekly': 'wk'}.get(label, label)


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


# Sets System.AppUserModel.ID on a shortcut. Without its own ID, Start treats
# every shortcut to pythonw.exe as one app (IDLE's) and hides the rest.
SET_APP_ID_CS = r'''
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {
    [PreserveSig] int GetCount(out uint count);
    [PreserveSig] int GetAt(uint index, out PropertyKey key);
    [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
    [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
    [PreserveSig] int Commit();
}
[StructLayout(LayoutKind.Sequential, Pack = 4)]
struct PropertyKey { public Guid fmtid; public uint pid; }
[StructLayout(LayoutKind.Explicit)]
struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }
public static class ShortcutAppId {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(string path, IntPtr bindCtx, int flags, ref Guid iid,
        [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);
    public static int Set(string lnk, string appId) {
        Guid iid = typeof(IPropertyStore).GUID;
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out store);  // GPS_READWRITE
        var key = new PropertyKey { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
        var value = new PropVariant { vt = 31, p = Marshal.StringToCoTaskMemUni(appId) };  // VT_LPWSTR
        int hr = store.SetValue(ref key, ref value);
        if (hr == 0) hr = store.Commit();
        Marshal.FreeCoTaskMem(value.p);
        Marshal.ReleaseComObject(store);
        return hr;
    }
}
'''
APP_ID = 'ClaudeUsageTracker.Tray'


def make_shortcut(lnk: Path, app_id: str | None = None) -> None:
    """Create a .lnk that starts the tracker with pythonw, optionally with its own Start app ID."""
    script = (
        '$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CUT_LNK);'
        '$s.TargetPath=$env:CUT_TARGET;$s.Arguments=$env:CUT_ARGS;$s.WorkingDirectory=$env:CUT_DIR;'
        '$s.IconLocation=$env:CUT_ICON;$s.Description=$env:CUT_DESC;$s.Save();'
        'if ($env:CUT_APPID) { Add-Type -TypeDefinition $env:CUT_CS; '
        '$hr=[ShortcutAppId]::Set($env:CUT_LNK, $env:CUT_APPID); if ($hr -ne 0) { exit 3 } }'
    )
    env = os.environ | {
        'CUT_LNK': str(lnk), 'CUT_TARGET': pythonw_path(), 'CUT_ARGS': f'"{Path(__file__).resolve()}"',
        'CUT_DIR': str(APP_DIR), 'CUT_ICON': str(ICON_FILE), 'CUT_DESC': APP_NAME,
        'CUT_APPID': app_id or '', 'CUT_CS': SET_APP_ID_CS,
    }
    proc = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                          env=env, creationflags=NO_WINDOW, timeout=60, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        log(f'shortcut {lnk.name} failed ({proc.returncode}): {proc.stderr.strip()[:300]}')


def slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'account'


def home_relative(path: Path) -> str:
    try:
        return '~/' + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


# Codex app-server


class CodexError(Exception):
    pass


def _read_lines(stream, lines: queue.Queue) -> None:
    try:
        for line in stream:
            lines.put(line)
    finally:
        lines.put(None)


def _rpc(proc: subprocess.Popen, lines: queue.Queue, request_id: int, method: str, params=None) -> dict:
    message = {'id': request_id, 'method': method}
    if params is not None:
        message['params'] = params
    try:
        proc.stdin.write(json.dumps(message) + '\n')
        proc.stdin.flush()
    except OSError as e:
        raise CodexError('lost connection to Codex') from e
    deadline = time.monotonic() + CODEX_TIMEOUT
    while True:
        try:
            line = lines.get(timeout=max(deadline - time.monotonic(), 0.01))
        except queue.Empty:
            raise CodexError(f'Codex timed out on {method}') from None
        if line is None:
            raise CodexError('Codex stopped unexpectedly')
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if not isinstance(reply, dict) or reply.get('id') != request_id:
            continue  # notifications and unrelated replies
        if 'error' in reply:
            error = reply['error']
            raise CodexError(str(error.get('message') if isinstance(error, dict) else error))
        result = reply.get('result')
        return result if isinstance(result, dict) else {}


def codex_read(env: dict) -> tuple[dict | None, dict | None]:
    """Ask a short-lived `codex app-server` for the signed-in account and its rate limits."""
    try:
        proc = subprocess.Popen(
            [CODEX_CLI, 'app-server', '--stdio'], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=NO_WINDOW,
        )
    except OSError as e:
        raise CodexError(f'could not start Codex: {e}') from e
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_read_lines, args=(proc.stdout, lines), daemon=True).start()
    try:
        _rpc(proc, lines, 1, 'initialize', {'clientInfo': {'name': 'claude-usage-tracker', 'version': '1'},
                                            'capabilities': {'experimentalApi': False}})
        account = _rpc(proc, lines, 2, 'account/read', {'refreshToken': False}).get('account')
        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
            return account if isinstance(account, dict) else None, None
        return account, _rpc(proc, lines, 3, 'account/rateLimits/read')
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # codex.cmd runs node underneath; kill the whole tree, not just cmd.exe.
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                           capture_output=True, creationflags=NO_WINDOW, check=False)


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
    """Pink tile with the first window's percentage on top and both windows as bars below."""
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


# Accounts


class Account:
    """One signed-in CLI login, identified by its config folder."""

    kind = ''
    label = ''
    env_var = ''
    folder_prefix = ''          # extra accounts live in ~/<prefix><name>
    default_dir = Path.home()
    cli: str | None = None
    login_args: tuple[str, ...] = ()

    def __init__(self, name: str, folder: str | None):
        self.name = name
        self.custom_dir = Path(os.path.expanduser(folder)) if folder else None
        self.dir = self.custom_dir or self.default_dir
        self.result: dict = {'status': 'loading'}
        self.backoff = 0
        self.backoff_until = 0.0

    def env(self) -> dict:
        env = os.environ.copy()
        if self.custom_dir:
            env[self.env_var] = str(self.custom_dir)
        else:
            env.pop(self.env_var, None)
        return env

    def to_setting(self) -> dict:
        return {'name': self.name, 'kind': self.kind, 'dir': home_relative(self.custom_dir) if self.custom_dir else None}

    def deletable_folder(self) -> Path | None:
        """The login folder Remove may delete: only ~/<prefix>* folders, never the CLI's main one."""
        if not self.custom_dir:
            return None
        try:
            rel = self.custom_dir.relative_to(Path.home())
        except ValueError:
            return None
        return self.custom_dir if len(rel.parts) == 1 and rel.name.startswith(self.folder_prefix) else None

    def has_login(self) -> bool:
        raise NotImplementedError

    def fetch(self, force: bool) -> None:
        raise NotImplementedError

    def _transient_failure(self, note: str, wait: int) -> None:
        """Back off and keep the last good numbers, with a note about why they may be stale."""
        self.backoff = wait
        self.backoff_until = time.time() + wait
        if self.result.get('status') == 'ok':
            self.result = {**self.result, 'note': note}
        else:
            self.result = {'status': 'error', 'message': note}


class ClaudeAccount(Account):
    kind = 'claude'
    label = 'Claude'
    env_var = 'CLAUDE_CONFIG_DIR'
    folder_prefix = '.claude-'
    default_dir = Path.home() / '.claude'
    cli = CLAUDE_CLI
    login_args = ('auth', 'login')

    def __init__(self, name: str, folder: str | None):
        super().__init__(name, folder)
        self.last_renew = 0.0

    def read_oauth(self) -> dict | None:
        try:
            data = json.loads((self.dir / '.credentials.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None  # also covers a read racing the CLI rewriting the file
        oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
        return oauth if isinstance(oauth, dict) and oauth.get('accessToken') else None

    def has_login(self) -> bool:
        oauth = self.read_oauth()
        return bool(oauth) and not self.expiring(oauth, ahead=0)

    @staticmethod
    def expiring(oauth: dict, ahead: int = RENEW_AHEAD) -> bool:
        expires = oauth.get('expiresAt')
        if not isinstance(expires, (int, float)):
            return False
        return expires / 1000 <= time.time() + ahead

    def renew(self) -> bool:
        """Have the Claude CLI renew this account's login. True if the token changed."""
        if time.time() - self.last_renew < RENEW_COOLDOWN:
            return False
        self.last_renew = time.time()
        before = (self.read_oauth() or {}).get('accessToken')
        for args in RENEW_COMMANDS:
            proc = run_cli(CLAUDE_CLI, args, self.env())
            if args == ['auth', 'status'] and proc and '"loggedIn": false' in proc.stdout:
                log(f'{self.name}: Claude Code reports this account is signed out')
                return False
            oauth = self.read_oauth()
            if oauth and oauth.get('accessToken') != before and not self.expiring(oauth):
                log(f'{self.name}: login renewed via `claude {" ".join(args)}`')
                return True
        log(f'{self.name}: login renewal produced no new token')
        return False

    def fetch(self, force: bool) -> None:
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

        status, body, retry_after = http_get_usage(oauth['accessToken'])
        if status == 401 and self.renew():
            oauth = self.read_oauth() or oauth
            status, body, retry_after = http_get_usage(oauth['accessToken'])

        plan = str(oauth.get('subscriptionType') or '').title()
        if status == 200 and isinstance(body, dict):
            self.backoff = 0
            self.backoff_until = 0.0
            self.result = {'status': 'ok', 'limits': parse_claude_usage(body), 'plan': plan}
            return
        if status == 401:
            self.result = {'status': 'signed_out'}
            return
        log(f'{self.name}: usage request failed with status {status}')
        if status == 429:
            wait = retry_after or min(max(self.backoff * 2, 60), MAX_BACKOFF)
            self._transient_failure(f'Rate limited · retrying in {fmt_left(wait)}', wait)
        else:
            self._transient_failure('Offline · retrying' if status == 0 else f'Server error {status} · retrying', 60)


class ChatGPTAccount(Account):
    kind = 'chatgpt'
    label = 'ChatGPT'
    env_var = 'CODEX_HOME'
    folder_prefix = '.codex-'
    default_dir = Path.home() / '.codex'
    cli = CODEX_CLI
    login_args = ('login',)

    def has_login(self) -> bool:
        return (self.dir / 'auth.json').exists()

    def fetch(self, force: bool) -> None:
        if not force and time.time() < self.backoff_until:
            return
        if not CODEX_CLI:
            self.result = {'status': 'error', 'message': 'Codex CLI not found (npm i -g @openai/codex)'}
            return
        try:
            account, payload = codex_read(self.env())
        except CodexError as e:
            if 'authentication required' in str(e).lower():
                self.result = {'status': 'signed_out'}
                return
            log(f'{self.name}: codex request failed: {e}')
            self._transient_failure('Codex unavailable · retrying', 60)
            return
        if account is None:
            self.result = {'status': 'signed_out'}
            return
        if account.get('type') != 'chatgpt':
            self.result = {'status': 'error', 'message': 'Codex uses an API key here. Sign in with ChatGPT to see plan limits.'}
            return
        self.backoff = 0
        self.backoff_until = 0.0
        self.result = {'status': 'ok', 'limits': parse_codex_limits(payload),
                       'plan': str(account.get('planType') or '').title()}


ACCOUNT_KINDS = {cls.kind: cls for cls in (ClaudeAccount, ChatGPTAccount)}


def make_account(entry: dict) -> Account | None:
    cls = ACCOUNT_KINDS.get(entry.get('kind'))
    name = str(entry.get('name') or '').strip()
    return cls(name, entry.get('dir')) if cls and name else None


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
        win.bind('<MouseWheel>', self._wheel)

        header = tk.Frame(win, bg=BG)
        header.pack(fill='x', padx=self.px(14), pady=(self.px(12), 0))
        tk.Label(header, text='■', fg=ACCENT, bg=BG, font=('Segoe UI', 11)).pack(side='left')
        tk.Label(header, text='Usage limits', fg=FG, bg=BG, font=('Segoe UI Semibold', 11)).pack(side='left', padx=(self.px(4), 0))
        self._link(header, '✕', self.hide, size=11).pack(side='right')
        self._link(header, '↻ Refresh', self.app.refresh, size=9).pack(side='right', padx=(0, self.px(12)))

        self.canvas = tk.Canvas(win, bg=BG, highlightthickness=0, yscrollincrement=self.px(20))
        self.canvas.pack(padx=self.px(14))
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window(0, 0, window=self.inner, anchor='nw')
        self.scrollable = False

        self.footer = tk.Label(win, fg=DIM, bg=BG, font=('Segoe UI', 8))
        self.footer.pack(anchor='w', padx=self.px(14), pady=(self.px(8), self.px(12)))
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
        self.canvas.yview_moveto(0)
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

    def _wheel(self, event) -> None:
        if self.scrollable:
            self.canvas.yview_scroll(int(-event.delta / 120), 'units')

    # Drawing

    def render(self) -> None:
        top = self.canvas.yview()[0]
        for child in self.inner.winfo_children():
            child.destroy()
        now = datetime.now(timezone.utc)

        cards = [self._account_card(account, now) for account in self.app.accounts]
        if not cards:
            empty = tk.Frame(self.inner, bg=PANEL, padx=self.px(12), pady=self.px(10))
            empty.pack(fill='x', pady=(self.px(10), 0))
            tk.Label(empty, text='No accounts yet.', fg=DIM, bg=PANEL, font=('Segoe UI', 9),
                     width=round(BAR_WIDTH / 7), anchor='w').pack(anchor='w')
            self._link(empty, 'Add account', self.app.open_add, size=9, bg=PANEL).pack(anchor='w', pady=(self.px(4), 0))

        self.inner.update_idletasks()
        width, height = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        visible = height
        if len(cards) > SCROLL_AFTER:
            visible = sum(card.winfo_reqheight() + self.px(10) for card in cards[:SCROLL_AFTER])
        _l, area_top, _r, area_bottom = work_area()
        visible = min(visible, area_bottom - area_top - self.px(140))
        self.scrollable = height > visible
        self.footer.configure(text='Refreshing…' if self.app.refreshing else self._footer_text())
        self.canvas.configure(width=width, height=visible, scrollregion=(0, 0, width, height))
        self.canvas.yview_moveto(top)
        self._place()

    def _footer_text(self) -> str:
        if not self.app.last_poll:
            return 'Loading…'
        updated = datetime.fromtimestamp(self.app.last_poll)
        nxt = datetime.fromtimestamp(self.app.next_poll)
        suffix = ' · scroll for more' if self.scrollable else ''
        return f'Updated {updated:%H:%M} · next check {nxt:%H:%M}{suffix}'

    def _account_card(self, account: Account, now: datetime) -> tk.Frame:
        card = tk.Frame(self.inner, bg=PANEL, padx=self.px(12), pady=self.px(10))
        card.pack(fill='x', pady=(self.px(10), 0))
        result = account.result

        top = tk.Frame(card, bg=PANEL)
        top.pack(fill='x')
        tk.Label(top, text=account.name, fg=FG, bg=PANEL, font=('Segoe UI Semibold', 10)).pack(side='left')
        if account.name == self.app.tray_name:
            tk.Label(top, text=' in tray ', fg=BG, bg=ACCENT, font=('Segoe UI Semibold', 7)).pack(side='left', padx=(self.px(6), 0))
        else:
            self._link(top, 'show in tray', lambda n=account.name: self.app.set_tray(n), size=8, bg=PANEL, fg=DIM).pack(side='left', padx=(self.px(6), 0))
        kind = account.label + (f' · {result["plan"]}' if result.get('plan') else '')
        tk.Label(top, text=kind, fg=DIM, bg=PANEL, font=('Segoe UI', 8)).pack(side='right')

        status = result['status']
        if status == 'loading':
            self._text(card, 'Loading…', DIM)
        elif status == 'signed_out':
            self._text(card, 'Not signed in.', DIM)
            self._link(card, 'Sign in', lambda a=account: self.app.sign_in(a), size=9, bg=PANEL).pack(anchor='w', pady=(self.px(4), 0))
        elif status == 'error':
            self._text(card, result['message'], WARN)
        else:
            for limit in result['limits']:
                self._bar(card, limit, now)
            if result.get('note'):
                self._text(card, result['note'], WARN, size=8)
        return card

    def _text(self, parent: tk.Frame, text: str, fg: str, size: int = 9) -> None:
        tk.Label(parent, text=text, fg=fg, bg=PANEL, font=('Segoe UI', size), justify='left',
                 wraplength=self.px(BAR_WIDTH)).pack(anchor='w', pady=(self.px(6), 0))

    def _bar(self, parent: tk.Frame, limit: dict, now: datetime) -> None:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', pady=(self.px(8), 0))
        tk.Label(row, text=limit['label'], fg=FG, bg=PANEL, font=('Segoe UI', 9)).pack(side='left')
        ahead = is_ahead(limit, now)
        tk.Label(row, text=fmt_pct(limit), fg=WARN if ahead else FG, bg=PANEL, font=('Segoe UI Semibold', 9)).pack(side='right')

        width, height, pad = self.px(BAR_WIDTH), self.px(12), self.px(2)
        canvas = tk.Canvas(parent, width=width, height=height, bg=PANEL, highlightthickness=0)
        canvas.pack(anchor='w', pady=(self.px(3), 0))
        canvas.create_rectangle(0, pad, width, height - pad, fill=TRACK, width=0)
        if limit['pct']:
            canvas.create_rectangle(0, pad, width * min(limit['pct'], 100) / 100, height - pad,
                                    fill=WARN if ahead else ACCENT, width=0)
        elapsed = elapsed_fraction(limit, now)
        if elapsed is not None:
            x = max(1, min(width - 1, round(width * elapsed)))
            canvas.create_line(x, 0, x, height, fill='white', width=max(1, self.px(2)))

        tk.Label(parent, text=fmt_reset(limit['resets'], now), fg=DIM, bg=PANEL, font=('Segoe UI', 8)).pack(anchor='w')

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


# Dialogs


class Dialog:
    """A small, normal window (title bar, taskbar button) in the popup's colours."""

    def __init__(self, app: 'App', title: str):
        self.app = app
        self.px = app.popup.px
        win = self.win = tk.Toplevel(app.root)
        win.title(title)
        win.configure(bg=BG)
        win.resizable(False, False)
        win.attributes('-topmost', True)
        if ICON_FILE.exists():
            win.iconbitmap(str(ICON_FILE))
        win.bind('<Escape>', lambda _e: self.close())
        win.protocol('WM_DELETE_WINDOW', self.close)
        self.body = tk.Frame(win, bg=BG, padx=self.px(18), pady=self.px(16))
        self.body.pack(fill='both', expand=True)

    def center(self) -> None:
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        left, top, right, bottom = work_area()
        self.win.geometry(f'+{left + (right - left - w) // 2}+{top + (bottom - top - h) // 2}')
        self.win.lift()
        self.win.focus_force()

    def close(self) -> None:
        self.win.destroy()

    def button(self, parent, text, command, primary=False) -> tk.Button:
        return tk.Button(parent, text=text, command=command, relief='flat', cursor='hand2',
                         font=('Segoe UI Semibold', 9), padx=self.px(12), pady=self.px(3),
                         bg=ACCENT if primary else PANEL, fg='white' if primary else FG,
                         activebackground=ACCENT, activeforeground='white', bd=0)


class AddDialog(Dialog):
    def __init__(self, app: 'App'):
        super().__init__(app, 'Add account')
        b = self.body
        tk.Label(b, text='Add an account', fg=FG, bg=BG, font=('Segoe UI Semibold', 11)).pack(anchor='w')

        self.kind = tk.StringVar(value='claude')
        kinds = tk.Frame(b, bg=BG)
        kinds.pack(anchor='w', pady=(self.px(10), 0))
        for kind, cls in ACCOUNT_KINDS.items():
            tk.Radiobutton(kinds, text=cls.label, value=kind, variable=self.kind, fg=FG, bg=BG,
                           selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                           font=('Segoe UI', 9)).pack(side='left', padx=(0, self.px(14)))

        tk.Label(b, text='Name (e.g. Work, Personal)', fg=DIM, bg=BG, font=('Segoe UI', 9)).pack(anchor='w', pady=(self.px(10), self.px(3)))
        self.name = tk.Entry(b, width=32, fg=FG, bg=PANEL, insertbackground=FG, relief='flat', font=('Segoe UI', 10))
        self.name.pack(anchor='w', ipady=self.px(4), fill='x')
        self.name.bind('<Return>', lambda _e: self.submit())
        self.error = tk.Label(b, text='', fg=WARN, bg=BG, font=('Segoe UI', 8))
        self.error.pack(anchor='w', pady=(self.px(4), 0))
        tk.Label(b, text='A sign-in window opens next. Finish signing in there, then close it.',
                 fg=DIM, bg=BG, font=('Segoe UI', 8), justify='left', wraplength=self.px(280)).pack(anchor='w')

        buttons = tk.Frame(b, bg=BG)
        buttons.pack(anchor='e', pady=(self.px(14), 0))
        self.button(buttons, 'Cancel', self.close).pack(side='right')
        self.button(buttons, 'Add and sign in', self.submit, primary=True).pack(side='right', padx=(0, self.px(8)))
        self.center()
        self.name.focus_set()

    def submit(self) -> None:
        name = self.name.get().strip()
        if not name:
            self.error.configure(text='Type a name for this account.')
        elif len(name) > 24:
            self.error.configure(text='Keep the name under 25 characters.')
        elif any(a.name.lower() == name.lower() for a in self.app.accounts):
            self.error.configure(text='That name is already used.')
        elif not ACCOUNT_KINDS[self.kind.get()].cli:
            self.error.configure(text=f'The {ACCOUNT_KINDS[self.kind.get()].label} CLI is not installed.')
        else:
            account = self.app.add_account(self.kind.get(), name)
            self.close()
            if not account.has_login():
                self.app.sign_in(account)


class ManageDialog(Dialog):
    def __init__(self, app: 'App'):
        super().__init__(app, 'Manage accounts')
        self.render()
        self.center()

    def render(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        b = self.body
        tk.Label(b, text='Accounts', fg=FG, bg=BG, font=('Segoe UI Semibold', 11)).pack(anchor='w')
        if not self.app.accounts:
            tk.Label(b, text='No accounts yet.', fg=DIM, bg=BG, font=('Segoe UI', 9)).pack(anchor='w', pady=(self.px(8), 0))
        for account in self.app.accounts:
            row = tk.Frame(b, bg=PANEL, padx=self.px(12), pady=self.px(8))
            row.pack(fill='x', pady=(self.px(8), 0))
            text = tk.Frame(row, bg=PANEL)
            text.pack(side='left', fill='x', expand=True)
            tk.Label(text, text=account.name, fg=FG, bg=PANEL, font=('Segoe UI Semibold', 10)).pack(anchor='w')
            signed_in = account.result.get('status') not in ('signed_out', 'loading') or account.has_login()
            state = 'signed in' if signed_in else 'not signed in'
            tk.Label(text, text=f'{account.label} · {state} · {home_relative(account.dir)}', fg=DIM, bg=PANEL,
                     font=('Segoe UI', 8)).pack(anchor='w')
            self.button(row, 'Remove', lambda a=account: self.remove(a)).pack(side='right')
            if not signed_in:
                self.button(row, 'Sign in', lambda a=account: self.app.sign_in(a), primary=True).pack(side='right', padx=(0, self.px(6)))

        buttons = tk.Frame(b, bg=BG)
        buttons.pack(fill='x', pady=(self.px(14), 0))
        self.button(buttons, 'Close', self.close).pack(side='right')
        self.button(buttons, 'Add account…', self.app.open_add, primary=True).pack(side='right', padx=(0, self.px(8)))

    def remove(self, account: Account) -> None:
        folder = account.deletable_folder()
        if folder:
            text = (f"Remove '{account.name}' and sign it out?\n\n"
                    f'This deletes its login folder:\n{home_relative(folder)}')
        else:
            text = (f"Remove '{account.name}' from the tracker?\n\n"
                    f'Its login folder ({home_relative(account.dir)}) is the main {account.label} one, '
                    'so it is kept and stays signed in.')
        if messagebox.askyesno('Remove account', text, parent=self.win, icon='warning'):
            self.app.remove_account(account)

    def close(self) -> None:
        self.app.manage = None
        super().close()


# App


class App:
    def __init__(self):
        self.settings = load_settings()
        self.accounts: list[Account] = []
        for entry in self.settings.get('accounts') or []:
            account = make_account(entry) if isinstance(entry, dict) else None
            if account and all(a.name != account.name for a in self.accounts):
                self.accounts.append(account)
        names = [a.name for a in self.accounts]
        tray = self.settings.get('tray_account')
        self.tray_name = tray if tray in names else (names[0] if names else None)
        self.refreshing = False
        self.last_poll = 0.0
        self.next_poll = 0.0
        self.stopping = False
        self.force = threading.Event()
        self.ui = queue.Queue()
        self.manage: ManageDialog | None = None
        self.add: AddDialog | None = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = Popup(self, self.root)
        self.icon = pystray.Icon('claude-usage-tracker', render_icon(64), APP_NAME, pystray.Menu(self._menu_items))

    # Tray (runs on pystray's thread; anything touching Tk goes through self.ui)

    def _menu_items(self):
        yield pystray.MenuItem('Show usage', self._send('toggle'), default=True)
        yield pystray.MenuItem('Refresh now', lambda _icon, _item: self.refresh())
        yield pystray.Menu.SEPARATOR
        for account in self.accounts:
            yield pystray.MenuItem(f'Tray shows: {account.name}', self._tray_action(account.name),
                                   checked=self._tray_checked(account.name), radio=True)
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem('Add account…', self._send('add'))
        yield pystray.MenuItem('Manage accounts…', self._send('manage'))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem('Start at login', self._on_startup, checked=lambda _item: STARTUP_LNK.exists())
        yield pystray.MenuItem('Uninstall…', self._send('uninstall'))
        yield pystray.MenuItem('Quit', self._send('quit'))

    def _send(self, message: str):
        def action(_icon, _item):
            self.ui.put(message)
        return action

    def _tray_action(self, name: str):
        def action(_icon, _item):
            self.set_tray(name)
        return action

    def _tray_checked(self, name: str):
        def checked(_item):
            return self.tray_name == name
        return checked

    def _on_startup(self, _icon, _item):
        if STARTUP_LNK.exists():
            STARTUP_LNK.unlink(missing_ok=True)
        else:
            if not ICON_FILE.exists():
                write_icon_file()
            make_shortcut(STARTUP_LNK)

    # State changes

    def save(self) -> None:
        self.settings['accounts'] = [a.to_setting() for a in self.accounts]
        self.settings['tray_account'] = self.tray_name
        save_settings(self.settings)

    def accounts_changed(self) -> None:
        self.save()
        self.icon.update_menu()
        self._update_tray()
        self.ui.put('render')

    def set_tray(self, name: str) -> None:
        self.tray_name = name
        self.accounts_changed()

    def refresh(self) -> None:
        self.force.set()

    def add_account(self, kind: str, name: str) -> Account:
        cls = ACCOUNT_KINDS[kind]
        used = {a.dir for a in self.accounts if a.kind == kind}
        folder = None
        if cls.default_dir in used:
            base = f'{cls.folder_prefix}{slugify(name)}'
            candidate, n = base, 2
            while Path.home() / candidate in used:
                candidate, n = f'{base}-{n}', n + 1
            folder = f'~/{candidate}'
        account = cls(name, folder)
        account.dir.mkdir(parents=True, exist_ok=True)
        self.accounts.append(account)
        if self.tray_name is None:
            self.tray_name = name
        log(f'added {cls.label} account {name} at {home_relative(account.dir)}')
        self.accounts_changed()
        self.refresh()
        return account

    def remove_account(self, account: Account) -> None:
        self.accounts = [a for a in self.accounts if a is not account]
        folder = account.deletable_folder()
        if folder and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        log(f'removed {account.label} account {account.name}' + (f', deleted {home_relative(folder)}' if folder else ''))
        if self.tray_name == account.name:
            self.tray_name = self.accounts[0].name if self.accounts else None
        self.accounts_changed()

    def sign_in(self, account: Account) -> None:
        """Open a console running the CLI's login for this account, then refresh when it closes."""
        if not account.cli:
            messagebox.showerror('Sign in', f'The {account.label} CLI is not installed.')
            return
        env = account.env() | {'CUT_NAME': account.name, 'CUT_KIND': account.label}
        command = (
            'Write-Host "Signing in $env:CUT_KIND account: $env:CUT_NAME" -ForegroundColor Magenta; '
            f'& "{account.cli}" {" ".join(account.login_args)}; '
            "Read-Host 'Done. Press Enter to close this window'"
        )
        try:
            proc = subprocess.Popen(['powershell', '-NoProfile', '-Command', command], env=env,
                                    creationflags=subprocess.CREATE_NEW_CONSOLE)
        except OSError as e:
            log(f'could not open sign-in window: {e}')
            return

        def wait_then_refresh():
            proc.wait()
            self.refresh()
        threading.Thread(target=wait_then_refresh, daemon=True).start()

    def open_add(self) -> None:
        if self.add and self.add.win.winfo_exists():
            self.add.win.lift()
            return
        self.popup.hide()
        self.add = AddDialog(self)

    def open_manage(self) -> None:
        if self.manage and self.manage.win.winfo_exists():
            self.manage.win.lift()
            return
        self.popup.hide()
        self.manage = ManageDialog(self)

    def uninstall(self) -> None:
        text = ('Uninstall Claude Usage Tracker?\n\nThis quits the tracker and removes its shortcuts. '
                'A window then asks whether to delete the extra account login folders and the app folder.')
        if not self.ask('Uninstall', text):
            return
        subprocess.Popen(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(UNINSTALL_SCRIPT)],
                         cwd=str(Path.home()), creationflags=subprocess.CREATE_NEW_CONSOLE)
        self.quit()

    def ask(self, title: str, text: str) -> bool:
        """Yes/No prompt that opens on top: tray actions have no visible window to own it."""
        owner = tk.Toplevel(self.root)
        owner.withdraw()
        owner.attributes('-topmost', True)
        try:
            return messagebox.askyesno(title, text, parent=owner, icon='warning')
        finally:
            owner.destroy()

    def quit(self) -> None:
        self.stopping = True
        self.force.set()
        self.icon.stop()
        self.root.destroy()

    def _update_tray(self) -> None:
        account = next((a for a in self.accounts if a.name == self.tray_name), None)
        if account is None:
            self.icon.icon = render_icon(64, state='error')
            self.icon.title = 'No accounts · right-click → Add account'
            return
        result = account.result
        if result['status'] == 'ok':
            now = datetime.now(timezone.utc)
            limits = (result['limits'] + [None, None])[:2]
            first, second = limits
            self.icon.icon = render_icon(64, first['pct'] if first else None, is_ahead(first, now),
                                         second['pct'] if second else None, is_ahead(second, now))
            parts = [account.name]
            for limit in result['limits'][:2]:
                left = f' ({fmt_left((limit["resets"] - now).total_seconds())})' if limit['resets'] else ''
                parts.append(f'{short_label(limit["label"])} {fmt_pct(limit)}{left}')
            title = ' · '.join(parts)
            if result.get('note'):
                title += f'\n{result["note"]}'
        elif result['status'] == 'loading':
            self.icon.icon = render_icon(64)
            title = f'{account.name} · loading…'
        else:
            self.icon.icon = render_icon(64, state='error')
            title = f'{account.name} · ' + ('not signed in' if result['status'] == 'signed_out' else result['message'])
        self.icon.title = title[:127]

    # Polling

    def _interval(self) -> float:
        base = self.settings.get('poll_seconds', 180)
        if idle_seconds() > IDLE_AFTER:
            base = self.settings.get('idle_poll_seconds', 900)
        # Wake just after the next reset so a refilled window shows up promptly.
        now = datetime.now(timezone.utc)
        for account in list(self.accounts):
            for limit in account.result.get('limits') or []:
                if limit['resets']:
                    until = (limit['resets'] - now).total_seconds() + 5
                    if 0 < until < base:
                        base = until
        return max(float(base), 30.0)

    def _worker(self) -> None:
        global CLAUDE_UA
        CLAUDE_UA = claude_user_agent()
        while True:
            forced = self.force.wait(timeout=15)
            if self.stopping:
                return
            self.force.clear()
            if not forced and time.time() < self.next_poll:
                continue
            self.refreshing = True
            self.ui.put('render')
            for account in list(self.accounts):
                try:
                    account.fetch(force=forced)
                except Exception as e:  # keep the tray alive whatever happens
                    log(f'{account.name}: unexpected error {type(e).__name__}: {e}')
                    account.result = {'status': 'error', 'message': 'Unexpected error, see tracker.log'}
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
                    if self.ask('Quit', 'Quit Claude Usage Tracker?\n\nUsage stops updating until you '
                                'open it again from the Start Menu.'):
                        self.quit()
                        return
                if message == 'toggle':
                    self.popup.toggle()
                elif message == 'add':
                    self.open_add()
                elif message == 'manage':
                    self.open_manage()
                elif message == 'uninstall':
                    self.uninstall()
                    if self.stopping:
                        return
                elif message == 'render':
                    if self.popup.visible:
                        self.popup.render()
                    if self.manage and self.manage.win.winfo_exists():
                        self.manage.render()
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def run(self) -> None:
        self._update_tray()
        self.icon.run_detached()
        threading.Thread(target=self._worker, daemon=True).start()
        self.force.set()
        self.root.after(100, self._pump)
        self.root.mainloop()


def setup() -> None:
    """Write icon, settings and shortcuts. Used by install.ps1."""
    write_icon_file()
    load_settings()
    make_shortcut(START_MENU_LNK, APP_ID)
    make_shortcut(STARTUP_LNK)
    make_shortcut(FOLDER_LNK)
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
