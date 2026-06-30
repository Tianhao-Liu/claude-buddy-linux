"""claude-buddy CLI entry point.

Subcommands:
  daemon      run the BLE bridge daemon (used by the systemd service)
  hook        Claude Code hook dispatcher (used by settings.json hooks)
  pair        one-time BLE pairing
  name        set the device display name (claude-buddy name <name>)
  unpair      tell the device to erase its bond + drop the host bond
  install     install systemd --user service + Claude Code hooks
  uninstall   remove the service + hooks
  status      show config + service status
"""
import sys


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd == "daemon":
        from . import daemon
        daemon.run()
        return 0
    if cmd == "hook":
        from . import hook
        return hook.main(rest)
    if cmd == "pair":
        from . import pair
        return pair.main(rest)
    if cmd in ("name", "unpair"):
        from . import cmds
        return getattr(cmds, cmd)(rest)
    if cmd in ("install", "uninstall", "status"):
        from . import setup_cmd
        return getattr(setup_cmd, cmd)(rest)
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
