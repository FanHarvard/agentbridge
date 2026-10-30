"""Exercise hub delivery without queueing real Codex turns."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__import__("agentbridge").__file__).resolve().parent))
import agent_hub as hub
import codex_hub_watch as watcher


class HubWatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / 'hub'
        self.state = Path(self.temp.name) / 'state'
        self.state.mkdir()
        hub.register(self.workspace, 'receiver', 'Receiver')
        hub.register(self.workspace, 'sender', 'Sender')
        hub.send(self.workspace, 'sender', 'receiver', '確認完了\n問題ありません')

    def notify(self):
        watcher.notify(self.workspace, 'receiver', 'verified-thread', 'codex', self.state)

    def test_preview_without_ack_and_no_duplicate_after_restart(self):
        with patch.object(watcher.subprocess, 'run') as queue:
            self.notify()
            args = queue.call_args.args[0]
            self.assertEqual(args[:4], ['codex', 'queue', '--thread', 'verified-thread'])
            self.assertEqual(args[-1], '[agentbridge] sender → Codex｜確認完了 問題ありません')
            self.assertEqual(hub.load_offset(hub.agent_dir(self.workspace, 'receiver') / '.read_offset'), 0)
            self.notify()
            queue.assert_called_once()
            self.assertEqual(len(hub.receive(self.workspace, 'receiver')), 1)
            self.notify()
            queue.assert_called_once()
            hub.send(self.workspace, 'sender', 'receiver', '次の報告')
            self.notify()
            self.assertEqual(queue.call_count, 2)

    def test_queue_failure_does_not_mark_notification_sent(self):
        with patch.object(watcher.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'codex')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.notify()
        self.assertFalse((self.state / 'pending_offset').exists())
        self.assertEqual(len(hub.receive(self.workspace, 'receiver')), 1)

    def test_waiting_reader_owns_delivery(self):
        with hub.locked(hub.agent_dir(self.workspace, 'receiver') / '.read.lock'):
            with patch.object(watcher.subprocess, 'run') as queue:
                self.notify()
                queue.assert_not_called()
