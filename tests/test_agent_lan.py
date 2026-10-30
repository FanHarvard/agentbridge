"""Exercise real loopback HTTP requests without invoking models or external hosts."""
from concurrent.futures import ThreadPoolExecutor
import json
from unittest.mock import patch
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BIN = Path(__import__("agentbridge").__file__).resolve().parent
sys.path.insert(0, str(BIN))
import agent_hub as hub
import agent_lan as lan


class LanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "public"
        hub.initialize(self.workspace)
        self.server = lan.HubServer(("127.0.0.1", 0), self.workspace, "test-secret")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        for name in ("linux-alice", "windows-bob"):
            self.call("register", agent=name, summary=f"{name} 開発", machine=name,
                      client_user=name, kind="codex")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def call(self, action, **data):
        return lan.request(self.url, "test-secret", action, data)

    def test_discovery_metadata_and_reply(self):
        records = self.call("list", query="windows")
        self.assertEqual([r["id"] for r in records], ["windows-bob"])
        self.assertEqual(records[0]["machine"], "windows-bob")
        self.assertEqual(records[0]["client_user"], "windows-bob")
        self.assertEqual(self.call("show", agent="linux-alice")["kind"], "codex")
        first = self.call("send", sender="linux-alice", to="windows-bob", text="依頼\n日本語")
        batch = self.call("read", agent="windows-bob")
        self.assertEqual(batch["records"][0]["text"], "依頼\n日本語")
        self.call("send", sender="windows-bob", to="linux-alice", text="返信", reply_to=first["id"])
        self.assertEqual(self.call("read", agent="linux-alice")["records"][0]["reply_to"], first["id"])
        self.assertEqual(len(self.call("tail", agent="windows-bob", n=10)), 1)

    def test_read_disconnect_does_not_ack_and_ack_is_idempotent(self):
        self.call("send", sender="linux-alice", to="windows-bob", text="retain")
        batch = self.call("read", agent="windows-bob")
        self.assertEqual(self.call("read", agent="windows-bob"), batch)
        for _ in range(2):
            self.call("ack", agent="windows-bob", start=batch["start"], end=batch["end"])
        self.assertEqual(self.call("read", agent="windows-bob")["records"], [])
        self.assertEqual(len(self.call("tail", agent="windows-bob")), 1)

    def test_local_and_lan_share_inboxes_and_cursor(self):
        hub.send(self.workspace, "linux-alice", "windows-bob", "local to LAN")
        batch = self.call("read", agent="windows-bob")
        self.call("ack", agent="windows-bob", start=batch["start"], end=batch["end"])
        self.assertEqual(hub.receive(self.workspace, "windows-bob"), [])
        self.call("send", sender="windows-bob", to="linux-alice", text="LAN to local")
        self.assertEqual(hub.receive(self.workspace, "linux-alice")[0]["text"], "LAN to local")

    def test_parallel_writers_and_long_poll(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = pool.submit(self.call, "read", agent="windows-bob", wait=2)
            self.call("send", sender="linux-alice", to="windows-bob", text="wake")
            self.assertTrue(pending.result(timeout=4)["records"])
            list(pool.map(lambda i: self.call("send", sender="linux-alice", to="windows-bob", text=str(i)), range(30)))
        self.assertEqual(len(self.call("read", agent="windows-bob")["records"]), 31)

    def test_token_required_and_bad_requests_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTP 401"):
            lan.request(self.url, "wrong", "list", {})
        for action, data in (
            ("show", {"agent": "../outside"}),
            ("send", {"sender": "linux-alice", "to": "missing", "text": "bad"}),
            ("read", {"agent": "windows-bob", "wait": float("nan")}),
            ("read", {"agent": "windows-bob", "wait": 31}),
            ("tail", {"agent": "windows-bob", "n": -1}),
            ("register", {"agent": "a", "summary": None}),
            ("ack", {"agent": "windows-bob", "start": 0, "end": 99999}),
        ):
            with self.subTest(action=action, data=data), self.assertRaises(ValueError):
                self.call(action, **data)
        req = Request(self.url + "/v1/send", data=b"[]", headers={"Authorization": "Bearer test-secret"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_profile_update_keeps_messages(self):
        self.call("send", sender="linux-alice", to="windows-bob", text="keep")
        record = self.call("register", agent="windows-bob", summary="updated", update=True,
                           machine="windows-desktop", client_user="Bob")
        self.assertEqual(record["summary"], "updated")
        self.assertEqual(record["machine"], "windows-desktop")
        self.assertEqual(self.call("read", agent="windows-bob")["records"][0]["text"], "keep")

    def test_stale_or_partial_ack_does_not_advance_cursor(self):
        self.call("send", sender="linux-alice", to="windows-bob", text="first")
        first = self.call("read", agent="windows-bob")
        with self.assertRaisesRegex(ValueError, "complete record"):
            self.call("ack", agent="windows-bob", start=0, end=first["end"] - 1)
        self.call("ack", agent="windows-bob", start=first["start"], end=first["end"])
        self.call("send", sender="linux-alice", to="windows-bob", text="second")
        second = self.call("read", agent="windows-bob")
        with self.assertRaisesRegex(ValueError, "cursor changed"):
            self.call("ack", agent="windows-bob", start=0, end=second["end"])
        self.assertEqual(self.call("read", agent="windows-bob"), second)

    def test_client_output_failure_does_not_ack(self):
        self.call("send", sender="linux-alice", to="windows-bob", text="retain")
        argv = ["agent_lan.py", "--url", self.url, "read", "windows-bob"]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, AGENTBRIDGE_TOKEN="test-secret"):
            with patch("builtins.print", side_effect=BrokenPipeError):
                with self.assertRaises(SystemExit):
                    lan.main()
        self.assertEqual(self.call("read", agent="windows-bob")["records"][0]["text"], "retain")

    def test_cli_standalone_client_and_ack(self):
        # Copy only the network script: the client must not need the file backend.
        client = Path(self.temp.name) / "agent_lan.py"
        client.write_bytes((BIN / "agent_lan.py").read_bytes())
        env = dict(os.environ, AGENTBRIDGE_TOKEN="test-secret", PYTHONIOENCODING="utf-8")
        command = [sys.executable, str(client), "--url", self.url]
        def run(*args):
            return subprocess.run(command + list(args), env=env, check=True,
                                  capture_output=True, text=True, encoding="utf-8")
        run("register", "third-client", "--summary", "Windows クライアント")
        run("send", "third-client", "日本語", "--from", "linux-alice")
        self.assertEqual(json.loads(run("read", "third-client").stdout)["text"], "日本語")
        self.assertEqual(run("read", "third-client").stdout, "")


if __name__ == "__main__":
    unittest.main()
