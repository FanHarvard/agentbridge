#!/usr/bin/env python3
"""Claude side: print each new message for Claude, sender and short preview for each message (for the Monitor tool).

Run it under Claude Code's Monitor tool (30 min maximum, re-arm on expiry):
  Monitor(command="python3 <agentbridge>/bin/claude_watch.py --channel NAME", timeout_ms=1800000)
The offset of what was printed is kept in <channel>/.claude_watch_offset, so messages
written while the watch was not armed are printed when it is re-armed (none lost,
none repeated).

Usage: claude_watch.py --channel NAME [--box-dir DIR] [--poll 1.0] [--once]
"""
import argparse
import json
import time

if __package__:
    from . import agentbridge_common as ab
else:
    import agentbridge_common as ab
if __package__:
    from .notification_preview import preview
else:
    from notification_preview import preview


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel")
    p.add_argument("--box-dir")
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--once", action="store_true", help="print what is new and exit")
    p.add_argument("--json", action="store_true", help="emit full JSON records instead of notification previews")
    a = p.parse_args()
    d = ab.channel_dir(a.channel, a.box_dir)
    marker = d / ".claude_watch_offset"
    path = ab.box_file(d, "claude")
    path.touch()
    while True:
        recs, off = ab.read_complete(path, ab.load_offset(marker))
        for m in recs:
            print(json.dumps(m, ensure_ascii=False) if a.json else
                  preview(m, recipient="claude", default_sender="codex"), flush=True)
        ab.save_offset(marker, off)
        if a.once:
            return
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
