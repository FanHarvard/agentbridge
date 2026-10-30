"""Shared helpers: channel directories, message records, append and read with offsets.

A channel is a directory boxes/<name>/ holding
  to_claude.jsonl   messages for the Claude Code session
  to_codex.jsonl    messages for the Codex session
  channel.json      settings (name, claude_project, codex_thread, ...)
  questions/        pending AskUserQuestion forwards (question bridge)
  .read_<reader>    byte offset read so far by agent_msg.py read
One JSON line per message: id, t (with timezone), source, to, reply_to, kind, text.
"""
import fcntl
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid

ROOT = Path(os.environ.get("AGENTBRIDGE_HOME", Path.home() / ".local/share/agentbridge"))
BOXES = ROOT / "boxes"
SIDES = ("claude", "codex")


def channel_dir(name=None, box_dir=None):
    """Channel directory from --box-dir, --channel, $AGENTBRIDGE_BOX or $AGENTBRIDGE_CHANNEL."""
    if box_dir:
        return Path(box_dir)
    if os.environ.get("AGENTBRIDGE_BOX"):
        return Path(os.environ["AGENTBRIDGE_BOX"])
    name = name or os.environ.get("AGENTBRIDGE_CHANNEL")
    if not name:
        raise SystemExit("no channel: give --channel NAME (or --box-dir, $AGENTBRIDGE_CHANNEL)")
    d = BOXES / name
    if not d.is_dir():
        raise SystemExit(f"channel {name} does not exist: bin/new_channel.sh {name}")
    return d


def box_file(d, to):
    if to not in SIDES:
        raise SystemExit(f"recipient must be one of {SIDES}")
    return Path(d) / f"to_{to}.jsonl"


def other(side):
    return "codex" if side == "claude" else "claude"


def append(d, to, text, kind=None, reply_to=None, source=None, message_id=None):
    rec = dict(id=message_id or uuid.uuid4().hex[:12], t=time.strftime("%Y-%m-%d %H:%M:%S %Z"),
               source=source or other(to), to=to, reply_to=reply_to,
               kind=kind or ("reply" if reply_to else "message"), text=text)
    with box_file(d, to).open("a+" if message_id else "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        if message_id:
            f.seek(0)
            for line in f:
                try:
                    existing = json.loads(line)
                except ValueError:
                    continue
                if existing.get("id") == message_id:
                    fields = ("source", "to", "reply_to", "kind", "text")
                    if any(existing.get(key) != rec[key] for key in fields):
                        raise ValueError("Message id already exists with different content")
                    return existing
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
    return rec


def read_complete(path, offset):
    """Complete lines after offset: (records, new offset). A partly written last line waits."""
    path = Path(path)
    if not path.exists():
        return [], 0
    with path.open("rb") as f:
        if f.seek(0, 2) < offset:   # file truncated or replaced
            offset = 0
        f.seek(offset)
        data = f.read()
    complete = data[:data.rfind(b"\n") + 1]
    recs = []
    for line in complete.splitlines():
        if line.strip():
            try:
                recs.append(json.loads(line))
            except ValueError:
                pass
    return recs, offset + len(complete)


def load_offset(marker):
    try:
        return int(Path(marker).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(marker, offset):
    tmp = Path(str(marker) + ".tmp")
    tmp.write_text(str(offset))
    tmp.replace(marker)


def settings(d):
    p = Path(d) / "channel.json"
    return json.loads(p.read_text()) if p.exists() else {}


@contextmanager
def delivery_lock(d, side, blocking=True):
    """Coordinate a waiting reader and its notifier; never lock the append path."""
    with (Path(d) / f".delivery_{side}.lock").open("a") as lock:
        acquired = False
        try:
            try:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(lock, flags)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            if acquired:
                fcntl.flock(lock, fcntl.LOCK_UN)


def receive(d, side, wait=0):
    """Read/ack one batch, reserving delivery while waiting for a reply."""
    with delivery_lock(d, side):
        marker = Path(d) / f".read_{side}"
        deadline = time.monotonic() + wait
        while True:
            records, offset = read_complete(box_file(d, side), load_offset(marker))
            if records or time.monotonic() >= deadline:
                save_offset(marker, offset)
                return records
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
