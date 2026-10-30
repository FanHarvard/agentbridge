"""Shared hub routing, concurrent delivery and permissions (no real agents)."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BIN = Path(__import__("agentbridge").__file__).resolve().parent
sys.path.insert(0, str(BIN))
import agent_hub as hub


class HubTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "public"
        old = os.umask(0o077)
        try:
            for agent in ("alice", "bob", "carol"):
                hub.register(self.workspace, agent, f"{agent} development")
        finally:
            os.umask(old)

    def tearDown(self):
        self.temp.cleanup()

    def test_public_permissions_ignore_umask(self):
        self.assertEqual(self.workspace.stat().st_mode & 0o7777, 0o1777)
        self.assertEqual((self.workspace / "agents").stat().st_mode & 0o7777, 0o1777)
        directory = hub.agent_dir(self.workspace, "alice")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o777)
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o666)
        hub.receive(self.workspace, "alice")
        self.assertEqual((directory / ".read_offset").stat().st_mode & 0o777, 0o666)

    def test_arbitrary_agents_reply_and_unread(self):
        first = hub.send(self.workspace, "alice", "carol", "依頼")
        hub.send(self.workspace, "bob", "carol", "Review")
        records = hub.receive(self.workspace, "carol")
        self.assertEqual([r["source"] for r in records], ["alice", "bob"])
        self.assertEqual(hub.receive(self.workspace, "carol"), [])
        hub.send(self.workspace, "carol", "alice", "回答", first["id"])
        reply = hub.receive(self.workspace, "alice")[0]
        self.assertEqual(reply["reply_to"], first["id"])
        self.assertEqual(reply["kind"], "reply")

    def test_concurrent_writers_and_readers(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda i: hub.send(self.workspace, "alice", "bob", str(i)), range(80)))
            batches = list(pool.map(lambda _: hub.receive(self.workspace, "bob"), range(4)))
        received = [r for batch in batches for r in batch]
        self.assertEqual({r["id"] for r in received}, {r["id"] for r in records})
        self.assertEqual(len(received), 80)

    def test_invalid_and_unknown_agents_cannot_create_inbox(self):
        for name in ("../outside", "/tmp/agent", ".", "", "a/b"):
            with self.assertRaises(ValueError):
                hub.agent_dir(self.workspace, name)
        with self.assertRaises(FileNotFoundError):
            hub.send(self.workspace, "alice", "missing", "hello")
        self.assertFalse((self.workspace / "agents/missing").exists())

    def test_registration_collision_and_update(self):
        with self.assertRaises(ValueError):
            hub.register(self.workspace, "alice", "replacement")
        with patch.object(hub.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                hub.register(self.workspace, "alice", "replacement", update=True)
        hub.send(self.workspace, "bob", "alice", "keep")
        hub.register(self.workspace, "alice", "updated", update=True)
        self.assertEqual(hub.profile(self.workspace, "alice")["summary"], "updated")
        self.assertEqual(hub.receive(self.workspace, "alice")[0]["text"], "keep")

    def test_symlink_inbox_is_rejected(self):
        inbox = hub.agent_dir(self.workspace, "bob") / "inbox.jsonl"
        inbox.unlink()
        victim = Path(self.temp.name) / "victim"
        victim.write_text("untouched")
        inbox.symlink_to(victim)
        with self.assertRaises(OSError):
            hub.send(self.workspace, "alice", "bob", "bad")
        self.assertEqual(victim.read_text(), "untouched")

    def test_cli_discovery_and_wait(self):
        command = [sys.executable, str(BIN / "agent_hub.py"), "--workspace", str(self.workspace)]
        result = subprocess.run(command + ["list", "--query", "carol"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["id"], "carol")
        reader = subprocess.Popen(command + ["read", "bob", "--wait", "3"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            hub.send(self.workspace, "alice", "bob", "wake up")
            output, error = reader.communicate(timeout=5)
            self.assertEqual(reader.returncode, 0, error)
            self.assertEqual(json.loads(output)["text"], "wake up")
        finally:
            if reader.poll() is None:
                reader.kill()
                reader.communicate()

    def test_install_to_temporary_prefix_preserves_data(self):
        prefix = Path(self.temp.name) / "program"
        hub.send(self.workspace, "alice", "bob", "preserved")
        subprocess.run(["bash", str(Path(__file__).resolve().parents[1] / "bin/install.sh"), str(prefix), str(self.workspace)], check=True, capture_output=True)
        self.assertTrue((prefix / "bin/agent_hub.py").exists())
        self.assertEqual(hub.receive(self.workspace, "bob")[0]["text"], "preserved")


if __name__ == "__main__":
    unittest.main()
