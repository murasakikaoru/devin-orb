"""Settings persistence + theme/display sync with OpenUsage + autostart."""

from __future__ import annotations

import json
import os
import sys

APP_NAME = "devin-orb"


def _config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expandvars(r"%APPDATA%")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


CONFIG_PATH = os.path.join(_config_dir(), "config.json")

DEFAULTS = {
    # Orb position (logical px, top-left of the 68px window)
    "orb_x": None,
    "orb_y": None,
    # Ring semantics: which quota drives the ring, and what it shows.
    "ring_source": "auto",      # auto | daily | weekly
    "ring_mode": "follow",      # follow (openusage displayMode) | left | used
    # Theme: follow openusage themeMode | system | light | dark
    "theme": "follow",
    # New setting: double-click orb pins the panel on top (no blur-hide).
    "dblclick_pin": True,
    "language": "zh",           # zh | en
    "start_on_login": False,
    "devin_hook": True,           # SessionStart hook -> launch orb with Devin
    "refresh_secs": 300,
    "panel_pinned": False,      # runtime state, persisted for convenience
}

_cache: dict | None = None


def load() -> dict:
    global _cache
    if _cache is not None:
        return dict(_cache)
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
        for k in cfg:
            if k in stored:
                cfg[k] = stored[k]
    except Exception:
        pass
    _cache = cfg
    return dict(cfg)


def save(cfg: dict) -> None:
    global _cache
    _cache = dict(cfg)
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


def update(**kw) -> dict:
    cfg = load()
    cfg.update(kw)
    save(cfg)
    return cfg


# --- OpenUsage settings sync -------------------------------------------------

OPENUSAGE_SETTINGS = os.path.join(
    os.environ.get("APPDATA", ""), "com.mesomya.openusage", "settings.json"
)


def openusage_settings() -> dict:
    try:
        with open(OPENUSAGE_SETTINGS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def openusage_mtime() -> float:
    try:
        return os.path.getmtime(OPENUSAGE_SETTINGS)
    except OSError:
        return 0.0


def system_is_light() -> bool:
    """Windows: AppsUseLightTheme (1=light, 0=dark). Default light elsewhere."""
    if sys.platform != "win32":
        return True
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(val)
    except Exception:
        return True


def effective_theme(cfg: dict, ou: dict | None = None) -> str:
    """Resolve to 'light' | 'dark'."""
    mode = cfg.get("theme", "follow")
    if mode == "follow":
        mode = (ou if ou is not None else openusage_settings()).get("themeMode", "system")
    if mode == "system" or mode not in ("light", "dark"):
        return "light" if system_is_light() else "dark"
    return mode


def effective_ring_mode(cfg: dict, ou: dict | None = None) -> str:
    """Resolve to 'left' | 'used'."""
    mode = cfg.get("ring_mode", "follow")
    if mode == "follow":
        mode = (ou if ou is not None else openusage_settings()).get("displayMode", "left")
    return mode if mode in ("left", "used") else "left"


# --- Autostart ---------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DevinOrb"


def set_autostart(enabled: bool, script_path: str) -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        key = winreg.OpenKey(RUN_KEY, None, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                              _launcher(script_path))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        key = winreg.OpenKey(RUN_KEY)
        winreg.QueryValueEx(key, RUN_VALUE)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


# --- Devin SessionStart hook injection ---------------------------------------

DEVIN_CONFIG = os.path.join(
    os.environ.get("APPDATA", ""), "devin", "config.json"
)
_HOOK_MARKER = "devin-orb"


def _launcher(script_path: str) -> str:
    """The command that launches the app — works both for source and frozen exe."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pythonw if os.path.isfile(pythonw) else sys.executable
    return f'"{exe}" "{script_path}"'


def _hook_command(script_path: str) -> str:
    # Devin CLI runs command hooks through its own shell (Git Bash on
    # Windows) where `cmd /c start` gets path-mangled, and inline
    # Start-Process quoting breaks on paths containing spaces.
    # A generated .ps1 keeps the hook command to a single quoted argument
    # that works from cmd, bash, or direct exec, and Start-Process detaches
    # the orb so the hook exits immediately and can't be killed on timeout.
    launcher = _launcher(script_path)  # '"exe"' or '"exe" "script"'
    parts = [p.strip('"') for p in launcher.split('"') if p.strip('" ')]
    exe, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    ps = f"Start-Process -FilePath '{exe}'"
    if arg:
        ps += f" -ArgumentList '\"{arg}\"'"
    ps1 = os.path.join(os.path.dirname(CONFIG_PATH), "start-orb.ps1")
    try:
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(ps + "\n")
    except Exception:
        pass
    return ('powershell -NoProfile -ExecutionPolicy Bypass '
            f'-WindowStyle Hidden -File "{ps1}"')


def _read_devin_config() -> dict:
    try:
        with open(DEVIN_CONFIG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_devin_config(data: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(DEVIN_CONFIG), exist_ok=True)
        tmp = DEVIN_CONFIG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DEVIN_CONFIG)
        return True
    except Exception:
        return False


def devin_hook_installed() -> bool:
    for grp in _read_devin_config().get("hooks", {}).get("SessionStart", []):
        for h in grp.get("hooks", []):
            if _HOOK_MARKER in str(h.get("command", "")):
                return True
    return False


def install_devin_hook(script_path: str) -> bool:
    """Add a SessionStart command hook that launches the orb (idempotent,
    and refreshes the stored command when the launcher path changes,
    e.g. source -> packaged exe)."""
    want = _hook_command(script_path)
    cfg = _read_devin_config()
    groups = cfg.get("hooks", {}).get("SessionStart", [])
    for grp in groups:
        for h in grp.get("hooks", []):
            if _HOOK_MARKER in str(h.get("command", "")):
                if h.get("command") != want:
                    h["command"] = want
                    return _write_devin_config(cfg)
                return True
    hooks = cfg.setdefault("hooks", {})
    entries = hooks.setdefault("SessionStart", [])
    entries.append({
        "hooks": [{"type": "command",
                   "command": want, "timeout": 10}]
    })
    return _write_devin_config(cfg)


def remove_devin_hook() -> bool:
    """Remove only the orb's SessionStart hook entries; keep everything else."""
    cfg = _read_devin_config()
    groups = cfg.get("hooks", {}).get("SessionStart")
    if not groups:
        return True
    kept = []
    for grp in groups:
        hs = [h for h in grp.get("hooks", [])
              if _HOOK_MARKER not in str(h.get("command", ""))]
        if hs:
            grp = dict(grp, hooks=hs)
            kept.append(grp)
        elif grp.get("matcher"):
            kept.append(grp)
    if kept:
        cfg["hooks"]["SessionStart"] = kept
    else:
        cfg["hooks"].pop("SessionStart", None)
        if not cfg["hooks"]:
            cfg.pop("hooks")
    return _write_devin_config(cfg)
