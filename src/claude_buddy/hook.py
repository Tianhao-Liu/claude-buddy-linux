"""Claude Code hook dispatcher.

Invoked as `claude-buddy hook` for several hook events (see settings.json).
Reads the hook payload as JSON on stdin, talks to the daemon over its unix
socket, and — for sensitive PreToolUse calls — blocks until the user
approves/denies on the device. Strictly fail-open: any error, missing daemon,
or timeout lets Claude Code proceed with its normal permission flow.
"""
import json
import socket
import sys

from . import config as cfgmod


def _send_recv(req: dict, timeout: float) -> dict | None:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(cfgmod.socket_path())
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        if buf:
            return json.loads(buf.split(b"\n", 1)[0])
    except Exception:
        return None
    return None


def _hint_for(tool: str, ti: dict) -> str:
    if not isinstance(ti, dict):
        ti = {}
    if tool == "Bash":
        h = ti.get("command", "")
    elif tool in ("Write", "Edit", "MultiEdit"):
        h = ti.get("file_path", "")
    elif tool == "NotebookEdit":
        h = ti.get("notebook_path", "")
    else:
        h = json.dumps(ti, ensure_ascii=False)
    return " ".join(str(h).split())[:60]


def _emit(decision: str, reason: str):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))


def main(argv=None) -> int:
    cfg = cfgmod.load_config()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    event = data.get("hook_event_name", "")
    sid = data.get("session_id", "")
    transcript = data.get("transcript_path", "")
    cwd = data.get("cwd", "")

    if event == "PreToolUse":
        tool = data.get("tool_name", "")
        if tool not in cfg.get("sensitive_tools", []):
            return 0
        req = {"kind": "prompt", "session_id": sid, "tool": tool,
               "hint": _hint_for(tool, data.get("tool_input", {}))}
        reply = _send_recv(req, timeout=cfg["approval_timeout_s"] + 10)
        if not reply:
            return 0
        dec = reply.get("decision")
        if dec == "allow":
            _emit("allow", "approved on buddy device")
        elif dec == "deny":
            _emit("deny", "denied on buddy device")
        return 0

    ev_map = {
        "UserPromptSubmit": "user_prompt",
        "Stop": "stop",
        "SessionStart": "session_start",
        "SessionEnd": "session_end",
        "Notification": "notification",
        "PostToolUse": "post_tool",
    }
    ev = ev_map.get(event)
    if ev:
        req = {"kind": "event", "event": ev, "session_id": sid,
               "transcript": transcript, "cwd": cwd}
        if event == "UserPromptSubmit":
            req["text"] = data.get("prompt", "")
        elif event == "Notification":
            req["text"] = data.get("message", "")
        _send_recv(req, timeout=cfg["hook_socket_timeout_s"])
    return 0
