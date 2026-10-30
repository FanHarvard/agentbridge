"""Compact, plain-text bridge notifications shared by session adapters."""
import json
import unicodedata

NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}


def compact(value, limit):
    # Prevent multiline/control characters from hiding the sender in notifications.
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(value))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def readable_text(record):
    text = str(record.get("text", ""))
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    if not isinstance(payload, dict):
        return text
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    questions = tool_input.get("questions")
    if isinstance(questions, list) and questions and all(isinstance(q, dict) for q in questions):
        lines = []
        for question in questions:
            lines.append(str(question.get("question", "")))
            lines.extend("・" + str(option.get("label", ""))
                         for option in (question.get("options") or []) if isinstance(option, dict))
        return "\n".join(lines)
    if isinstance(payload.get("answers"), dict):
        return "\n".join(f"{question}\n→ {answer}"
                         for question, answer in payload["answers"].items())
    if record.get("kind") == "permission_notice":
        return str(payload.get("message") or tool_input.get("description")
                   or tool_input.get("command") or payload.get("reply_route") or text)
    return text



def preview(record, recipient=None, default_sender="不明"):
    """Use an explicit summary when supplied; otherwise excerpt readable content."""
    source = record.get("source") or default_sender
    sender = compact(NAMES.get(source, source), 64)
    target = recipient or record.get("to")
    heading = sender
    if target:
        heading += " → " + compact(NAMES.get(target, target), 64)
    summary = record.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = readable_text(record)
    return heading + "｜" + (compact(summary, 120) or "（本文なし）")


def batch_preview(records, recipient=None, default_sender="不明"):
    lines = [preview(record, recipient, default_sender) for record in records[:8]]
    if len(records) > 8:
        lines.append(f"ほか {len(records) - 8} 件")
    return "\n".join(lines)
