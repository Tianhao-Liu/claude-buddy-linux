"""`claude-buddy name <name>` and `claude-buddy unpair`.

These forward a command to the device through the running daemon (which owns
the BLE link) over the unix socket, and wait for the device's ack.
"""
import json
import socket
import subprocess
import sys

from . import config as cfgmod


def _send_cmd(obj: dict, timeout: float = 12.0) -> dict | None:
    """Ask the daemon to send `obj` to the device; return the device's ack."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(cfgmod.socket_path())
        s.sendall((json.dumps({"kind": "cmd", "obj": obj}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        if buf:
            return json.loads(buf.split(b"\n", 1)[0])
    except Exception as e:
        print(f"  (daemon not reachable: {e})")
        return None
    return None


def name(argv=None) -> int:
    argv = argv or []
    if not argv:
        print("usage: claude-buddy name <display-name>")
        return 2
    new = " ".join(argv)[:18]   # device petName buffer is small
    ack = _send_cmd({"cmd": "name", "name": new})
    if ack and ack.get("ok"):
        print(f"device display name set to: {new}")
        return 0
    print(f"failed: {ack.get('error') if ack else 'daemon down / device not connected'}")
    return 1


def unpair(argv=None) -> int:
    cfg = cfgmod.load_config()
    mac = cfg.get("device_mac", "")

    # 1) ask the device to erase its bond (best-effort; needs a live link)
    ack = _send_cmd({"cmd": "unpair"})
    if ack and ack.get("ok"):
        print("device erased its bond.")
    else:
        print("note: could not tell the device to unpair (daemon down / disconnected); "
              "continuing with host-side cleanup.")

    # 2) drop the host-side BlueZ bond
    if mac:
        subprocess.run(["bluetoothctl", "remove", mac], capture_output=True)
        print(f"removed host bond for {mac}.")

    # 3) forget the MAC so the daemon stops trying to reconnect
    cfg["device_mac"] = ""
    cfgmod.save_config(cfg)
    print("cleared device_mac from config. Run `claude-buddy pair` to bond again.")
    return 0
