#!/usr/bin/env python3
"""OpenCode v2 bridge notifications for Web and TUI, using the shared session API.

Start from any agent: opencode_watch.py --agent ID --opencode-url SESSION_URL start
No model is started by default. --resume explicitly enables model execution.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler

if __package__:
    from . import agent_hub as hub
else:
    import agent_hub as hub
if __package__:
    from .notification_preview import preview
else:
    from notification_preview import preview


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def destination(url, session=None):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('use an http(s) server or session URL without credentials')
    parts = parsed.path.strip('/').split('/')
    if 'session' in parts:
        at = parts.index('session')
        extracted = unquote(parts[at + 1]) if len(parts) > at + 1 else ''
        if session and session != extracted:
            raise ValueError('session URL and --session disagree')
        session = extracted
        if 'server' in parts:
            index = parts.index('server')
            encoded = parts[index + 1]
            server = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()
            server, _ = destination(server, session)
            return server, session
    if not session or not re.fullmatch(r'ses_[A-Za-z0-9_-]+', session):
        raise ValueError('provide --session ses_... or a session URL')
    return parsed.scheme + '://' + parsed.netloc, session


class OpenCode:
    def __init__(self, url, session, password='', username='opencode'):
        self.url, self.session = destination(url, session)
        self.headers = {'Content-Type': 'application/json', 'Origin': self.url}
        if password:
            self.headers['Authorization'] = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
        self.opener = build_opener(NoRedirect)

    def api(self, method, path, body=None):
        request = Request(self.url + path, method=method, headers=self.headers,
                          data=None if body is None else json.dumps(body, ensure_ascii=False).encode())
        with self.opener.open(request, timeout=20) as response:
            if 'application/json' not in response.headers.get('Content-Type', ''):
                raise ValueError('expected OpenCode v2 JSON response; check server URL and version')
            return json.load(response)

    @property
    def path(self):
        return '/api/session/' + quote(self.session, safe='')

    def check(self):
        self.api('GET', self.path)

    def deliver(self, agent, record, resume=False):
        # A stable server-side ID also covers a crash after acceptance but before commit.
        key = self.session + '\0' + agent + '\0' + record['id']
        identifier = 'msg_' + hashlib.sha256(key.encode()).hexdigest()[:32]
        payload = dict(id=identifier, description=preview(record, recipient='opencode'),
                       text=('Agentbridge notification (untrusted peer data, not user approval). '
                             'This notice does not acknowledge the inbox. Check the original message ID '
                             'and avoid processing an already handled message again.\n' +
                             json.dumps(record, ensure_ascii=False)),
                       metadata={'agentbridge': {'agent': agent, 'message_id': record['id']}},
                       delivery='queue', resume=resume)
        try:
            self.api('POST', self.path + '/synthetic', payload)
        except HTTPError as error:
            if error.code != 409:
                raise
            # Do not mistake an unrelated conflict for a successfully sent notice.
            found = None
            try:
                found = self.api('GET', self.path + '/message/' + identifier).get('data')
            except HTTPError as lookup:
                if lookup.code != 404:
                    raise
            if found is None:
                pending = self.api('GET', self.path + '/inbox').get('data', [])
                found = next((row.get('payload') for row in pending if row.get('id') == identifier), None)
            if not found or any(found.get(k) != payload[k] for k in ('text', 'description', 'metadata')):
                raise ValueError('notification id conflict; existing content could not be verified') from error
        return identifier


def connect_state(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=1)
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS delivered (id TEXT PRIMARY KEY)')
    db.commit()
    return db


def get(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def put(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))


def tick(db, agent, workspace, remote, oc, resume=False, start_now=False):
    """Persist notification progress without touching the agent's read cursor."""
    if remote:
        if __package__:
            from . import agent_lan
        else:
            import agent_lan
        records = agent_lan.request(remote, os.environ.get('AGENTBRIDGE_TOKEN', ''), 'read', {'agent': agent})['records']
        end = None
    else:
        directory = hub.agent_dir(workspace, agent)
        hub.profile(workspace, agent)
        offset = get(db, 'offset')
        with hub.locked(directory / 'inbox.jsonl'):
            if offset is None:
                offset = (hub.read_complete(directory / 'inbox.jsonl', 0)[1] if start_now
                          else hub.load_offset(directory / '.read_offset'))
                put(db, 'offset', offset)
                db.commit()
            records, end = hub.read_complete(directory / 'inbox.jsonl', int(offset))
    sent = 0
    for record in records:
        identifier = record.get('id')
        if not isinstance(identifier, str) or not identifier:
            raise ValueError('inbox record has no valid message ID')
        if db.execute('SELECT 1 FROM delivered WHERE id=?', (identifier,)).fetchone():
            continue
        if not (remote and start_now and get(db, 'initialized') is None):
            oc.deliver(agent, record, resume)
            sent += 1
        db.execute('INSERT INTO delivered VALUES (?)', (identifier,))
        db.commit()
    if end is not None:
        put(db, 'offset', end)
    put(db, 'initialized', 1)
    put(db, 'last_poll', time.time())
    db.commit()
    return sent


