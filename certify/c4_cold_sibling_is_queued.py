"""C4 — a cold sibling is queued, not woken; and a DEAD sibling is neither.

The precedent runs the other way, which is why this needs certifying.
``send_to_subagent`` branches on the target's state: busy → queue at
PARENT priority; idle → dispatch to a background thread, i.e. WAKE it.
Right for a subagent, which exists because its parent wanted work done
now.  A sibling is not owned by its sender, so waking one on message
arrival makes every sibling a cost centre any other sibling can start.

Two claims, and the second is the one that bites:

``C4a``  cold → queued, not woken, unless ``wake=True``.  Both
         directions, because a ``send_to_sibling`` that never wakes
         anything would pass a one-sided test with a decorative flag.
``C4b``  a TERMINATED sibling is distinguishable from a cold one.  They
         look identical from silence and their truths are opposite: a
         cold sibling can later wake and drain, a terminated one never
         will.  Queueing to a dead sibling is a black hole that reports
         success, so the two must never share a status.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional, Tuple, List

from certify import harness, surface
from certify.contract import (
    ACCEPTED, NO_SUCH_SIBLING, QUEUED, SEND_TO_SIBLING, STATUS_COLD,
)
from certify.verdict import BLOCKED, FAIL, PASS, Verdict

CLAIM_ID = "C4"
CLAIM_A = "a COLD sibling is never woken; an IDLE one is accepted and runs"
CLAIM_B = "a DELETED sibling is distinguishable from a cold one"

REVERT_A = (
    "make a cold sibling dispatch on delivery (as send_to_subagent's idle "
    "branch does).  The cold probe must then fail by observing a "
    "SessionWokenEvent it was promised not to see.\n"
    "Second reversion: make an IDLE sibling report `queued` and take no "
    "turn.  The idle probe must fail — otherwise this claim would pass on "
    "a send_to_sibling that never reaches anybody, which is the shape the "
    "old version of this claim actually had: it asserted a `wake` flag "
    "that the shipped design does not have, so it could only ever have "
    "been wrong about something that does not exist."
)
REVERT_B = (
    "make send_to_sibling return `queued` for a DELETED sibling (the "
    "'harmless' unification — neither is draining right now).  C4b must "
    "fail.  That collapse is the black hole: the sender is told its "
    "message waits for a reader that will never come.  Second reversion: "
    "drop cold rows from the roster, which makes cold and deleted "
    "indistinguishable from the other side too."
)

QUIET_WINDOW_S = 30.0

#: There is no `wake` argument.  Cold is cold.
COLD_SEND = ("Call send_to_sibling now with sibling_name='resting' and "
             "message='hello'. Call the tool; do not describe it. Report the "
             "status you get back.")
IDLE_SEND = ("Call send_to_sibling now with sibling_name='awake' and "
             "message='hello'. Call the tool; do not describe it. Report the "
             "status you get back.")
#: 'gone' is the address that WAS real and whose session was deleted —
#: the case C4b is about.  An earlier version named 'sibling-gone', which
#: had never existed at all, so it tested the never-existed path while
#: claiming to test the deleted one.  Both matter; they are not the same
#: fact and the claim must say which it exercised.
DEAD_SEND = ("Call send_to_sibling now with sibling_name='gone' and "
             "message='hello'. Call the tool; do not describe it. Report the "
             "status you get back.")
ABSENT_SEND = "Send one short greeting to the sibling named 'nobody-here'."


def _wire(ev) -> str:
    return getattr(getattr(ev, "type", None), "value", None) or getattr(ev, "type", "")


def _woken(log: harness.EventLog, session_id: str) -> bool:
    return any(getattr(ev, "session_id", None) == session_id
               for ev in log.of_type("session.woken"))


def _receipts_for(cid: str, target: str) -> List[str]:
    """Receipts for sends addressed to ``target``, by NAME not by position.

    Index-window slicing (snapshot the count, send, take the tail) looked
    obvious and was wrong: a receipt reaches disk only after the model's
    turn, so the next phase's snapshot picks up the PREVIOUS phase's
    receipt.  The run that exposed it put the COLD send's receipt in the
    idle slot and the IDLE send's in the deleted slot — and the deleted
    check then read `accepted` and declared a black hole that did not
    exist.

    The receipt names the sibling it was addressed to.  Correlating on
    that is immune to lag, ordering and retries, and it removes the whole
    class rather than tuning the window.
    """
    out: List[str] = []
    for call in harness.tool_calls_from_disk(cid, SEND_TO_SIBLING):
        body = call.get("response")
        text = str(body)
        named = isinstance(body, dict) and body.get("sibling_name") == target
        if named or f"'{target}'" in text or f'"{target}"' in text:
            out.append(text)
    return out


def _tool_results(cid: str, tool: str) -> List[Any]:
    """Receipts for ``tool`` across the cascade, read from disk.

    Two earlier versions of this failed for different reasons, both of
    which reported "no receipt observed" while the tool had run:
    reading ``ev.output`` (a field that does not exist), then reading
    ``request_history`` AFTER the sending session had finished — that
    endpoint serves the client's CURRENT session and a finished one
    cannot be asked.  The transcripts on disk have neither problem.
    """
    return [str(c["response"]) for c in
            harness.tool_calls_from_disk(cid, tool)]


def _contains(result: Any, needle: str) -> bool:
    if isinstance(result, dict):
        return needle in str(result.get("status", "")) or needle in str(result)
    return isinstance(result, str) and needle in result


async def _run(note) -> tuple:
    client = harness.new_client()
    if not await client.connect(timeout=120.0):
        raise harness.PreconditionUnmet("daemon did not start; run the doctor")

    cid = harness.new_cascade_id()
    log = harness.EventLog()

    async def _observe() -> None:
        async for ev in client.cascade_events(cid, event_types=None,
                                              role="observer"):
            log.record(ev)

    observer = asyncio.create_task(_observe())
    try:
        # Three subjects, each in a state the claim needs:
        #   resting — made COLD deliberately, see below
        #   awake   — created and prompted, then settles: IDLE (loaded)
        #   gone    — created and DELETED
        resting = await harness.create_sibling(client, cid, "resting",
                                               profile="sibling-b")
        gone = await harness.create_sibling(client, cid, "gone",
                                            profile="sibling-b")
        await client.delete_session(gone.session_id)
        awake = await harness.create_sibling(client, cid, "awake",
                                             profile="sibling-b")
        await client.send_message("Reply with the single word: ready")
        await asyncio.sleep(30)
        sender = await harness.create_sibling(client, cid, "sender",
                                              profile="sibling-a",
                                              agent="sibling-sender")

        # COLD IS REACHED BY ATTACHING AWAY, not by waiting.
        # attach_session detaches the client from its CURRENT session and
        # unloads the one it left, when no client remains attached and the
        # model is not running: saved to disk, dropped from memory.
        #
        # An earlier version created a session, never prompted it, and
        # called that cold.  It was idle-LOADED — because THIS DRIVER WAS
        # STILL ATTACHED TO IT — and the framework said so by answering
        # `accepted`.  The receipt was right and the test was wrong.
        await client.attach_session(resting.session_id)   # become its client
        await client.attach_session(sender.session_id)    # ...then leave it
        await asyncio.sleep(10)   # the unload is deferred to a background thread
        note(f"cold={resting.session_id} idle={awake.session_id} "
             f"deleted={gone.session_id} (cold by attach-away)")

        # 1. COLD: must NOT be woken, and must say so.
        await client.send_message(COLD_SEND)
        await asyncio.sleep(QUIET_WINDOW_S)
        cold_woke = _woken(log, resting.session_id)
        note(f"cold: woken={cold_woke}")

        # 2. IDLE: must be accepted, and a turn must start on ITS session.
        await client.inject_prompt(IDLE_SEND, source_type="user",
                                   source_id="certify-c4")
        await asyncio.sleep(QUIET_WINDOW_S)
        idle_ran = awake.session_id in log.sessions_that_ran()
        note(f"idle: took_a_turn={idle_ran}")

        # 3. DELETED: must be no_such_sibling, never queued/accepted.
        await client.inject_prompt(DEAD_SEND, source_type="user",
                                   source_id="certify-c4")
        await asyncio.sleep(QUIET_WINDOW_S)
        # Force the SENDER's transcript to disk before reading it.
        # Transcripts are written on SAVE, not continuously, so every read
        # taken while the sender was still loaded came back empty — which
        # is indistinguishable from "it never sent".  Attaching away
        # unloads and saves it, the same mechanism used to make a sibling
        # cold, so the evidence exists before it is asked for.
        await client.attach_session(awake.session_id)

        # POLL for the receipts rather than sleeping a guessed interval.
        # A fixed wait read the transcript with two of three receipts in
        # it and reported the third as "never sent" — while the sender's
        # own history showed all three calls.  A missing receipt and a
        # not-yet-saved one are the same empty list, so the wait has to
        # be driven by the evidence appearing, not by a number I picked.
        expected = ("resting", "awake", "gone")
        for _ in range(40):
            await asyncio.sleep(5)
            if all(_receipts_for(cid, t) for t in expected):
                break

        cold_receipts = _receipts_for(cid, "resting")
        idle_receipts = _receipts_for(cid, "awake")
        dead_receipts = _receipts_for(cid, "gone")
        rosters = _tool_results(cid, "list_siblings")
        note(f"cold={cold_receipts} idle={idle_receipts} "
             f"deleted={dead_receipts}")
    finally:
        observer.cancel()
        await client.disconnect()

    return (cold_woke, cold_receipts, idle_receipts, idle_ran,
            dead_receipts, rosters)


def certify() -> List[Verdict]:
    gate_a = surface.gate(CLAIM_ID + "a", CLAIM_A, REVERT_A)
    gate_b = surface.gate(CLAIM_ID + "b", CLAIM_B, REVERT_B)
    if gate_a is not None or gate_b is not None:
        return [g for g in (gate_a, gate_b) if g is not None]

    a = Verdict(CLAIM_ID + "a", CLAIM_A, FAIL, revert=REVERT_A)
    b = Verdict(CLAIM_ID + "b", CLAIM_B, FAIL, revert=REVERT_B)
    try:
        harness.check_preconditions()
        (cold_woke, cold_receipts, idle_receipts, idle_ran,
         dead_receipts, rosters) = asyncio.run(_run(a.note))
    except harness.PreconditionUnmet as exc:
        for v in (a, b):
            v.state, v.detail = BLOCKED, f"precondition unmet: {exc}"
        return [a, b]

    if cold_woke:
        a.detail = ("a COLD sibling was woken by a message — any sibling "
                    "could start any other, which D1 deliberately ruled out")
    elif not any(_contains(r, STATUS_COLD) for r in cold_receipts):
        a.detail = (f"the cold sibling was not woken, but the receipt did not "
                    f"say {STATUS_COLD!r}: {cold_receipts} — silence and a "
                    f"stated refusal are different facts")
    elif not any(_contains(r, ACCEPTED) for r in idle_receipts):
        a.detail = (f"an IDLE sibling was not {ACCEPTED!r}: {idle_receipts} — "
                    f"without this the cold result proves nothing, because a "
                    f"send_to_sibling that never reaches anybody would pass")
    elif not idle_ran:
        a.detail = (f"receipt said {ACCEPTED!r} but no turn started on the "
                    f"idle sibling's session — accepted must mean a turn "
                    f"began, not merely that the message was taken")
    else:
        a.state = PASS
        a.detail = (f"cold -> {STATUS_COLD!r} and no wake; idle -> "
                    f"{ACCEPTED!r} and a turn started on its own session")

    # Two asymmetric facts, and BOTH are needed: cold must be present and
    # addressable, deleted must be absent and refused.  Asserting either
    # alone leaves the other free to collapse into it.
    if not dead_receipts:
        b.detail = "no receipt observed for the send to a deleted sibling"
    elif any(_contains(r, QUEUED) or _contains(r, ACCEPTED)
             for r in dead_receipts):
        b.detail = (f"send_to_sibling reported {QUEUED!r}/{ACCEPTED!r} for a "
                    f"DELETED sibling — a black hole that reports success; "
                    f"the sender is told its message reached a reader that "
                    f"will never come: {dead_receipts}")
    elif not any(_contains(r, NO_SUCH_SIBLING) for r in dead_receipts):
        b.detail = (f"receipt was neither {NO_SUCH_SIBLING!r} nor "
                    f"{QUEUED!r}: {dead_receipts}")
    elif rosters and not any(_contains(r, STATUS_COLD) for r in rosters):
        b.detail = (f"the COLD sibling is missing from the roster (no "
                    f"{STATUS_COLD!r} row) — dropping cold rows makes cold "
                    f"and deleted identical from the roster side, so "
                    f"{NO_SUCH_SIBLING!r} becomes a race rather than a fact")
    else:
        b.state = PASS
        b.detail = (f"cold sibling stays listed as {STATUS_COLD!r}; a deleted "
                    f"one is absent and refused with {NO_SUCH_SIBLING!r}")
    return [a, b]
