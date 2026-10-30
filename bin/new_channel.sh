#!/bin/bash
# Create a Claude <-> Codex channel and print the setup for both sides.
# Usage: new_channel.sh NAME [CLAUDE_PROJECT_DIR] [CODEX_THREAD_ID] [CODEX_PROJECT_DIR]
# NAME: letters, digits, - and _. Re-running updates channel.json and keeps the messages.
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAME=$1
[[ "$NAME" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "usage: $0 NAME [CLAUDE_PROJECT_DIR] [CODEX_THREAD_ID] [CODEX_PROJECT_DIR]"; exit 1; }
D=${AGENTBRIDGE_HOME:-$HOME/.local/share/agentbridge}/boxes/$NAME
mkdir -p "$D/questions"
touch "$D/to_claude.jsonl" "$D/to_codex.jsonl"
python3 - "$D" "$NAME" "${2:-}" "${3:-}" "${4:-}" <<'EOF'
import json, sys, time
from pathlib import Path
d, name, cproj, thread, xproj = sys.argv[1:]
p = Path(d) / "channel.json"
s = json.loads(p.read_text()) if p.exists() else {"created": time.strftime("%Y-%m-%d %H:%M:%S %Z")}
s["name"] = name
for k, v in (("claude_project", cproj), ("codex_thread", thread), ("codex_project", xproj)):
    if v:
        s[k] = v
p.write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")
print(json.dumps(s, ensure_ascii=False))
EOF
B=$ROOT/bin
HOOK="python3 $B/claude_question_bridge.py --channel $NAME hook"
cat <<EOF

=== channel $NAME: $D

--- Claude Code side ---
1) Watch for messages (ask Claude in the session; re-arm every 30 min):
   Monitor command: python3 $B/claude_watch.py --channel $NAME   (timeout_ms 1800000)
2) Send / read:
   python3 $B/agent_msg.py --channel $NAME send codex "text" [--reply-to ID]
3) Optional question forwarding: merge into <claude project>/.claude/settings.local.json
EOF
sed "s#@HOOK@#$HOOK#g" "$ROOT/templates/claude_hooks.json"
cat <<EOF

--- Codex side ---
1) Background notifier (wakes the Codex thread only when Claude writes):
   setsid nohup python3 $B/codex_watch.py --channel $NAME >> $D/codex_watch.log 2>&1 &
   status: python3 $B/codex_watch.py --channel $NAME --status
2) Read / send:
   python3 $B/agent_msg.py --channel $NAME read codex
   python3 $B/agent_msg.py --channel $NAME send claude "text" [--reply-to ID]
3) Answer a forwarded question:
   python3 $B/claude_question_bridge.py --channel $NAME answer --id QID --answers-file answers.json
Snippets for AGENTS.md / CLAUDE.md: $ROOT/templates/
EOF
