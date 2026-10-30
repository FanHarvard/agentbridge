"""Exercise routing and delivery coordination without invoking an agent or hardware."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BIN = Path(__import__("agentbridge").__file__).resolve().parent
sys.path.insert(0, str(BIN))
import agentbridge_common as ab
import claude_question_bridge as qb
import codex_watch as cw


class BridgeTest(unittest.TestCase):
    def test_same_question_hook_emits_once_and_preserves_expiry(self):
        event = {"session_id": "session", "tool_use_id": "tool1",
                 "tool_input": {"questions": [{"question": "Q?"}]}}
        qb.ask(event, self.d, 0)
        first, _ = ab.read_complete(self.d / "to_codex.jsonl", 0)
        qb.ask(event, self.d, 0)
        second, _ = ab.read_complete(self.d / "to_codex.jsonl", 0)
        self.assertEqual(second, first)
        self.assertEqual([r["kind"] for r in second], ["question", "question_timeout"])

    def test_new_tool_call_with_same_words_is_not_suppressed(self):
        event = {"session_id": "session", "tool_use_id": "tool1",
                 "tool_input": {"questions": [{"question": "Q?"}]}}
        qb.ask(event, self.d, 0)
        qb.ask(dict(event, tool_use_id="tool2"), self.d, 0)
        rows, _ = ab.read_complete(self.d / "to_codex.jsonl", 0)
        self.assertEqual(sum(r["kind"] == "question" for r in rows), 2)

    def test_retry_returns_existing_user_answer_without_another_notification(self):
        event = {"session_id": "session", "tool_use_id": "tool1",
                 "tool_input": {"questions": [{"question": "Q?"}]}}
        def answer(_):
            rows, _ = ab.read_complete(self.d / "to_codex.jsonl", 0)
            ab.append(self.d, "claude", json.dumps({"answers": {"Q?": "Yes"}}),
                      kind="user_answer", source="codex", reply_to=rows[0]["id"])
        with patch.object(qb.time, "sleep", side_effect=answer):
            output = qb.ask(event, self.d, 2)
        retried = qb.ask(event, self.d, 2)
        self.assertEqual(output, retried)
        self.assertEqual(output["hookSpecificOutput"]["updatedInput"]["answers"], {"Q?": "Yes"})
        rows, _ = ab.read_complete(self.d / "to_codex.jsonl", 0)
        self.assertEqual(len(rows), 1)

    def test_append_once_rejects_changed_message_with_same_id(self):
        ab.append(self.d, "codex", "one", message_id="stable")
        ab.append(self.d, "codex", "one", message_id="stable")
        with self.assertRaises(ValueError):
            ab.append(self.d, "codex", "different", message_id="stable")
        rows, _ = ab.read_complete(self.d / "to_codex.jsonl", 0)
        self.assertEqual(len(rows), 1)

    def test_notification_contains_sender_and_short_preview(self):
        record = {"id": "test-id", "kind": "hourly_check", "t": "2026-10-17 22:44 EDT",
                  "text": "Normal\nTEC 48.6 mV"}
        message = cw.notification_message(Path("ecdl"), [record])
        self.assertEqual(message, "Claude Code → Codex｜Normal TEC 48.6 mV")
        self.assertNotIn("test-id", message)

    def test_notification_question_and_answer_are_readable(self):
        record = {"kind": "question", "text": json.dumps({"tool_input": {
            "questions": [{"question": "Q?", "options": [{"label": "Yes"}, {"label": "No"}]}]}})}
        self.assertEqual(cw.readable_text(record), "Q?\n・Yes\n・No")
        record = {"kind": "user_answer", "text": json.dumps({"answers": {"Q?": "Yes"}})}
        self.assertEqual(cw.readable_text(record), "Q?\n→ Yes")
        record = {"kind": "permission_notice", "text": json.dumps({
            "tool_input": None, "message": "Permission needed"})}
        self.assertEqual(cw.readable_text(record), "Permission needed")

    def test_notification_size_is_bounded_and_truncation_is_visible(self):
        record = {"text": "あ" * 10000}
        message = cw.notification_message(Path("ecdl"), [record] * 20)
        self.assertLess(len(message), 1600)
        self.assertIn("…", message)
        self.assertIn("ほか 12 件", message)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.d = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_local_stays_native_without_forward_or_answer(self):
        event = {"tool_input": {"questions": [{"header": "[local]Test", "question": "Q?"}]}}
        self.assertEqual(qb.ask(event, self.d, 0), {})
        self.assertFalse((self.d / "to_codex.jsonl").exists())

    def test_mixed_routes_must_be_separate(self):
        event = {"tool_input": {"questions": [{"header": "[local]Test"}, {"header": "Normal"}]}}
        out = qb.ask(event, self.d, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertFalse((self.d / "to_codex.jsonl").exists())

    def test_default_route_still_forwards_and_times_out(self):
        event = {"tool_input": {"questions": [{"header": "Normal", "question": "Q?"}]}}
        self.assertEqual(qb.ask(event, self.d, 0), {})
        rows, _ = ab.read_complete(self.d / "to_codex.jsonl", 0)
        self.assertEqual([r["kind"] for r in rows], ["question", "question_timeout"])

    def test_permission_hook_does_not_approve(self):
        self.assertEqual(qb.notice({"hook_event_name": "PermissionRequest"}, self.d), {})

    def test_request_local_cli_tags_and_sends_questions(self):
        source = self.d / "questions.json"
        source.write_text(json.dumps({"questions": [{"question": "Q?", "header": "A long header",
                           "options": [{"label": "Yes", "description": "Yes answer"},
                                       {"label": "No", "description": "No answer"}]}]}))
        result = subprocess.run([sys.executable, str(BIN / "claude_question_bridge.py"),
                                 "--box-dir", str(self.d), "request-local",
                                 "--questions-file", str(source)], check=True, capture_output=True, text=True)
        rows, _ = ab.read_complete(self.d / "to_claude.jsonl", 0)
        self.assertEqual(rows[0]["id"], result.stdout.strip())
        self.assertEqual(rows[0]["kind"], "local_question")
        header = json.loads(rows[0]["text"])["questions"][0]["header"]
        self.assertTrue(header.startswith("[local]"))
        self.assertLessEqual(len(header), 12)

    def test_read_before_notifier_produces_no_empty_notice(self):
        ab.append(self.d, "codex", "reply")
        self.assertEqual(len(ab.receive(self.d, "codex")), 1)
        with patch.object(cw.subprocess, "run") as run:
            cw.notify(self.d, "test", "codex")
            run.assert_not_called()

    def test_waiting_reader_owns_delivery(self):
        ab.append(self.d, "codex", "reply")
        with ab.delivery_lock(self.d, "codex"):
            with patch.object(cw.subprocess, "run") as run:
                cw.notify(self.d, "test", "codex")
                run.assert_not_called()
        ab.receive(self.d, "codex")
        with patch.object(cw.subprocess, "run") as run:
            cw.notify(self.d, "test", "codex")
            run.assert_not_called()

    def test_actual_waiting_cli_and_notifier_do_not_duplicate(self):
        proc = subprocess.Popen([sys.executable, str(BIN / "agent_msg.py"), "--box-dir",
                                 str(self.d), "read", "codex", "--wait", "3"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            locked = False
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if (self.d / ".delivery_codex.lock").exists():
                    with ab.delivery_lock(self.d, "codex", blocking=False) as acquired:
                        locked = not acquired
                    if locked:
                        break
                time.sleep(0.01)
            self.assertTrue(locked)
            ab.append(self.d, "codex", "waiting reply")
            with patch.object(cw.subprocess, "run") as run:
                cw.notify(self.d, "test", "codex")
                stdout, stderr = proc.communicate(timeout=4)
                self.assertEqual(proc.returncode, 0, stderr)
                self.assertEqual(json.loads(stdout)["text"], "waiting reply")
                cw.notify(self.d, "test", "codex")
                run.assert_not_called()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_queue_coalesces_until_read_then_next_message_notifies(self):
        with patch.object(cw.subprocess, "run") as run:
            ab.append(self.d, "codex", "first")
            cw.notify(self.d, "test", "codex")
            ab.append(self.d, "codex", "second")
            cw.notify(self.d, "test", "codex")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(ab.receive(self.d, "codex")), 2)
            cw.notify(self.d, "test", "codex")
            self.assertEqual(run.call_count, 1)
            ab.append(self.d, "codex", "third")
            cw.notify(self.d, "test", "codex")
            self.assertEqual(run.call_count, 2)

    def test_queue_failure_retries_without_ack(self):
        ab.append(self.d, "codex", "reply")
        with patch.object(cw.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "codex")):
            with self.assertRaises(subprocess.CalledProcessError):
                cw.notify(self.d, "test", "codex")
        self.assertEqual(ab.load_offset(self.d / ".codex_notify_offset"), 0)
        with patch.object(cw.subprocess, "run") as run:
            cw.notify(self.d, "test", "codex")
            run.assert_called_once()

    def test_accepted_queue_is_not_repeated_while_user_is_busy(self):
        ab.append(self.d, "codex", "reply")
        with patch.object(cw.subprocess, "run") as run:
            cw.notify(self.d, "test", "codex")
            os.utime(self.d / ".codex_pending_offset", (0, 0))
            cw.notify(self.d, "test", "codex")
            self.assertEqual(run.call_count, 1)

    def test_read_timeout_releases_notifier(self):
        self.assertEqual(ab.receive(self.d, "codex", 0.01), [])
        ab.append(self.d, "codex", "after timeout")
        with patch.object(cw.subprocess, "run") as run:
            cw.notify(self.d, "test", "codex")
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
