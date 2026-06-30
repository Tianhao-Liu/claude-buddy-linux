"""One-time BLE pairing for the Claude buddy.

The firmware requires LE Secure Connections + MITM passkey bonding and is
DisplayOnly: it shows a 6-digit passkey on the AMOLED *and* prints it to the
USB serial port (`[ble] passkey NNNNNN`). Default mode reads the passkey off
serial and answers bluetoothctl automatically; `--manual` just opens
bluetoothctl for you to type it.

After bonding we deliberately leave the device UNtrusted: a trusted+bonded
device auto-connects at the BlueZ layer and blocks the daemon from grabbing it.
"""
import argparse
import re
import subprocess
import sys
import threading
import time

from . import config as cfgmod


def _discover_mac(prefix: str, timeout: int = 8) -> str:
    subprocess.run(["bluetoothctl", "--timeout", str(timeout), "scan", "on"],
                   capture_output=True, text=True)
    out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and parts[0] == "Device" and parts[2].startswith(prefix):
            return parts[1]
    return ""


def _auto_pair(mac: str, port: str) -> bool:
    try:
        import serial  # pyserial; optional
    except ImportError:
        print("pyserial not installed; falling back to --manual.\n"
              "  install with:  pip install pyserial   (or run with --manual)")
        return _manual_pair(mac)

    passkey = {"val": None}
    btlines: list[str] = []

    def read_serial():
        try:
            s = serial.Serial(port, 115200, timeout=0.5)
        except Exception as e:
            print("serial open failed:", e)
            return
        s.reset_input_buffer()
        end = time.time() + 40
        while time.time() < end and passkey["val"] is None:
            line = s.readline().decode("utf-8", "replace").strip()
            if line:
                m = re.search(r"passkey\s+0*(\d{1,6})", line, re.I)
                if m:
                    passkey["val"] = m.group(1).zfill(6)
                    print("serial passkey:", passkey["val"])
        s.close()

    threading.Thread(target=read_serial, daemon=True).start()
    proc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)

    def read_bt():
        for line in proc.stdout:
            btlines.append(line.rstrip("\n"))

    threading.Thread(target=read_bt, daemon=True).start()

    def send(cmd, wait=0.6):
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
        time.sleep(wait)

    send("power on")
    send("agent KeyboardDisplay")
    send("default-agent")
    send("scan on", wait=4)
    send(f"pair {mac}", wait=0.5)

    deadline = time.time() + 30
    while time.time() < deadline and passkey["val"] is None:
        time.sleep(0.3)
    if passkey["val"]:
        time.sleep(0.5)
        send(str(passkey["val"]), wait=0.5)
        send("yes", wait=0.5)
    else:
        print("!! no passkey captured from serial; try --manual")

    deadline = time.time() + 25
    while time.time() < deadline:
        blob = "\n".join(btlines).lower()
        if "pairing successful" in blob or "paired: yes" in blob:
            break
        time.sleep(0.4)

    send(f"untrust {mac}")
    send("scan off")
    send("quit", wait=0.3)
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    return _is_paired(mac)


def _manual_pair(mac: str) -> bool:
    print(f"""
Opening bluetoothctl. Run these lines (passkey = the 6 digits on the screen):

    pair {mac}
    (type the 6-digit passkey shown on the AMOLED)
    quit

Do NOT 'trust' the device — the daemon owns the connection itself.
""")
    subprocess.call(["bluetoothctl", "--agent", "KeyboardDisplay"])
    subprocess.run(["bluetoothctl", "untrust", mac], capture_output=True)
    return _is_paired(mac)


def _is_paired(mac: str) -> bool:
    info = subprocess.run(["bluetoothctl", "info", mac],
                          capture_output=True, text=True).stdout
    return "Paired: yes" in info


def main(argv=None) -> int:
    cfg = cfgmod.ensure_config()
    ap = argparse.ArgumentParser(prog="claude-buddy pair")
    ap.add_argument("mac", nargs="?", help="device MAC (else auto-discover)")
    ap.add_argument("--port", default=cfg.get("serial_port", "/dev/ttyACM0"),
                    help="serial port for the auto passkey read")
    ap.add_argument("--manual", action="store_true",
                    help="type the passkey yourself instead of reading serial")
    args = ap.parse_args(argv)

    if subprocess.run(["which", "bluetoothctl"], capture_output=True).returncode != 0:
        print("bluetoothctl not found (install bluez)")
        return 1

    subprocess.run(["bluetoothctl", "power", "on"], capture_output=True)
    mac = args.mac or cfg.get("device_mac") or _discover_mac(
        cfg.get("device_name_prefix", "Claude"))
    if not mac:
        print("No device found. Re-run with an explicit MAC: claude-buddy pair AA:BB:..")
        return 1
    print(f"device: {mac}")

    subprocess.run(["bluetoothctl", "remove", mac], capture_output=True)  # clean slate
    ok = _manual_pair(mac) if args.manual else _auto_pair(mac, args.port)

    if ok:
        cfg["device_mac"] = mac
        cfgmod.save_config(cfg)
        print(f"PAIRED. saved device_mac={mac} to {cfgmod.config_path()}")
        return 0
    print("pairing did not complete (device shows 'Paired: no'). Try --manual.")
    return 1
