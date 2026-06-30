"""claude-buddy bridge daemon.

Owns the BLE link to the ESP32 buddy and an aggregate view of all local
Claude Code sessions (fed by hook scripts over a unix socket). Pushes
heartbeat snapshots to the device and relays tool-permission decisions made
on the device back to the blocking PreToolUse hook.

Protocol: device repo REFERENCE.md (Nordic UART Service, newline JSON).
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import date, datetime

from bleak import BleakClient, BleakScanner

from . import config as cfgmod

# ---- Nordic UART Service UUIDs (REFERENCE.md) ----
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # host -> device (write)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # device -> host (notify)


def tz_offset_seconds() -> int:
    off = datetime.now().astimezone().utcoffset()
    return int(off.total_seconds()) if off else 0


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.log = logging.getLogger("claude-buddy")

        self.sessions: dict[str, dict] = {}
        self.entries: deque[tuple[float, str]] = deque(maxlen=64)
        self.note_msg = ""

        self.tokens = 0
        self.tokens_today = 0
        self.today = date.today().isoformat()
        self.tail_pos: dict[str, int] = {}

        self.pending: dict[str, asyncio.Future] = {}
        self.current_prompt: dict | None = None
        self.prompt_lock = asyncio.Lock()
        self.ack_waiters: dict[str, asyncio.Future] = {}   # cmd name -> ack future

        self.client: BleakClient | None = None
        self.mtu = 20
        self._rxbuf = bytearray()
        self.connected = False
        self._write_lock = asyncio.Lock()   # serialize BLE writes (no interleave)

        self.dirty = asyncio.Event()
        self._last_snapshot = ""
        self._last_send = 0.0
        self._load_state()

    # ---------- persistence ----------
    def _load_state(self):
        try:
            s = json.loads(cfgmod.state_path().read_text())
            if s.get("today") == self.today:
                self.tokens_today = int(s.get("tokens_today", 0))
            if not self.cfg.get("device_mac") and s.get("device_mac"):
                self.cfg["device_mac"] = s["device_mac"]
        except (FileNotFoundError, ValueError):
            pass

    def _save_state(self):
        cfgmod.state_dir().mkdir(parents=True, exist_ok=True)
        cfgmod.state_path().write_text(json.dumps({
            "today": self.today,
            "tokens_today": self.tokens_today,
            "device_mac": self.cfg.get("device_mac", ""),
        }))

    def _roll_day(self):
        t = date.today().isoformat()
        if t != self.today:
            self.today = t
            self.tokens_today = 0
            self._save_state()

    # ---------- transcript tailing ----------
    def _add_entry(self, ts: float, text: str):
        text = " ".join(str(text).split())
        if not text:
            return
        cap = self.cfg["entry_text_max"]
        if len(text) > cap:
            text = text[: cap - 1] + "…"
        self.entries.append((ts, text))

    def _tail_transcript(self, path: str) -> bool:
        changed = False
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if path not in self.tail_pos:
            self.tail_pos[path] = size      # first sight: seek to end
            return False
        if size < self.tail_pos[path]:
            self.tail_pos[path] = 0
        if size == self.tail_pos[path]:
            return False
        try:
            with open(path, "r", errors="replace") as fh:
                fh.seek(self.tail_pos[path])
                data = fh.read()
                self.tail_pos[path] = fh.tell()
        except OSError:
            return False
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            typ = o.get("type")
            msg = o.get("message") if isinstance(o.get("message"), dict) else {}
            ts = time.time()
            tstr = o.get("timestamp")
            if tstr:
                try:
                    ts = datetime.fromisoformat(tstr.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
            if typ == "assistant":
                usage = msg.get("usage") or {}
                ot = int(usage.get("output_tokens", 0) or 0)
                if ot:
                    self._roll_day()
                    self.tokens += ot
                    self.tokens_today += ot
                    changed = True
                for block in (msg.get("content") or []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        self._add_entry(ts, block.get("text", ""))
                        changed = True
            elif typ == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    self._add_entry(ts, content)
                    changed = True
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            self._add_entry(ts, block.get("text", ""))
                            changed = True
        if changed:
            self._save_state()
        return changed

    def _tail_all(self) -> bool:
        changed = False
        for s in list(self.sessions.values()):
            p = s.get("transcript")
            if p and self._tail_transcript(p):
                changed = True
        return changed

    # ---------- snapshot ----------
    def build_snapshot(self) -> dict:
        total = len(self.sessions)
        running = sum(1 for s in self.sessions.values() if s.get("running"))
        waiting = 1 if self.current_prompt else 0
        ents = [f"{hhmm(ts)} {txt}" for ts, txt in reversed(self.entries)]
        ents = ents[: self.cfg["entries_max"]]

        if self.current_prompt:
            msg = f"approve: {self.current_prompt['tool']}"
        elif self.note_msg:
            msg = self.note_msg
        elif running > 0:
            msg = ents[0] if ents else "working…"
        elif total > 0:
            msg = "idle"
        else:
            msg = "no sessions"

        snap = {
            "total": total, "running": running, "waiting": waiting,
            "msg": msg, "entries": ents,
            "tokens": self.tokens, "tokens_today": self.tokens_today,
        }
        if self.current_prompt:
            snap["prompt"] = {
                "id": self.current_prompt["id"],
                "tool": self.current_prompt["tool"],
                "hint": self.current_prompt["hint"],
            }
        return snap

    def mark_dirty(self):
        self.dirty.set()

    # ---------- BLE send ----------
    async def send_obj(self, obj: dict):
        if not (self.client and self.connected):
            return
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        chunk = max(20, self.mtu - 3)
        try:
            async with self._write_lock:   # never interleave two writers' chunks
                for i in range(0, len(data), chunk):
                    await self.client.write_gatt_char(NUS_RX, data[i:i + chunk], response=True)
        except Exception as e:
            self.log.warning("send failed: %s", e)

    # ---------- inbound device lines ----------
    def _on_notify(self, _char, data: bytearray):
        self._rxbuf.extend(data)
        while b"\n" in self._rxbuf:
            line, _, rest = self._rxbuf.partition(b"\n")
            self._rxbuf = bytearray(rest)
            line = line.strip()
            if line:
                asyncio.create_task(self._handle_line(bytes(line)))

    async def _handle_line(self, raw: bytes):
        try:
            o = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        # ack to one of our outgoing commands (name/unpair/status/...)
        if "ack" in o:
            fut = self.ack_waiters.pop(o["ack"], None)
            if fut and not fut.done():
                fut.set_result(o)
            return
        cmd = o.get("cmd")
        if cmd == "permission":
            fut = self.pending.get(o.get("id"))
            if fut and not fut.done():
                fut.set_result("allow" if o.get("decision") == "once" else "deny")
            return
        if cmd:
            await self.send_obj({"ack": cmd, "ok": True, "n": 0})

    async def _send_cmd(self, obj: dict) -> dict:
        """Send a command to the device and wait for its matching ack."""
        if not self.connected:
            return {"ok": False, "error": "device not connected"}
        name = obj.get("cmd", "")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.ack_waiters[name] = fut
        try:
            await self.send_obj(obj)
            return await asyncio.wait_for(fut, timeout=8.0)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "no ack from device"}
        finally:
            self.ack_waiters.pop(name, None)

    # ---------- hook unix-socket server ----------
    async def _handle_hook(self, reader, writer):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            req = json.loads(raw.decode("utf-8", "replace"))
        except (asyncio.TimeoutError, ValueError):
            writer.close()
            return
        try:
            if req.get("kind") == "event":
                self._apply_event(req)
                self.mark_dirty()
                writer.write(b'{"ok":true}\n')
                await writer.drain()
            elif req.get("kind") == "prompt":
                dec = await self._do_prompt(req)
                writer.write((json.dumps({"decision": dec}) + "\n").encode())
                await writer.drain()
            elif req.get("kind") == "cmd":
                ack = await self._send_cmd(req.get("obj") or {})
                writer.write((json.dumps(ack) + "\n").encode())
                await writer.drain()
        except Exception as e:
            self.log.warning("hook handler error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _apply_event(self, req: dict):
        ev = req.get("event")
        sid = req.get("session_id") or "?"
        s = self.sessions.setdefault(sid, {"running": False, "transcript": "",
                                           "cwd": "", "branch": "", "last": 0.0})
        s["last"] = time.time()
        if req.get("transcript"):
            s["transcript"] = req["transcript"]
        if req.get("cwd"):
            s["cwd"] = req["cwd"]
        if ev == "session_end":
            self.sessions.pop(sid, None)
        elif ev == "user_prompt":
            s["running"] = True
            if req.get("text"):
                self._add_entry(time.time(), req["text"])
        elif ev == "stop":
            s["running"] = False
        elif ev == "notification":
            self.note_msg = req.get("text", "")[:60]

    async def _do_prompt(self, req: dict) -> str:
        if not self.connected:
            return "ask"
        pid = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        timeout = self.cfg["approval_timeout_s"]
        async with self.prompt_lock:
            if not self.connected:
                return "ask"
            self.pending[pid] = fut
            self.current_prompt = {"id": pid, "tool": req.get("tool", "?"),
                                   "hint": req.get("hint", "")}
            self.note_msg = ""
            self.mark_dirty()
            try:
                dec = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                dec = "ask"
            finally:
                self.pending.pop(pid, None)
                self.current_prompt = None
                self.mark_dirty()
            return dec

    # ---------- main loops ----------
    async def heartbeat_loop(self):
        keepalive = self.cfg["keepalive_s"]
        while True:
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            self.dirty.clear()
            self._tail_all()
            if not self.connected:
                continue
            snap = self.build_snapshot()
            key = json.dumps(snap, sort_keys=True, ensure_ascii=False)
            now = time.time()
            if key != self._last_snapshot or (now - self._last_send) >= keepalive:
                await self.send_obj(snap)
                self._last_snapshot = key
                self._last_send = now

    async def _on_connect(self):
        await self.send_obj({"time": [int(time.time()), tz_offset_seconds()]})
        if self.cfg.get("owner"):
            await self.send_obj({"cmd": "owner", "name": self.cfg["owner"]})
        self._last_snapshot = ""
        self.mark_dirty()

    async def ble_loop(self):
        backoff = 2
        while True:
            try:
                dev = await self._find()
                if not dev:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                self.log.info("connecting to %s", dev.address)
                async with BleakClient(dev) as client:
                    self.client = client
                    try:
                        await client._acquire_mtu()
                    except Exception:
                        pass
                    try:
                        self.mtu = client.mtu_size or 23
                    except Exception:
                        self.mtu = 23
                    await client.start_notify(NUS_TX, self._on_notify)
                    self.connected = True
                    self.cfg["device_mac"] = dev.address
                    self._save_state()
                    self.log.info("connected (mtu=%d)", self.mtu)
                    await self._on_connect()
                    backoff = 2
                    while client.is_connected:
                        await asyncio.sleep(1)
            except Exception as e:
                self.log.warning("ble error: %s", e)
            finally:
                self.connected = False
                self.client = None
                self._rxbuf = bytearray()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _find(self):
        mac = self.cfg.get("device_mac") or ""
        if mac:
            try:
                d = await BleakScanner.find_device_by_address(mac, timeout=8.0)
                if d:
                    return d
            except Exception as e:
                self.log.warning("find_by_address failed: %s", e)
            # Not advertising: most likely a stale BlueZ connection left over
            # from a previous (unclean) daemon exit keeps it connected. Drop it
            # so it advertises again, then retry once.
            try:
                subprocess.run(["bluetoothctl", "disconnect", mac],
                               capture_output=True, timeout=8)
                await asyncio.sleep(2)
                d = await BleakScanner.find_device_by_address(mac, timeout=8.0)
                if d:
                    self.log.info("recovered stale connection to %s", mac)
                    return d
            except Exception:
                pass
        prefix = self.cfg.get("device_name_prefix", "Claude")
        self.log.info("scanning for '%s*'", prefix)
        try:
            devices = await BleakScanner.discover(timeout=6.0)
        except Exception as e:
            self.log.warning("scan failed: %s", e)
            return None
        for d in devices:
            if (d.name or "").startswith(prefix):
                self.log.info("found %s (%s)", d.name, d.address)
                return d
        return None

    async def run(self):
        cfgmod.state_dir().mkdir(parents=True, exist_ok=True)
        sp = cfgmod.socket_path()
        try:
            os.unlink(sp)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(self._handle_hook, path=sp)
        os.chmod(sp, 0o600)
        self.log.info("hook socket: %s", sp)
        async with server:
            await asyncio.gather(self.ble_loop(), self.heartbeat_loop())


def run():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    try:
        asyncio.run(Bridge(cfgmod.load_config()).run())
    except KeyboardInterrupt:
        pass
