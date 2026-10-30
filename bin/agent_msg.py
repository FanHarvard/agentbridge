#!/usr/bin/env python3
"""Two-way messages between a Claude Code session and a Codex session (file based).

Usage (either side; channel from --channel NAME, --box-dir DIR or $AGENTBRIDGE_CHANNEL):
  agent_msg.py --channel NAME send claude|codex "text" [--reply-to ID] [--kind KIND]
  agent_msg.py --channel NAME read claude|codex [--wait SECONDS]   # only new messages
  agent_msg.py --channel NAME get ID                                # a message and its replies
  agent_msg.py --channel NAME tail [N]                              # last N messages, both ways
Kinds in use: message, reply, question, user_answer, permission_notice, report (free to add).
`send` prints the new id. Text "-" reads the text from stdin.
"""
import argparse
import json
import sys

if __package__:
    from . import agentbridge_common as ab
else:
    import agentbridge_common as ab


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel")
    p.add_argument("--box-dir")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("to", choices=ab.SIDES)
    s.add_argument("text")
    s.add_argument("--reply-to")
    s.add_argument("--kind")
    r = sub.add_parser("read")
    r.add_argument("reader", choices=ab.SIDES)
    r.add_argument("--wait", type=float, default=0.0)
    g = sub.add_parser("get")
    g.add_argument("id")
    t = sub.add_parser("tail")
    t.add_argument("n", type=int, nargs="?", default=10)
    a = p.parse_args()
    d = ab.channel_dir(a.channel, a.box_dir)

    if a.cmd == "send":
        text = sys.stdin.read() if a.text == "-" else a.text
        print(ab.append(d, a.to, text, a.kind, a.reply_to)["id"])
    elif a.cmd == "read":
        recs = ab.receive(d, a.reader, a.wait)
        for m in recs:
            print(json.dumps(m, ensure_ascii=False))
        if not recs:
            print("no new messages")
    else:
        recs = []
        for side in ab.SIDES:
            recs += ab.read_complete(ab.box_file(d, side), 0)[0]
        recs.sort(key=lambda m: m.get("t", ""))
        if a.cmd == "get":
            recs = [m for m in recs if a.id in (m.get("id"), m.get("reply_to"))]
        else:
            recs = recs[-a.n:]
        for m in recs:
            print(json.dumps(m, ensure_ascii=False))


if __name__ == "__main__":
    main()
