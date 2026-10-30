#!/usr/bin/env python3
"""LAN hub server and client for Linux and Windows, using Python 3 stdlib.

Server: agent_lan.py serve --host 0.0.0.0 --workspace /opt/agentbridge-public
Client: agent_lan.py --url http://192.168.1.10:8765 list
Set AGENTBRIDGE_TOKEN (or --token-file) on both ends.
The client is standalone; the server also needs agent_hub.py beside this file.
"""
import argparse
import getpass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_BODY = 1024 * 1024
MAX_WAIT = 30


def integer(data, key, default=None):
    value = data.get(key, default)
    if type(value) is not int or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def string(data, key, default=None):
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def dispatch(workspace, action, data):
    # Defer the file backend import so Windows clients need just this script.
    if __package__:
        from . import agent_hub as hub
    else:
        import agent_hub as hub
    if action == "register":
        if type(data.get("update", False)) is not bool:
            raise ValueError("update must be a boolean")
        return hub.register(workspace, string(data, "agent"), string(data, "summary"),
                            string(data, "kind", "generic"), string(data, "project", ""),
                            data.get("update", False),
                            machine=string(data, "machine", ""),
                            client_user=string(data, "client_user", ""))
    if action == "list":
        query = string(data, "query", "").casefold()
        records = []
        for directory in sorted((workspace / "agents").iterdir()):
            if directory.is_dir() and (directory / "profile.json").exists():
                record = hub.profile(workspace, directory.name)
                if query in json.dumps(record, ensure_ascii=False).casefold():
                    records.append(record)
        return records
    if action == "show":
        return hub.profile(workspace, string(data, "agent"))
    if action == "send":
        reply = data.get("reply_to")
        kind = data.get("kind")
        if reply is not None and not isinstance(reply, str):
            raise ValueError("reply_to must be a string or null")
        if kind is not None and not isinstance(kind, str):
            raise ValueError("kind must be a string or null")
        return hub.send(workspace, string(data, "sender"), string(data, "to"),
                        string(data, "text"), reply, kind)
    if action not in ("read", "ack", "tail"):
        raise ValueError("unknown action")
    agent = string(data, "agent")
    hub.profile(workspace, agent)
    directory = hub.agent_dir(workspace, agent)
    marker = directory / ".read_offset"
    if action == "tail":
        count = integer(data, "n", 10)
        if not 1 <= count <= 1000:
            raise ValueError("n must be between 1 and 1000")
        with hub.locked(directory / "inbox.jsonl"):
            return hub.read_complete(directory / "inbox.jsonl", 0)[0][-count:]
    if action == "ack":
        start = integer(data, "start")
        end = integer(data, "end")
        with hub.locked(directory / ".read.lock"):
            current = hub.load_offset(marker)
            if current != start:
                # Repeating a successful acknowledgement is harmless.
                if current == end:
                    return {"offset": current}
                raise ValueError("read cursor changed; use one reader per agent")
            with hub.locked(directory / "inbox.jsonl") as stream:
                size = os.fstat(stream.fileno()).st_size
                if not start <= end <= size:
                    raise ValueError("invalid acknowledgement range")
                if end:
                    with open(directory / "inbox.jsonl", "rb") as raw:
                        raw.seek(end - 1)
                        if raw.read(1) != b"\n":
                            raise ValueError("acknowledgement must end at a complete record")
                hub.atomic_write(marker, str(end))
        return {"offset": end}
    wait = data.get("wait", 0)
    if type(wait) not in (int, float) or not math.isfinite(wait) or not 0 <= wait <= MAX_WAIT:
        raise ValueError(f"wait must be between 0 and {MAX_WAIT}")
    deadline = time.monotonic() + wait
    while True:
        with hub.locked(directory / ".read.lock"):
            start = hub.load_offset(marker)
            with hub.locked(directory / "inbox.jsonl"):
                records, end = hub.read_complete(directory / "inbox.jsonl", start)
        if records or time.monotonic() >= deadline:
            # The client acknowledges only after it receives and prints the batch.
            return {"records": records, "start": start, "end": end}
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, workspace, token):
        self.workspace = workspace
        self.token = token
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "agentbridge/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(40)

    def log_message(self, format, *args):
        # Never log request contents, credentials, or user-provided URLs.
        pass

    def respond(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            supplied = self.headers.get("Authorization", "").encode("utf-8")
            expected = ("Bearer " + self.server.token).encode("utf-8")
            if not hmac.compare_digest(supplied, expected):
                self.respond(401, {"error": "invalid hub token"})
                return
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Transfer-Encoding is not supported")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                self.respond(413, {"error": "request must be 1 byte to 1 MiB"})
                return
            routes = {"/v1/" + action: action for action in
                      ("register", "list", "show", "send", "read", "ack", "tail")}
            if self.path not in routes:
                self.respond(404, {"error": "unknown endpoint"})
                return
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("request must be a JSON object")
            result = dispatch(self.server.workspace, routes[self.path], data)
            self.respond(200, result)
        except FileNotFoundError:
            self.respond(404, {"error": "agent or workspace file not found"})
        except (ValueError, TypeError, UnicodeError) as error:
            self.respond(400, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except OSError:
            self.respond(500, {"error": "hub filesystem operation failed; check server permissions"})


def request(url, token, action, data):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL must be an http(s) hub address without credentials, query or fragment")
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = Request(url.rstrip("/") + "/v1/" + action, data=body,
                  headers={"Authorization": "Bearer " + token,
                           "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=45) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.load(error).get("error", str(error))
        except (ValueError, AttributeError):
            message = str(error)
        raise ValueError(f"HTTP {error.code}: {message}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("AGENTBRIDGE_URL"))
    parser.add_argument("--token-file", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--workspace", type=Path, default=Path(os.environ.get("AGENTBRIDGE_WORKSPACE", "./agentbridge-public")))
    register = sub.add_parser("register")
    register.add_argument("agent")
    register.add_argument("--summary", required=True)
    register.add_argument("--kind", default="generic")
    register.add_argument("--project", default="")
    register.add_argument("--update", action="store_true")
    ls = sub.add_parser("list")
    ls.add_argument("--query", default="")
    for name in ("show", "read", "tail"):
        command = sub.add_parser(name)
        command.add_argument("agent")
        if name == "read":
            command.add_argument("--wait", type=float, default=0)
        if name == "tail":
            command.add_argument("-n", type=int, default=10)
    send = sub.add_parser("send")
    send.add_argument("to")
    send.add_argument("text")
    send.add_argument("--from", dest="sender", default=os.environ.get("AGENTBRIDGE_AGENT"))
    send.add_argument("--reply-to")
    send.add_argument("--kind")
    args = parser.parse_args()
    try:
        token = args.token_file.read_text(encoding="utf-8-sig").strip() if args.token_file else os.environ.get("AGENTBRIDGE_TOKEN", "")
        if not token or not token.isascii() or any(char.isspace() for char in token):
            parser.error("set AGENTBRIDGE_TOKEN or --token-file to a non-empty ASCII token without whitespace")
        if args.command == "serve":
            if __package__:
                from . import agent_hub as hub
            else:
                import agent_hub as hub
            workspace = args.workspace.absolute()
            hub.initialize(workspace)
            with HubServer((args.host, args.port), workspace, token) as server:
                print(f"agentbridge listening on {args.host}:{server.server_port}; workspace={workspace}", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
            return
        if not args.url:
            parser.error("set --url or AGENTBRIDGE_URL")
        fields = {k: v for k, v in vars(args).items() if k not in ("command", "url", "token_file")}
        if args.command == "register":
            fields.update(machine=socket.gethostname(), client_user=getpass.getuser())
        if args.command == "send":
            if not args.sender:
                parser.error("send requires --from or AGENTBRIDGE_AGENT")
            if args.text == "-":
                fields["text"] = sys.stdin.read()
        result = request(args.url, token, args.command, fields)
        if args.command == "read":
            for record in result["records"]:
                print(json.dumps(record, ensure_ascii=False))
            sys.stdout.flush()
            if result["end"] != result["start"]:
                request(args.url, token, "ack", dict(agent=args.agent, start=result["start"], end=result["end"]))
        elif args.command in ("list", "tail"):
            for record in result:
                print(json.dumps(record, ensure_ascii=False))
        elif args.command == "send":
            print(result["id"])
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, URLError) as error:
        parser.exit(1, f"agentbridge: {error}\n")


if __name__ == "__main__":
    main()
