#!/usr/bin/env python3
"""Wake a Codex session for unread messages in the /opt/agentbridge public hub.

The watcher does not consume messages. The notified session must use
agent_hub.py read AGENT to acknowledge them. Runtime state is private to this
watcher, while inbox and reader locks follow the installed hub's protocol.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

if __package__:
    from . import agent_hub as hub
else:
    import agent_hub as hub
if __package__:
    from . import agentbridge_common as common
else:
    import agentbridge_common as common
if __package__:
    from .notification_preview import batch_preview
else:
    from notification_preview import batch_preview


def display_records(records, state):
    """Private presentation aliases never change message IDs or routing."""
    labels_file = state / "sender-labels.json"
    try:
        info = labels_file.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 65536):
            return records
        labels = json.loads(labels_file.read_text())
        if not isinstance(labels, dict):
            return records
    except (OSError, ValueError):
        return records
    return [dict(record, source=labels[record.get("source")])
            if isinstance(labels.get(record.get("source")), str)
            and labels[record.get("source")].strip() else record for record in records]


def notify(workspace, agent, thread, codex, state):
    directory = hub.agent_dir(workspace, agent)
    # A reader waiting for messages owns this lock and needs no wakeup.
    with hub.shared_file(directory / ".read.lock") as reader:
        try:
            fcntl.flock(reader, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with hub.locked(directory / "inbox.jsonl"):
            size = (directory / "inbox.jsonl").stat().st_size
            read = common.load_offset(directory / ".read_offset")
            if read > size:
                read = 0
            pending = common.load_offset(state / "pending_offset")
            if read < pending <= size:
                return  # Already queued; the session will read all arrivals.
            records, offset = common.read_complete(directory / "inbox.jsonl", read)
        if not records:
            return
        message = "[agentbridge] " + batch_preview(display_records(records, state), recipient="codex")
        subprocess.run(
            [codex, "queue", "--thread", thread, "--message", message],
            check=True, timeout=30,
        )
        common.save_offset(state / "pending_offset", offset)
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} queued through byte {offset}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("/opt/agentbridge-public"))
    parser.add_argument("--agent", required=True)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    hub.profile(args.workspace, args.agent)
    state = args.state_dir
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "watcher.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            running = False
        except BlockingIOError:
            running = True
        if args.status:
            print(f"watcher {'running' if running else 'stopped'}")
            if (state / "watcher.json").exists():
                print((state / "watcher.json").read_text())
            return
        if running:
            parser.error("a watcher is already running in this state directory")
        identity = {"agent": args.agent, "thread": args.thread,
                    "workspace": str(args.workspace.resolve())}
        identity_path = state / "identity.json"
        if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
            parser.error("state directory belongs to another inbox or thread")
        identity_path.write_text(json.dumps(identity))
        (state / "watcher.json").write_text(json.dumps({
            **identity, "pid": os.getpid(),
            "started_at": time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }, indent=2) + "\n")
        print(f"watching {args.agent} -> {args.thread}; pid={os.getpid()}", flush=True)
        while True:
            try:
                notify(args.workspace, args.agent, args.thread, args.codex, state)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {error}", flush=True)
                if args.once:
                    raise
                time.sleep(10)
            if args.once:
                return
            time.sleep(1)


if __name__ == "__main__":
    main()
