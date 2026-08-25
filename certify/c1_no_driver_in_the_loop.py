"""C1 — a stage reaches a sibling stage without the driver in the loop.

The actual gap.  Today every agent-to-agent message routes through the
parent, so "sibling coordination" is a driver relay wearing a costume.

The certification is structural, not observational.  After a single
kickoff the driver is put on a :class:`DriverLeash` that permits
observation and nothing else, so a relay does not merely fail an
assertion at the end — it raises at the call site.  An example that only
checked "did a message arrive" would go green on a relay, which is the
exact vacuous pass this repository exists to avoid.
"""
from __future__ import annotations

import asyncio
from typing import Any, List

from certify import harness, surface
from certify.contract import ACCEPTED, RECEIPT_STATUSES, SEND_TO_SIBLING
from certify.facade_guard import DriverLeash, LeashBroken
from certify.verdict import FAIL, PASS, Verdict

CLAIM_ID = "C1"
CLAIM = "a stage reaches a sibling stage without the driver in the loop"
REVERT = (
    "replace the sibling's send_to_sibling call with a driver relay (driver "
    "receives from sibling-a, calls send_message on sibling-b).  The leash must "
    "raise LeashBroken.  If it goes green, the certification is measuring "
    "'a message arrived' instead of 'the driver was not the courier'.  "
    "Second reversion: make send_to_sibling await the sibling's turn before "
    "returning.  The receipt-ordering check must fail — see below."
)

#: `accepted` means HANDED TO THE QUEUE, never "the sibling processed it".
#: The distinction cannot be read off the word, so it is read off the
#: CLOCK: a receipt that only ever arrives after the target's turn has
#: completed is a blocking semantic wearing a receipt's clothes.  Both
#: events cross one observer stream, so their arrival order is a real
#: happens-before witness rather than an inference.
def _receipt_precedes_turn(log: harness.EventLog, sibling_b) -> tuple:
    """(ok, note) — did the send_to_sibling receipt land before sibling-b ran?"""
    receipts = [i for i, ev in enumerate(log.events)
                if getattr(ev, "tool_name", None) == SEND_TO_SIBLING
                and _wire(ev) == "tool.call_end"]
    turns = [i for i, ev in enumerate(log.events)
             if _wire(ev) == "turn.completed"
             and getattr(ev, "session_id", None) == sibling_b.session_id]
    if not receipts:
        return False, f"no {SEND_TO_SIBLING} result observed on the bus"
    if not turns:
        return False, "sibling-b never completed a turn; ordering unknowable"
    if receipts[0] > turns[0]:
        return False, (
            f"the {SEND_TO_SIBLING} receipt arrived only AFTER sibling-b's turn "
            f"completed — 'accepted' has come to mean 'processed', which "
            f"is a blocking call with a receipt's name on it"
        )
    return True, "receipt landed before sibling-b's turn — handed to the queue"


def _wire(ev) -> str:
    return getattr(getattr(ev, "type", None), "value", None) or getattr(ev, "type", "")


def _receipt_status(bodies: List[str]) -> object:
    """The receipt, read from the result BODY.

    An earlier version read ``ev.output`` / ``ev.result`` off tool
    events.  Neither field exists, so it always returned None — which
    C1 would have read as "no status declared" and passed over.  The
    body arrives via history; see ``harness.tool_bodies``.
    """
    for body in bodies:
        for status in RECEIPT_STATUSES:
            if status in body:
                return status
    return None

#: sibling-a is told to hand work to sibling-b itself.  The prompt names the
#: verb because the point is the verb existing, not the model guessing.
#: The behaviour lives in the PERSONA, not in this prompt.  Asking a bare
#: profile nicely in a kickoff is how C1 failed its first live run: the
#: model simply did not reach for the verb, and the claim reported "the
#: sibling never took a turn" — which reads like a framework failure and
#: was an authoring one.
KICKOFF = "Send your message to partner now, using send_to_sibling."