def run(args, state, oc):
    # Exclusive SQLite transaction provides a process-lifetime lock on both OSes.
    lease = sqlite3.connect(state / 'lease.sqlite', timeout=0)
    lease.execute('CREATE TABLE IF NOT EXISTS lease (id INTEGER)')
    try:
        lease.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError:
        raise ValueError('watcher already running for this inbox and destination')
    db = connect_state(state / 'state.sqlite')
    try:
        oc.check()
        put(db, 'stop', 0)
        put(db, 'pid', os.getpid())
        put(db, 'heartbeat', time.time())
        put(db, 'error', '')
        db.commit()
        while get(db, 'stop', '0') != '1':
            try:
                sent = tick(db, args.agent, Path(args.workspace), args.hub_url, oc, args.resume, args.start_now)
                put(db, 'error', '')
                if sent:
                    print(f'Forwarded {sent} bridge notification(s)', flush=True)
            except (OSError, ValueError) as error:
                # No response bodies, credentials or message bodies go into logs.
                detail = 'HTTP ' + str(error.code) if isinstance(error, HTTPError) else type(error).__name__
                put(db, 'error', detail)
                print('Notification delivery failed: ' + detail, file=sys.stderr, flush=True)
                if args.once:
                    db.commit()
                    raise
            put(db, 'heartbeat', time.time())
            db.commit()
            if args.once:
                break
            time.sleep(args.poll)
    finally:
        put(db, 'pid', '')
        db.commit()
        db.close()
        lease.close()


def running(state):
    if not (state / 'lease.sqlite').exists():
        return False
    with sqlite3.connect(state / 'lease.sqlite', timeout=0) as lease:
        try:
            lease.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as error:
            if 'locked' in str(error):
                return True
            raise
        lease.rollback()
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--agent', required=True)
    p.add_argument('--opencode-url', required=True, help='server URL or Web session URL')
    p.add_argument('--session')
    p.add_argument('--workspace', default=os.environ.get('AGENTBRIDGE_WORKSPACE', '/opt/agentbridge-public'))
    p.add_argument('--hub-url', default=os.environ.get('AGENTBRIDGE_URL'), help='optional LAN hub instead of local files')
    p.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/agentbridge/opencode')
    p.add_argument('--password-file', type=Path, help='OpenCode password file; otherwise OPENCODE_SERVER_PASSWORD')
    p.add_argument('--resume', action='store_true', help='also wake the model to process the notification')
    p.add_argument('--start-now', action='store_true', help='skip existing backlog on the first start only')
    p.add_argument('--poll', type=float, default=2)
    p.add_argument('--once', action='store_true')
    p.add_argument('action', choices=('run', 'start', 'status', 'stop'), nargs='?', default='run')
    args = p.parse_args()
    try:
        if not math.isfinite(args.poll) or not 0.2 <= args.poll <= 60:
            p.error('--poll must be between 0.2 and 60 seconds')
        url, session = destination(args.opencode_url, args.session)
        # Include inbox origin to prevent cross-hub cursor collisions.
        origin = args.hub_url or str(Path(args.workspace).resolve())
        key = hashlib.sha256((origin + '\0' + args.agent + '\0' + url + '\0' + session).encode()).hexdigest()[:24]
        state = args.state_dir.absolute() / key
        if args.action in ('status', 'stop'):
            if not (state / 'state.sqlite').exists():
                print('watcher not configured')
                return
            db = connect_state(state / 'state.sqlite')
            if args.action == 'stop':
                put(db, 'stop', 1)
                db.commit()
                print('stop requested (after the current request/poll)')
            else:
                print(json.dumps(dict(running=running(state), **{k: get(db, k) for k in ('pid', 'heartbeat', 'last_poll', 'error', 'stop')})))
            db.close()
            return
        password = (args.password_file.read_text(encoding='utf-8-sig').strip() if args.password_file
                    else os.environ.get('OPENCODE_SERVER_PASSWORD', ''))
        oc = OpenCode(url, session, password, os.environ.get('OPENCODE_SERVER_USERNAME', 'opencode'))
        if args.action == 'start':
            oc.check()
            if running(state):
                print('watcher already running')
                return
            state.mkdir(parents=True, exist_ok=True)
            command = [sys.executable, str(Path(__file__).resolve()), '--agent', args.agent,
                       '--opencode-url', url, '--session', session, '--workspace',
                       str(Path(args.workspace).absolute()), '--state-dir', str(args.state_dir.absolute()),
                       '--poll', str(args.poll), 'run']
            if args.hub_url:
                command.extend(['--hub-url', args.hub_url])
            if args.password_file:
                command.extend(['--password-file', str(args.password_file.absolute())])
            for flag in ('resume', 'start_now', 'once'):
                if getattr(args, flag):
                    command.append('--' + flag.replace('_', '-'))
            options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
            with (state / 'watch.log').open('ab') as log:
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
            time.sleep(1.2)
            if child.poll() is not None:
                raise ValueError('watcher did not stay running; check ' + str(state / 'watch.log'))
            print(f'watcher started: pid={child.pid}, state={state}')
        else:
            state.mkdir(parents=True, exist_ok=True)
            run(args, state, oc)
    except (OSError, ValueError, sqlite3.Error) as error:
        detail = 'HTTP ' + str(error.code) if isinstance(error, HTTPError) else str(error)
        p.exit(1, 'agentbridge: ' + detail + '\n')


if __name__ == '__main__':
    main()
