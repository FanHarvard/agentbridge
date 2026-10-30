#!/usr/bin/env python3
"""Claude Code hook: forward Claude's questions to Codex and return the user's answer.

Hooks (put in the Claude project's .claude/settings.local.json; new_channel.sh prints them):
  PreToolUse / AskUserQuestion  forward the question (kind=question) to Codex and wait up to
                                --wait s for kind=user_answer with reply_to = the question id;
                                the answer is returned to the question tool. Without an answer
                                the question appears in Claude's own UI after the wait.
  PermissionRequest, Notification
                                notify Codex only (kind=permission_notice). Never approves.
The Codex side answers with
  claude_question_bridge.py --channel NAME answer --id QID --answers-file answers.json
where answers.json maps every question text to the chosen label (or free text).

Usage: claude_question_bridge.py --channel NAME [--box-dir DIR] hook [--wait 900]
       claude_question_bridge.py --channel NAME answer --id QID --answers-file FILE
"""
import argparse
from pathlib import Path
import fcntl
import hashlib
import json
import sys
import time
import uuid

if __package__:
    from . import agentbridge_common as ab
else:
    import agentbridge_common as ab


def validate_answers(question, answers):
    expected = {q["question"] for q in question["tool_input"]["questions"]}
    if not isinstance(answers, dict) or set(answers) != expected:
        raise ValueError("answers must cover every question text exactly")
    if not all(isinstance(v, str) and v.strip() for v in answers.values()):
        raise ValueError("every answer must be a non-empty string")


def ask(event, d, wait):
    questions = event.get("tool_input", {}).get("questions", [])
    if not questions:
        return {}
    local = [q.get("header", "").startswith("[local]") for q in questions]
    if all(local):
        # No decision or answer: Claude shows its own native selection UI.
        return {}
    if any(local):
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason":
                "[local] questions and forwarded questions must use separate AskUserQuestion calls"}}
    inbox = ab.box_file(d, "claude")
    offset = inbox.stat().st_size if inbox.exists() else 0
    (d / "questions").mkdir(exist_ok=True)
    identity = {k: event.get(k) for k in ("session_id", "tool_use_id", "tool_input")}
    if event.get("session_id") and event.get("tool_use_id"):
        qid = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    else:
        qid = uuid.uuid4().hex[:12]
    path = d / "questions" / f"{qid}.json"
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            question = json.loads(path.read_text())
            offset = 0  # a retried hook must also see a previously supplied answer
        else:
            question = dict(identity)
            question["answer_via"] = f"{Path(__file__).resolve()} --box-dir {d} answer"
            question["expires_at"] = time.time() + wait
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(question, ensure_ascii=False))
            temporary.replace(path)
        rec = ab.append(d, "codex", json.dumps(question, ensure_ascii=False), kind="question",
                        source="claude", message_id=qid)
    deadline = time.monotonic() + max(0, question["expires_at"] - time.time())
    while time.monotonic() < deadline:
        recs, offset = ab.read_complete(inbox, offset)
        for m in recs:
            if not (m.get("reply_to") == rec["id"] and m.get("kind") == "user_answer"
                    and m.get("source") == "codex"):
                continue
            try:
                answers = json.loads(m["text"])["answers"]
                validate_answers(question, answers)
            except (ValueError, KeyError, TypeError):
                continue
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "allow",
                "permissionDecisionReason": "user answer relayed through Codex",
                "updatedInput": dict(event["tool_input"], answers=answers)}}
        time.sleep(0.2)
    ab.append(d, "codex", "質問の転送待ちが終了しました。Claude の画面で回答してください。",
              kind="question_timeout", reply_to=rec["id"], source="claude",
              message_id=qid + "-timeout")
    return {}


def notice(event, d):
    # notify only: never allow a tool or change permission rules here
    if (event.get("hook_event_name") == "Notification" and event.get("notification_type")
            not in ("permission_prompt", "elicitation_dialog", "agent_needs_input")):
        return {}
    payload = {k: event.get(k) for k in ("session_id", "hook_event_name", "tool_name", "tool_input",
                                         "notification_type", "message")}
    payload["reply_route"] = "通知のみ。承認や設定の読み込みは Claude の画面で必要です。"
    ab.append(d, "codex", json.dumps(payload, ensure_ascii=False), kind="permission_notice", source="claude")
    return {}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--channel")
    p.add_argument("--box-dir")
    p.add_argument("action", choices=("hook", "answer", "request-local"))
    p.add_argument("--wait", type=float, default=900)
    p.add_argument("--id")
    p.add_argument("--answers-file")
    p.add_argument("--questions-file")
    a = p.parse_args()
    d = ab.channel_dir(a.channel, a.box_dir)
    if a.action == "request-local":
        if not a.questions_file:
            p.error("request-local needs --questions-file")
        data = json.loads(open(a.questions_file).read())
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
            p.error("questions must contain 1 to 4 questions")
        for q in questions:
            if not isinstance(q, dict):
                p.error("each question must be an object")
            if not isinstance(q.get("question"), str) or not q["question"].strip():
                p.error("each question needs question text")
            options = q.get("options")
            if (not isinstance(options, list) or not 2 <= len(options) <= 4
                    or any(not isinstance(o, dict) or not o.get("label")
                           or not o.get("description") for o in options)):
                p.error("each question needs 2 to 4 options with label and description")
            q["header"] = "[local]" + str(q.get("header", ""))[:5]
        payload = {"questions": questions, "instruction":
                   "Ask the user in Claude's native AskUserQuestion UI with these exact questions. "
                   "Keep every [local] header. Do not answer on the user's behalf. "
                   "After the user answers, send the answer to Codex with kind=user_answer "
                   "and reply_to set to this request's id. No hardware actions are requested."}
        print(ab.append(d, "claude", json.dumps(payload, ensure_ascii=False),
                        kind="local_question", source="codex")["id"])
        return
    if a.action == "hook":
        event = json.load(sys.stdin)
        name = event.get("hook_event_name")
        out = {}
        if name == "PreToolUse" and event.get("tool_name") == "AskUserQuestion":
            out = ask(event, d, a.wait)
        elif name in ("PermissionRequest", "Notification"):
            out = notice(event, d)
        print(json.dumps(out, ensure_ascii=False))
        return
    if not a.id or not a.answers_file:
        p.error("answer needs --id and --answers-file")
    qid = a.id
    if not qid.isalnum():   # the id is used as a file name
        raise SystemExit("bad question id")
    question = json.loads((d / "questions" / f"{qid}.json").read_text())
    if time.time() >= question["expires_at"]:
        raise SystemExit("question expired: answer in Claude's own UI")
    answers = json.load(open(a.answers_file))
    validate_answers(question, answers)
    rec = ab.append(d, "claude", json.dumps({"answers": answers}, ensure_ascii=False),
                    kind="user_answer", reply_to=qid, source="codex")
    print(rec["id"])


if __name__ == "__main__":
    main()