async def _run(report_note) -> tuple:
    client = harness.new_client()
    if not await client.connect(timeout=120.0):
        raise harness.PreconditionUnmet("daemon did not start; run the doctor")

    cid = harness.new_cascade_id()
    log = harness.EventLog()

    # partner FIRST: it must exist and be addressable before the
    # initiator sends to it, or the receipt is no_such_sibling and the
    # claim fails on ordering rather than on its subject.
    sibling_b = await harness.create_sibling(client, cid, "partner",
                                             profile="sibling-b",
                                             agent="sibling-partner")
    sibling_a = await harness.create_sibling(client, cid, "initiator",
                                             profile="sibling-a",
                                             agent="sibling-initiator")
    report_note(f"cascade {cid}: sibling-a={sibling_a.session_id} sibling-b={sibling_b.session_id}")

    # The ONE outbound call the driver is allowed to make all run.
    await client.send_message(KICKOFF)

    # From here the driver may watch and nothing else.  allow=() keeps
    # only DriverLeash.OBSERVER_METHODS.
    leashed = DriverLeash(client, allow=())

    observer = asyncio.create_task(_observe(leashed, cid, log))
    bodies: List[str] = []
    try:
        await asyncio.wait_for(_until_peer_b_ran(log, sibling_b), timeout=180.0)
        # Fetch BEFORE disconnecting: the receipt body lives in session
        # history, and history is served for the client's live session.
        bodies = [str(c["response"]) for c in
                  harness.tool_calls_from_disk(cid, SEND_TO_SIBLING)]
    finally:
        observer.cancel()
        await client.disconnect()

    return sibling_a, sibling_b, log, bodies


async def _observe(leashed: Any, cid: str, log: harness.EventLog) -> None:
    async for ev in leashed.cascade_events(cid, event_types=None, role="observer"):
        log.record(ev)


async def _until_peer_b_ran(log: harness.EventLog, sibling_b) -> None:
    while sibling_b.session_id not in log.sessions_that_ran():
        await asyncio.sleep(0.25)


def certify() -> Verdict:
    blocked = surface.gate(CLAIM_ID, CLAIM, REVERT)
    if blocked is not None:
        return blocked

    v = Verdict(claim_id=CLAIM_ID, claim=CLAIM, state=FAIL, revert=REVERT)
    try:
        harness.check_preconditions()
        sibling_a, sibling_b, log, bodies = asyncio.run(_run(v.note))
    except harness.PreconditionUnmet as exc:
        from certify.verdict import BLOCKED
        v.state, v.detail = BLOCKED, f"precondition unmet: {exc}"
        return v
    except LeashBroken as exc:
        v.detail = f"the driver was in the loop after all — {exc}"
        return v
    except asyncio.TimeoutError:
        v.detail = ("sibling-b never took a turn within the window — either "
                    "sibling-a never called send_to_sibling, or the receipt "
                    "was `queued`/`sibling_cold` and no turn followed. "
                    "Neither `accepted` nor `queued` claims the peer READ "
                    "the message, so silence here is not evidence the "
                    "delivery failed.")
        return v

    ran = log.sessions_that_ran()
    if sibling_b.session_id not in ran:
        v.detail = "sibling-b never took a turn; no sibling traffic occurred"
        return v

    status = _receipt_status(bodies)
    if status is not None and status not in RECEIPT_STATUSES:
        v.detail = (f"send_to_sibling returned an undeclared status {status!r}; "
                    f"the receipt contract is {sorted(RECEIPT_STATUSES)}")
        return v

    ordered, order_note = _receipt_precedes_turn(log, sibling_b)
    v.note(order_note)
    if not ordered:
        v.detail = "the receipt contract did not hold"
        return v

    v.state = PASS
    v.detail = "sibling-b ran on sibling-a's message with the driver leashed"
    v.note("driver outbound calls after kickoff: 0 (leash held)")
    v.note(f"receipt status: {status!r}; sessions that ran: {len(ran)}")
    return v
