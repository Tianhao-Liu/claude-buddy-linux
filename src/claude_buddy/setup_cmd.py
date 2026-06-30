"""`claude-buddy install` / `uninstall`: systemd --user service + Claude Code
hooks wired into ~/.claude/settings.json."""
import json
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from . import config as cfgmod

SETTINGS = Path.home() / ".claude" / "settings.json"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_NAME = "claude-buddy.service"

# event -> tool matcher ("" = all tools; None = event takes no matcher)
HOOK_EVENTS = {
    "PreToolUse": "",
    "PostToolUse": "",
    "UserPromptSubmit": None,
    "Stop": None,
    "SessionStart": None,
    "SessionEnd": None,
    "Notification": None,
}


def _exe() -> str:
    """Absolute path to the installed `claude-buddy` console script."""
    w = shutil.which("claude-buddy")
    if w:
        return str(Path(w).resolve())
    # running as `python -m claude_buddy` or from sys.argv[0]
    cand = Path(sys.argv[0]).resolve()
    if cand.name == "claude-buddy":
        return str(cand)
    return f"{sys.executable} -m claude_buddy"


def _is_ours(item: dict) -> bool:
    for h in item.get("hooks", []) if isinstance(item, dict) else []:
        cmd = h.get("command", "")
        if "claude-buddy" in cmd and cmd.rstrip().endswith("hook"):
            return True
        if "claude_buddy" in cmd and "hook" in cmd:
            return True
    return False


def _merge_hooks(remove_only: bool):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if not SETTINGS.exists():
        SETTINGS.write_text("{}")
    shutil.copy(SETTINGS, str(SETTINGS) + ".bak")
    cfg = json.loads(SETTINGS.read_text())
    hooks = cfg.setdefault("hooks", {})
    cmd = f'"{_exe()}" hook'
    for ev, matcher in HOOK_EVENTS.items():
        lst = [it for it in hooks.get(ev, []) if not _is_ours(it)]
        if not remove_only:
            entry = {"hooks": [{"type": "command", "command": cmd}]}
            if matcher is not None:
                entry["matcher"] = matcher
            lst.append(entry)
        if lst:
            hooks[ev] = lst
        else:
            hooks.pop(ev, None)
    if not hooks:
        cfg.pop("hooks", None)
    SETTINGS.write_text(json.dumps(cfg, indent=2))


def _systemctl(*args) -> int:
    return subprocess.run(["systemctl", "--user", *args]).returncode


def install(argv=None) -> int:
    cfgmod.ensure_config()
    exe = _exe()
    print(f"== claude-buddy install (exe: {exe}) ==")

    print("wiring hooks into", SETTINGS, "(backup .bak)")
    _merge_hooks(remove_only=False)
    print("  events:", ", ".join(HOOK_EVENTS))

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    tmpl = files("claude_buddy.data").joinpath("claude-buddy.service.in").read_text()
    (UNIT_DIR / UNIT_NAME).write_text(tmpl.replace("__EXEC__", exe))
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", UNIT_NAME)
    subprocess.run(["loginctl", "enable-linger"], capture_output=True)
    print("service enabled.")
    print(f"\nIf not paired yet:  claude-buddy pair")
    print(f"Status:  systemctl --user status claude-buddy")
    print(f"Logs:    journalctl --user -u claude-buddy -f")
    return 0


def uninstall(argv=None) -> int:
    print("== claude-buddy uninstall ==")
    _systemctl("disable", "--now", UNIT_NAME)
    try:
        (UNIT_DIR / UNIT_NAME).unlink()
    except FileNotFoundError:
        pass
    _systemctl("daemon-reload")
    print("removing hooks from", SETTINGS, "(backup .bak)")
    _merge_hooks(remove_only=True)
    print("done. (config + bond left intact)")
    return 0


def status(argv=None) -> int:
    cfg = cfgmod.load_config()
    print("config :", cfgmod.config_path())
    print("device :", cfg.get("device_mac") or "(unset)")
    print("socket :", cfgmod.socket_path())
    print("gated  :", ", ".join(cfg.get("sensitive_tools", [])) or "(none)")
    return _systemctl("status", "--no-pager", UNIT_NAME)
