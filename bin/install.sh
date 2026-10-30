#!/usr/bin/env bash
# Install read-only program files and initialize the cross-user public workspace.
# Usage: sudo bash bin/install.sh [/opt/agentbridge] [/opt/agentbridge-public]
set -euo pipefail
SOURCE=$(cd -- "$(dirname -- "$0")/.." && pwd)
PREFIX=${1:-/opt/agentbridge}
WORKSPACE=${2:-/opt/agentbridge-public}
if [[ "$PREFIX" != /* || "$WORKSPACE" != /* || "$PREFIX" == "$WORKSPACE" || "$PREFIX" == / || "$WORKSPACE" == / ]]; then
  echo 'Use distinct absolute program and workspace paths, other than /.' >&2
  exit 1
fi
case "$WORKSPACE/" in "$PREFIX/"*) echo 'Workspace must be outside program directory.' >&2; exit 1;; esac
case "$PREFIX/" in "$WORKSPACE/"*) echo 'Program must be outside workspace directory.' >&2; exit 1;; esac
if [[ -L "$PREFIX" || -L "$WORKSPACE" ]]; then
  echo 'Install targets must not be symbolic links.' >&2
  exit 1
fi
install -d -m 755 "$PREFIX" "$PREFIX/bin" "$PREFIX/templates" "$PREFIX/doc"
for file in "$SOURCE"/bin/*.py "$SOURCE"/bin/*.sh; do
  install -m 755 "$file" "$PREFIX/bin/"
done
for file in "$SOURCE"/templates/*; do install -m 644 "$file" "$PREFIX/templates/"; done
for file in "$SOURCE"/doc/*.html; do install -m 644 "$file" "$PREFIX/doc/"; done
python3 "$PREFIX/bin/agent_hub.py" --workspace "$WORKSPACE" init
# Existing targets must also be made public when explicitly installing.
chmod 1777 "$WORKSPACE" "$WORKSPACE/agents"
printf 'Program: %s\nPublic workspace: %s\n' "$PREFIX" "$WORKSPACE"
