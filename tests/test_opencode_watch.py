"""OpenCode notifications: real HTTP adapter + durable, non-consuming polling."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError

BIN = Path(__import__("agentbridge").__file__).resolve().parent
sys.path.insert(0, str(BIN))
import agent_hub as hub
import opencode_watch as watch


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith('/inbox'):
            self.reply(200, {'data': [{'id': k, 'payload': v} for k, v in self.server.messages.items()]})
        elif '/message/' in self.path:
            self.reply(404, {})
        else:
            self.reply(200, {'data': {'id': 'ses_test'}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.server.fail:
            self.reply(503, {})
        elif body['id'] in self.server.messages:
            self.reply(409, {})
        else:
            self.server.messages[body['id']] = body
            self.reply(200, {'data': body})


class OpenCodeWatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'public'
        for agent in ('sender', 'receiver'):
            hub.register(self.workspace, agent, agent)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.messages, self.server.fail = {}, False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.oc = watch.OpenCode(self.url, 'ses_test')
        self.db = watch.connect_state(self.root / 'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def tick(self, **kwargs):
        return watch.tick(self.db, 'receiver', self.workspace, None, self.oc, **kwargs)

    def test_summary_full_text_and_non_consuming_delivery(self):
        rec = hub.send(self.workspace, 'sender', 'receiver', '日本語\n' + '長い本文' * 60)
        self.assertEqual(self.tick(), 1)
        body = next(iter(self.server.messages.values()))
        self.assertTrue(body['description'].startswith('sender → OpenCode｜日本語'))
        self.assertLessEqual(len(body['description'].split('｜')[1]), 120)
        self.assertIn(rec['id'], body['text'])
        self.assertFalse(body['resume'])
        self.assertEqual(hub.receive(self.workspace, 'receiver')[0]['id'], rec['id'])
        self.assertEqual(self.tick(), 0)

    def test_failure_retry_and_restart_does_not_duplicate(self):
        hub.send(self.workspace, 'sender', 'receiver', 'Retry')
        self.server.fail = True
        with self.assertRaises(HTTPError): self.tick()
        self.server.fail = False
        self.assertEqual(self.tick(), 1)
        self.db.close()
        self.db = watch.connect_state(self.root / 'state.sqlite')
        self.assertEqual(self.tick(), 0)
        self.assertEqual(len(self.server.messages), 1)

    def test_server_acceptance_before_state_commit_is_deduplicated(self):
        rec = hub.send(self.workspace, 'sender', 'receiver', 'Retry after lost response')
        self.oc.deliver('receiver', rec)
        self.assertEqual(self.tick(), 1)
        self.assertEqual(len(self.server.messages), 1)

    def test_conflict_with_different_content_is_not_ignored(self):
        rec = hub.send(self.workspace, 'sender', 'receiver', 'original')
        mid = self.oc.deliver('receiver', rec)
        self.server.messages[mid]['text'] = 'different'
        with self.assertRaises(ValueError): self.tick()
        self.assertEqual(self.db.execute('SELECT count(*) FROM delivered').fetchone()[0], 0)

    def test_start_now_and_fast_consuming_peer(self):
        hub.send(self.workspace, 'sender', 'receiver', 'old')
        self.assertEqual(self.tick(start_now=True), 0)
        hub.send(self.workspace, 'sender', 'receiver', 'new')
        hub.receive(self.workspace, 'receiver')
        self.assertEqual(self.tick(start_now=True), 1)
        self.assertIn('new', next(iter(self.server.messages.values()))['description'])

    def test_background_start_status_double_start_stop(self):
        command = [sys.executable, str(BIN / 'opencode_watch.py'), '--agent', 'receiver',
                   '--workspace', str(self.workspace), '--opencode-url', self.url,
                   '--session', 'ses_test', '--state-dir', str(self.root / 'daemon'), '--poll', '0.2']
        def cli(action):
            return subprocess.run(command + [action], capture_output=True, text=True, check=True).stdout
        try:
            self.assertIn('started', cli('start'))
            self.assertTrue(json.loads(cli('status'))['running'])
            self.assertIn('already running', cli('start'))
            hub.send(self.workspace, 'sender', 'receiver', 'background')
            deadline = time.monotonic() + 4
            while not self.server.messages and time.monotonic() < deadline: time.sleep(0.05)
            self.assertEqual(len(self.server.messages), 1)
        finally:
            cli('stop')
            deadline = time.monotonic() + 4
            while json.loads(cli('status'))['running'] and time.monotonic() < deadline: time.sleep(0.05)
        self.assertFalse(json.loads(cli('status'))['running'])

    def test_web_url_and_session_mismatch(self):
        import base64
        encoded = base64.urlsafe_b64encode(self.url.encode()).decode().rstrip('=')
        url = self.url + '/server/' + encoded + '/session/ses_test'
        self.assertEqual(watch.destination(url), (self.url, 'ses_test'))
        with self.assertRaises(ValueError): watch.destination(url, 'ses_other')


if __name__ == '__main__':
    unittest.main()
