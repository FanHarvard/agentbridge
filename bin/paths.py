"""Print installed program locations; creates no state and reads no inbox."""
import argparse
import json
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    root = Path(__file__).resolve().parent
    print(json.dumps({"LDCON_AGENTBRIDGE_HUB": str(root / "agent_hub.py"),
                      "LDCON_AGENTBRIDGE_SCRIPT": str(root / "agent_msg.py")}, indent=2))
