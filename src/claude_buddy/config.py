"""Config + filesystem paths for claude-buddy (XDG-based)."""
import json
import os
from pathlib import Path

APP = "claude-buddy"

DEFAULTS = {
    "owner": "",
    "device_name_prefix": "Claude",
    "device_mac": "",
    "sensitive_tools": ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"],
    "entries_max": 6,
    "entry_text_max": 40,
    "approval_timeout_s": 120,
    "keepalive_s": 10,
    "hook_socket_timeout_s": 0.3,
    "serial_port": "/dev/ttyACM0",
}


def _xdg(env: str, default: Path) -> Path:
    v = os.environ.get(env)
    return Path(v) if v else default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / APP


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP


def config_path() -> Path:
    return config_dir() / "config.json"


def state_path() -> Path:
    return state_dir() / "state.json"


def socket_path() -> str:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt):
        return os.path.join(rt, "claude-buddy.sock")
    return str(state_dir() / "buddyd.sock")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(config_path().read_text()))
    except (FileNotFoundError, ValueError):
        pass
    return cfg


def save_config(cfg: dict):
    config_dir().mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2))


def ensure_config() -> dict:
    """Create config.json with defaults if missing; return it."""
    if not config_path().exists():
        save_config(dict(DEFAULTS))
    return load_config()
