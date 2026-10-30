#!/usr/bin/env python3
"""Codex side: queue a turn in the Codex thread only when Claude wrote something new.

Waits without calling any model (shell polling of <channel>/to_codex.jsonl). When
complete new lines appear it runs
  codex queue --thread THREAD --message "<sender → Codex｜short preview>"
so the existing Codex chat wakes up and reads them itself. One watcher per channel
(lock file); the notified offset is kept in <channel>/.codex_notify_offset.

Usage:
  codex_watch.py --channel NAME [--thread ID] [--codex PATH] [--once]
  codex_watch.py --channel NAME --status
The thread defaults to codex_thread in <channel>/channel.json.
Run it in the background, e.g.
  setsid nohup python3 codex_watch.py --channel NAME >> boxes/NAME/codex_watch.log 2>&1 &
"""
import argparse
import fcntl
import json
import shutil
import subprocess
import time

if __package__:
    from . import agentbridge_common as ab
else:
    import agentbridge_common as ab

if __package__:
    from .notification_preview import batch_preview, readable_text
else:
    from notification_preview import batch_preview, readable_text


def notification_message(d, records):
    """Display sender and short excerpts; full records remain in the inbox."""
    return batch_preview(records, recipient="codex", default_sender="claude")


def notify(d, thread, codex):
    with ab.delivery_lock(d, "codex", blocking=False) as acquired:
        if not acquired:  # read --wait owns delivery; it will consume and ack the reply
            return
        marker = d / ".codex_notify_offset"
        read = ab.load_offset(d / ".read_codex")
        pending = ab.load_offset(d / ".codex_pending_offset")
        size = ab.box_file(d, "codex").stat().st_size if ab.box_file(d, "codex").exists() else 0
        waiting = read < pending <= size
        if waiting:
            return  # one queued turn is waiting to read; coalesce later arrivals
        recs, off = ab.read_complete(ab.box_file(d, "codex"),
                                     read if waiting else max(read, ab.load_offset(marker)))
        if recs:
            msg = notification_message(d, recs)
            subprocess.run([codex, "queue", "--thread", thread, "--message", msg], check=True, timeout=30)
            ab.save_offset(d / ".codex_pending_offset", off)
        ab.save_offset(marker, off)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel")
    p.add_argument("--box-dir")
    p.add_argument("--thread")
    p.add_argument("--codex", default=shutil.which("codex") or "codex")
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    a = p.parse_args()
    d = ab.channel_dir(a.channel, a.box_dir)
    lock_path = d / ".codex_watch.lock"
    if a.status:
        running = False
        if lock_path.exists():
            with lock_path.open() as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    running = True
        print(f"watcher {'running' if running else 'stopped'}; notified up to "
              f"{ab.load_offset(d / '.codex_notify_offset')} bytes of to_codex.jsonl")
        return
    thread = a.thread or ab.settings(d).get("codex_thread")
    if not thread:
        p.error("no Codex thread: --thread ID or codex_thread in channel.json")
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("a watcher is already running for this channel")
        while True:
            try:
                notify(d, thread, a.codex)
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                print(time.strftime("%Y-%m-%d %H:%M:%S %Z"), e, flush=True)
                if a.once:
                    raise
                time.sleep(10)
            if a.once:
                return
            time.sleep(1)


if __name__ == "__main__":
    main()
