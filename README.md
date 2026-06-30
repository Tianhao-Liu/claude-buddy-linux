# claude-buddy

Linux bridge for the **ESP32 Claude desktop buddy**. Re-implements the
macOS/Windows *Hardware Buddy* bridge: connects to the device over BLE, pushes
live Claude Code session state, and relays tool-permission decisions made on the
device back to Claude Code.

```
Claude Code sessions ──hooks──▶ unix socket ──▶ daemon ──BLE──▶ ESP32 buddy
                                          ◀── approve / deny (button press)
```

## Install

```bash
pip install claude-buddy            # or: pip install "claude-buddy[autopair]"
claude-buddy install                # systemd --user service + Claude Code hooks
claude-buddy pair                   # one-time bond (auto-reads passkey via serial)
```

`pip install .` from a checkout works too. The `[autopair]` extra adds
`pyserial`, letting `claude-buddy pair` read the 6-digit passkey straight off the
device's USB serial (`/dev/ttyACM0`) instead of you typing it. Without it, use
`claude-buddy pair --manual`.

## Commands

| Command | Purpose |
| --- | --- |
| `claude-buddy daemon` | run the BLE bridge (the systemd service runs this) |
| `claude-buddy hook` | Claude Code hook dispatcher (wired by `install`) |
| `claude-buddy pair [MAC] [--manual] [--port /dev/ttyACM0]` | one-time BLE bond |
| `claude-buddy install` / `uninstall` | service + hooks in `~/.claude/settings.json` |
| `claude-buddy status` | config + service status |

## Configuration

`~/.config/claude-buddy/config.json` (auto-created). Key fields:

- `owner` — name shown on the device.
- `sensitive_tools` — tools that require on-device approval (default
  `Bash`/`Write`/`Edit`/`MultiEdit`/`NotebookEdit`). Set `[]` to disable gating;
  the hook re-reads this file every call, so no restart needed.
- `device_mac`, `device_name_prefix`, timeouts, `serial_port`.

## How it works

- A `systemd --user` daemon owns the BLE link + aggregate session state and sends
  REFERENCE.md heartbeat snapshots (`total/running/waiting/msg/entries/tokens`).
- Thin hooks feed it session events over `$XDG_RUNTIME_DIR/claude-buddy.sock`.
  Sensitive `PreToolUse` calls block until the device answers; everything else
  passes through. **Fail-open:** daemon down or device gone → normal terminal
  permission prompt, never a hang.
- Tokens / recent entries come from tailing `~/.claude/projects/.../*.jsonl`.

On the device: **Key1 = approve**, **Key3 short = deny**.

## Pairing notes

The firmware requires LE Secure Connections + MITM passkey bonding (DisplayOnly).
After bonding, the device is left **untrusted** on purpose — a trusted+bonded
device auto-connects at the BlueZ layer and blocks the daemon. The bond persists
regardless, so reconnects need no passkey.

## Uninstall

```bash
claude-buddy uninstall      # stops service, removes hooks (config + bond kept)
```

Scope (v1): pairing, heartbeat, time sync, owner, approve/deny relay. Deferred
(firmware already supports): battery status panel, `turn` events, `name`/`unpair`,
folder/GIF push. Protocol: the device repo's `REFERENCE.md`.
