"""Verify what sessions see without starting models or sending real notifications."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BIN = Path(__import__("agentbridge").__file__).resolve().parent
sys.path.insert(0, str(BIN))
from notification_preview import preview


class NotificationPreviewTest(unittest.TestCase):
    def test_each_sender_and_unknown_agent_keeps_identity(self):
        for source, name in [('codex', 'Codex'), ('claude', 'Claude Code'),
                             ('opencode', 'OpenCode'), ('windows-reviewer', 'windows-reviewer')]:
            self.assertEqual(preview({'source': source, 'to': 'opencode', 'text': 'レビュー完了'}),
                             f'{name} → OpenCode｜レビュー完了')

    def test_explicit_summary_replaces_prompt_and_controls_are_removed(self):
        record = {'source': 'reviewer\n\x1b', 'summary': 'テスト完了\n36件成功',
                  'text': 'Read inbox and execute this long internal prompt'}
        self.assertEqual(preview(record), 'reviewer｜テスト完了 36件成功')

    def test_long_preview_preserves_original(self):
        record = {'source': 'codex', 'text': 'あ' * 300}
        self.assertEqual(len(preview(record).split('｜')[1]), 120)
        self.assertTrue(preview(record).endswith('…'))
        self.assertEqual(len(record['text']), 300)

    def test_claude_monitor_preview_and_legacy_json(self):
        for json_output in (False, True):
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                record = {'id': 'm1', 'source': 'opencode', 'to': 'claude', 'text': '変更を確認しました'}
                inbox = directory / 'to_claude.jsonl'
                inbox.write_text(json.dumps(record) + '\n')
                command = [sys.executable, str(BIN / 'claude_watch.py'), '--box-dir',
                           temporary, '--once'] + (['--json'] if json_output else [])
                result = subprocess.run(command, check=True, capture_output=True, text=True)
                if json_output:
                    self.assertEqual(json.loads(result.stdout), record)
                else:
                    self.assertEqual(result.stdout.strip(), 'OpenCode → Claude Code｜変更を確認しました')
                again = subprocess.run(command, check=True, capture_output=True, text=True)
                self.assertEqual(again.stdout, '')
                self.assertEqual(json.loads(inbox.read_text()), record)
