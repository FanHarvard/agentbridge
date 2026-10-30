#!/usr/bin/env python3
"""Discover agents and exchange messages through a shared local directory.

Examples: agent_hub.py --workspace /opt/agentbridge-public list
          agent_hub.py register alice-codex --summary 'Python development'
          agent_hub.py send bob-claude 'Please review' --from alice-codex
          agent_hub.py read bob-claude --wait 30
Only the Python standard library is required (Linux or Windows, local filesystem).
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:  # Windows
    import msvcrt
import getpass
import json
import math
import os
from pathlib import Path
import re
import stat
import socket
import sys
import tempfile
import time
import uuid


def read_complete(path, offset):
    """Read complete JSONL records and return their byte offset."""
    with open(path, "rb") as stream:
        if stream.seek(0, os.SEEK_END) < offset:
            offset = 0
        stream.seek(offset)
        data = stream.read()
    complete = data[:data.rfind(b"\n") + 1]
    return [json.loads(line) for line in complete.splitlines() if line.strip()], offset + len(complete)


def load_offset(path):
    try:
        return int(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0


def uid():
    return os.getuid() if hasattr(os, "getuid") else getpass.getuser()


def file_mode(fd, path, mode):
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)
    else:
        os.chmod(path, mode)


def agent_dir(workspace, agent):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", agent):
        raise ValueError("agent id: 1–80 letters, digits, underscores or hyphens")
    directory = workspace / "agents" / agent
    if directory.is_symlink():
        raise ValueError("agent directory must not be a symbolic link")
    return directory


def shared_dir(path, mode):
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"not a regular directory: {path}")
    else:
        path.chmod(mode)  # independent of the registering user's umask


def shared_file(path):
    """Open shared files without following symlinks or changing existing modes."""
    if path.is_symlink():
        raise OSError(f"symbolic link is not allowed: {path}")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError:
        fd = os.open(path, flags)
    else:
        file_mode(fd, path, 0o666)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"not a regular file: {path}")
    return os.fdopen(fd, "r+", encoding="utf-8", newline="")


@contextmanager
def locked(path):
    with shared_file(path) as stream:
        if os.name != "nt":
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield stream
        else:
            # Windows byte locks use a dedicated file, never bytes in the inbox.
            with shared_file(path.with_name(path.name + ".winlock")) as lock:
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(" ")
                    lock.flush()
                lock.seek(0)
                while True:
                    try:
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if error.errno not in (13, 36):
                            raise
                        time.sleep(0.05)
                try:
                    yield stream
                finally:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def atomic_write(path, text):
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            file_mode(stream.fileno(), temporary, 0o666)
        # Windows may briefly deny replacement while another request reads it.
        for attempt in range(21):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 20:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize(workspace):
    # Parent must already exist; this avoids silently making public ancestor dirs.
    shared_dir(workspace, 0o1777)
    shared_dir(workspace / "agents", 0o1777)


def profile(workspace, agent):
    directory = agent_dir(workspace, agent)
    with shared_file_existing(directory / "profile.json") as stream:
        record = json.load(stream)
    if record.get("id") != agent or record.get("inbox") != f"agents/{agent}/inbox.jsonl":
        raise ValueError(f"invalid profile for {agent}")
    return record


def shared_file_existing(path):
    if path.is_symlink():
        raise OSError(f"symbolic link is not allowed: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"not a regular file: {path}")
    return os.fdopen(fd, encoding="utf-8")


def register(workspace, agent, summary, kind="generic", project="", update=False,
             machine=None, client_user=None):
    if not summary.strip():
        raise ValueError("summary must not be empty")
    initialize(workspace)
    directory = agent_dir(workspace, agent)
    shared_dir(directory, 0o777)
    with locked(directory / ".profile.lock"):
        path = directory / "profile.json"
        if path.exists():
            previous = profile(workspace, agent)
            if not update:
                raise ValueError("agent already exists; choose a unique id or use --update")
            if previous.get("uid") != uid():
                raise ValueError("only the registering OS user may update this profile")
        with shared_file(directory / "inbox.jsonl"):
            pass
        record = dict(id=agent, summary=summary, kind=kind, project=project,
                      user=getpass.getuser(), uid=uid(),
                      machine=machine or socket.gethostname(),
                      client_user=client_user or getpass.getuser(),
                      inbox=f"agents/{agent}/inbox.jsonl",
                      updated_at=datetime.now(timezone.utc).isoformat())
        atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return record


def send(workspace, sender, recipient, text, reply_to=None, kind=None):
    profile(workspace, sender)
    profile(workspace, recipient)
    record = dict(id=uuid.uuid4().hex, t=datetime.now(timezone.utc).isoformat(),
                  source=sender, to=recipient, reply_to=reply_to,
                  kind=kind or ("reply" if reply_to else "message"), text=text)
    with locked(agent_dir(workspace, recipient) / "inbox.jsonl") as stream:
        stream.seek(0, os.SEEK_END)
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
    return record


def receive(workspace, agent, wait=0):
    profile(workspace, agent)
    directory = agent_dir(workspace, agent)
    deadline = time.monotonic() + wait
    with locked(directory / ".read.lock"):
        marker = directory / ".read_offset"
        while True:
            # Coordinate with append so offsets always refer to complete records.
            with locked(directory / "inbox.jsonl"):
                records, offset = read_complete(directory / "inbox.jsonl", load_offset(marker))
            if records or time.monotonic() >= deadline:
                atomic_write(marker, str(offset))
                return records
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", default=os.environ.get("AGENTBRIDGE_WORKSPACE", "/opt/agentbridge-public"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    reg = sub.add_parser("register")
    reg.add_argument("agent")
    reg.add_argument("--summary", required=True)
    reg.add_argument("--kind", default="generic")
    reg.add_argument("--project", default="")
    reg.add_argument("--update", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--query", default="", help="case-insensitive search across profile fields")
    show = sub.add_parser("show")
    show.add_argument("agent")
    msg = sub.add_parser("send")
    msg.add_argument("to")
    msg.add_argument("text", help="use - to read from stdin")
    msg.add_argument("--from", dest="sender", default=os.environ.get("AGENTBRIDGE_AGENT"))
    msg.add_argument("--reply-to")
    msg.add_argument("--kind")
    read = sub.add_parser("read")
    read.add_argument("agent")
    read.add_argument("--wait", type=float, default=0)
    tail = sub.add_parser("tail")
    tail.add_argument("agent")
    tail.add_argument("-n", type=int, default=10)
    args = parser.parse_args()
    workspace = Path(args.workspace).absolute()
    try:
        if args.command == "init":
            initialize(workspace)
            print(workspace)
        elif args.command == "register":
            print(json.dumps(register(workspace, args.agent, args.summary, args.kind,
                                      args.project, args.update), ensure_ascii=False))
        elif args.command == "list":
            if not (workspace / "agents").is_dir():
                raise ValueError("workspace is not initialized; run init first")
            for path in sorted((workspace / "agents").iterdir()):
                if path.is_dir() and (path / "profile.json").exists():
                    record = profile(workspace, path.name)
                    if args.query.casefold() in json.dumps(record, ensure_ascii=False).casefold():
                        print(json.dumps(record, ensure_ascii=False))
        elif args.command == "show":
            print(json.dumps(profile(workspace, args.agent), ensure_ascii=False, indent=2))
        elif args.command == "send":
            if not args.sender:
                parser.error("send requires --from ID or AGENTBRIDGE_AGENT")
            text = sys.stdin.read() if args.text == "-" else args.text
            print(send(workspace, args.sender, args.to, text, args.reply_to, args.kind)["id"])
        else:
            if args.command == "read":
                if not math.isfinite(args.wait) or args.wait < 0:
                    parser.error("--wait must be finite and non-negative")
                records = receive(workspace, args.agent, args.wait)
            else:
                if args.n < 1:
                    parser.error("-n must be positive")
                profile(workspace, args.agent)
                with locked(agent_dir(workspace, args.agent) / "inbox.jsonl"):
                    records = read_complete(agent_dir(workspace, args.agent) / "inbox.jsonl", 0)[0][-args.n:]
            for record in records:
                print(json.dumps(record, ensure_ascii=False))
    except (OSError, ValueError) as error:
        parser.exit(1, f"agentbridge: {error}\n")


if __name__ == "__main__":
    main()
