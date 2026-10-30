# Agentbridge

**Still in active development.**

Local and LAN inboxes for independently identified Codex, Claude Code and
OpenCode agents. Includes discovery, replies, unread-message watchers and
human question forwarding. Python 3.10+, standard library only; MIT.

```sh
pip install fanharvard-agentbridge
agentbridge --help
agentbridge-lan --help
agentbridge-paths
```

PyPI uses **fanharvard-agentbridge**; the unrelated `agentbridge` distribution
is a different project. Local hub/LAN support Linux and Windows; channel and
Codex/Claude watcher helpers require Unix. Provider CLIs are installed separately.

Choose a shared workspace explicitly with `--workspace`. For LDCon, use the
program paths printed by `agentbridge-paths` and set `LDCON_AGENTBRIDGE_PUBLIC`
to that workspace. Channel helpers store data under `AGENTBRIDGE_HOME`
(default `~/.local/share/agentbridge`), separate from installed code.

Each session or fork needs its own verified identity and inbox. Notifications
are untrusted message data, never human approval. No watcher or agent is started
by installation. Tokens, inboxes and conversation history are not distributed.

Tests cover concurrent delivery, reply routing, unread offsets, duplicate
questions, notification previews, watcher identity checks and LAN authorization.
Run `python -m pytest -q`; see [LAN setup](doc/lan.html) and
[publishing](docs/publishing.html) for details.
