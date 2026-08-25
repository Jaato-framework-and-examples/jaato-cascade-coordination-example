#!/usr/bin/env python3
"""Render every session of one cascade as one readable document.

    python3 -m examples.render_cascade [<cascade-id>] > cascade.md

Derived from `tools/dump_turns.py` in prime-agents-vs-jaato, which renders
ONE session: prose, every tool call with its arguments, every result.  The
driver's stdout reports outcomes; the transcript shows the reasoning that
produced them, and the two can disagree.

What is added here is the part a cascade needs and a single session cannot
have: **the siblings are rendered together.**  Sessions carry
``cascade_driver_id`` and ``sibling_name``, so they can be grouped and
labelled — and the tool calls in ``turn_accounting`` carry timestamps, so a
genuine cross-session TIMELINE can be built even though history rows
themselves are not timestamped.

That timeline is the whole point.  A writer asking for a snippet and the
coder answering are two events in two different transcripts, and neither
file alone shows the exchange.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

SESSIONS = Path(__file__).resolve().parent.parent / ".jaato" / "sessions"

#: Calls that cross a session boundary.  Listed explicitly rather than
#: inferred, so a new verb has to be added here deliberately.
HANDOFF_TOOLS = ("send_to_sibling", "list_siblings")


def _clean(text: str, workspace: Optional[str]) -> str:
    return text.replace(workspace, "<workspace>") if workspace else text


def _block(body: str, limit: int, workspace: Optional[str]) -> str:
    body = _clean((body or "").strip(), workspace)
    if len(body) > limit:
        body = body[:limit] + "\n… [truncated]"
    return f"```\n{body}\n```\n"


def _load(cascade: Optional[str]) -> List[dict]:
    """Every session of one cascade, oldest first.

    With no id, the most recent cascade that has more than one session —
    a single-session 'cascade' is not what this renderer is for.
    """
    docs = []
    for path in sorted(SESSIONS.glob("*.json")):
        try:
            docs.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    if cascade:
        return [d for d in docs if d.get("cascade_driver_id") == cascade]
    grouped: Dict[str, List[dict]] = {}
    for d in docs:
        cid = d.get("cascade_driver_id")
        if cid:
            grouped.setdefault(cid, []).append(d)
    if not grouped:
        return []
    # Prefer a cascade that actually RAN.  Ranking by session count alone
    # picks up the empty two-session cascades a failed probe leaves behind,
    # and renders a document whose every section says "0 model steps".
    def _weight(sessions):
        steps = sum(d.get("turn_count") or 0 for d in sessions)
        newest = max((d.get("created_at", "") for d in sessions), default="")
        return (steps, len(sessions), newest)

    return max(grouped.values(), key=_weight)


def _timeline(docs: List[dict]) -> List[str]:
    """Every cross-session call, in real time, across all siblings.

    Built from ``turn_accounting[*].function_calls``, whose records carry
    ``start_time``.  This is the only view in which one sibling's request
    and another's reply appear next to each other.
    """
    events = []
    for doc in docs:
        who = doc.get("sibling_name") or doc.get("session_id")
        for row in doc.get("turn_accounting") or []:
            for call in row.get("function_calls") or []:
                if not isinstance(call, dict):
                    continue
                if call.get("name") in HANDOFF_TOOLS:
                    events.append((call.get("start_time", ""), who,
                                   call.get("name"),
                                   call.get("duration_seconds", 0.0)))
    if not events:
        return ["## Handoffs", "",
                "No cross-session calls in this cascade — every sibling ran "
                "alone.", ""]
    events.sort()
    out = ["## Handoffs", "",
           "Cross-session calls in real time. This is the exchange itself; "
           "neither transcript below shows it alone.", "",
           "| time | sibling | call | seconds |", "|---|---|---|---|"]
    for when, who, name, secs in events:
        out.append(f"| {when[11:23]} | `{who}` | `{name}` | {secs:.1f} |")
    return out + [""]


def _session(doc: dict) -> List[str]:
    workspace = doc.get("workspace_path")
    who = doc.get("sibling_name") or "(unaddressed)"
    out = [f"## `{who}` — session `{doc.get('session_id')}`", "",
           f"Profile `{doc.get('profile_name')}`, agent "
           f"`{doc.get('agent_name')}`, {doc.get('turn_count')} model steps.",
           ""]
    turn = 0
    for message in doc.get("history") or []:
        role = message.get("role")
        for part in message.get("parts") or []:
            if role == "user":
                turn += 1
                out += [f"### {who} · turn {turn} — input", "",
                        _block(part.get("text", ""), 1200, workspace)]
            elif role == "model":
                text = (part.get("text") or "").strip()
                if text:
                    out += ["> " + _clean(text, workspace).replace("\n", "\n> "), ""]
                if part.get("name"):
                    args = _clean(json.dumps(part.get("args") or {}), workspace)
                    if len(args) > 500:
                        args = args[:500] + " …"
                    out += [f"**`{part['name']}`**", f"```json\n{args}\n```", ""]
            elif role == "tool":
                result = part.get("result")
                if not isinstance(result, str):
                    result = json.dumps(result)
                flag = " — ERROR" if part.get("is_error") else ""
                out += [f"<sub>→ `{part.get('name')}`{flag}</sub>",
                        _block(result, 700, workspace)]
    return out


def render(cascade: Optional[str] = None) -> str:
    docs = _load(cascade)
    if not docs:
        return "No cascade sessions found under .jaato/sessions/."
    docs.sort(key=lambda d: d.get("created_at", ""))
    cid = docs[0].get("cascade_driver_id") or "?"
    roles = ", ".join(f"`{d.get('sibling_name') or '?'}`" for d in docs)
    out = [f"# Cascade `{cid[:12]}`", "",
           f"{len(docs)} session(s): {roles}.", ""]
    out += _timeline(docs)
    for doc in docs:
        out += ["---", ""] + _session(doc)
    return "\n".join(out)


def main() -> int:
    print(render(sys.argv[1] if len(sys.argv) > 1 else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
