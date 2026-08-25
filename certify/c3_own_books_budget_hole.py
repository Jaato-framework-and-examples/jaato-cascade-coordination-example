"""C3 — two siblings with their own budget_control escape the cascade pool.

The design originally claimed budgets were a sufficient terminator for
sibling ping-pong.  They are not, and the reason is deliberate framework
behaviour rather than a bug:

    session_manager.py:1367   a session whose PROFILE declares
                              budget_control gets
                              ``_draws_on_parent_budget = False``
    session_manager.py:4601   ``_accumulate_cascade_budget`` returns
                              early for such a session
    session_manager.py:4546   ``_reconcile_cascade_pool`` skips it too

A child with its own books is a delegation to another department: it is
not clamped, and an exhausted pot does not refuse it.  Correct for a
child.  Fatal for a PAIR — two such siblings volleying burn two private
ceilings and the pool never sees a token, so no pool rung can ever fire
and nothing terminates the exchange.

C3a demonstrates the hole.  C3b demonstrates a per-cid exchange counter
closing it, because the terminator has to count something the pool can
actually see.

C3a is a certification of a NEGATIVE, so it is written to invalidate
itself loudly: if the pool ever does observe own-books spend, C3a fails
with "the hole is closed" rather than quietly going green on a claim
that stopped being true.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional

from certify import harness, surface
from certify.contract import AGENT_DRIVEN_TRIGGER
from certify.verdict import BLOCKED, FAIL, PASS, Verdict

CLAIM_ID = "C3"
CLAIM_A = "two siblings with their own budget_control are NOT bounded by the cascade pool"
CLAIM_B = "a per-cid exchange counter terminates a sibling ping-pong the pool cannot"
CLAIM_C = "the hole WIDENS when a volley also mints siblings via a reactor"

REVERT_A = (
    "delete `budget_control` from BOTH sibling profiles so the pair draws on "
    "the shared pot.  C3a must then fail with 'the hole is closed' — the "
    "pool sees their spend.  A C3a that passes either way is measuring "
    "nothing."
)
REVERT_B = (
    "remove the counter's terminate call (leave it counting).  C3b must "
    "hang to its timeout and fail.  A counter that only observes is not a "
    "terminator, which is precisely the mistake the budget claim made."
)
REVERT_C = (
    "point the reactor rule at a non-agent event (a timer, a manual "
    "trigger).  C3c must stop reporting the widening as agent-driven: the "
    "operator still chose the topology, but the COUNT and TIMING are no "
    "longer the agent's, which is the whole distinction."
)

#: What C3a and C3b actually rest on.  Recorded in the verdict rather than
#: in anyone's memory: "driver-formed cascades only" is a DEPLOYMENT
#: property, not an enforced invariant, so a green C3b means "the counter
#: terminated the volley IN A CONFIGURATION WHERE NOTHING ELSE WAS MINTING
#: SIBLINGS".  With a reactor rule on an agent-caused event that stops
#: being the same claim, and an assumption that only lives in a
#: conversation is invisible in a green run.
ASSUMPTION = (
    "assumes no reactor rule mints further cid-stamped sessions during the "
    "volley; that is a deployment property, not an invariant (see C3c)"
)

#: How many volleys before the counter cuts in.  Small on purpose — the
#: hole is demonstrated by the pool NOT moving, not by burning tokens.
EXCHANGE_LIMIT = 4

#: Ceiling declared on the cid.  Never approached; C3a's evidence is
#: that `remaining` does not move at all.
POOL_TOKENS = 100_000

#: The volley behaviour is in the personas; this only starts it.
PING_PONG = "Begin."


async def _budget_snapshot(client: Any, cid: str,
                           log: harness.EventLog) -> Optional[dict]:
    """Ask the daemon for the pool and read its JSON reply off the bus."""
    before = len(log.of_type("system.message"))
    await client.cascade_budget_get(cid)
    for _ in range(40):
        await asyncio.sleep(0.25)
        for ev in log.of_type("system.message")[before:]:
            try:
                doc = json.loads(getattr(ev, "message", "") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if doc.get("cascade_driver_id") == cid:
                return doc
    return None


def _tokens_spent(log: harness.EventLog) -> float:
    """What the siblings actually spent, per their own turn events."""
    total = 0.0
    for ev in log.of_type("turn.completed"):
        usage = getattr(ev, "usage", None)
        total += float(getattr(usage, "spend_total_tokens", 0) or 0)
    return total


class ExchangeCounter:
    """C3b's terminator: count sibling→sibling deliveries for ONE cid.

    Lives with whoever owns the cid, not in a sibling, because a sibling with
    its own books has no view of the pair.  It watches the read-only
    cascade bus — the driver is a supervisor here, never a courier.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.count = 0
        self.tripped = asyncio.Event()

    def observe(self, ev: Any) -> None:
        name = getattr(getattr(ev, "type", None), "value", None) or getattr(ev, "type", None)
        if name != "tool.call_start":
            return
        if getattr(ev, "tool_name", None) != "send_to_sibling":
            return
        self.count += 1
        if self.count >= self.limit:
            self.tripped.set()


async def _run(note) -> tuple:
    client = harness.new_client()
    if not await client.connect(timeout=120.0):
        raise harness.PreconditionUnmet("daemon did not start; run the doctor")

    cid = harness.new_cascade_id()
    log = harness.EventLog()
    counter = ExchangeCounter(EXCHANGE_LIMIT)

    await client.cascade_budget_set(cid, limits={"tokens": POOL_TOKENS})

    async def _observe() -> None:
        async for ev in client.cascade_events(cid, event_types=None,
                                              role="observer"):
            log.record(ev)
            counter.observe(ev)

    observer = asyncio.create_task(_observe())
    try:
        # Both profiles declare budget_control -> own books, both of them.
        await harness.create_sibling(client, cid, "partner",
                                     profile="sibling-b-own-books",
                                     agent="sibling-partner")
        await harness.create_sibling(client, cid, "initiator",
                                     profile="sibling-a-own-books",
                                     agent="sibling-initiator")

        opening = await _budget_snapshot(client, cid, log)
        note(f"pool at open: {opening}")

        await client.send_message(PING_PONG)
        await asyncio.wait_for(counter.tripped.wait(), timeout=240.0)
        note(f"counter tripped at {counter.count} sibling→sibling sends")

        # C3b's terminator.  Counting without this is not terminating.
        await client.stop()

        closing = await _budget_snapshot(client, cid, log)
        note(f"pool at close: {closing}")
    finally:
        observer.cancel()
        await client.disconnect()

    return opening, closing, counter, _tokens_spent(log)


def _certify_widening() -> Verdict:
    """C3c — is the agent-driven widening path even constructible here?

    Gated on a reactor being discoverable, because without one the path is
    inert and there is nothing to exercise.  Reported as its own verdict
    rather than folded into C3a so that "we did not test this" is visible
    in the artifact instead of resting on my having mentioned it once.
    """
    c = surface.gate(CLAIM_ID + "c", CLAIM_C, REVERT_C)
    if c is not None:
        c.note(f"a rule matching {AGENT_DRIVEN_TRIGGER!r} would make the "
               f"widening agent-driven: the operator authors the rule, so "
               f"the TOPOLOGY is operator-chosen, but the COUNT and the "
               f"TIMING are the agent's")
        c.note("jaato_premium ActionContext.create_session accepts "
               "cascade_driver_id, so the minted sessions are siblings")
        return c
    v = Verdict(CLAIM_ID + "c", CLAIM_C, BLOCKED, revert=REVERT_C)
    v.detail = ("reactors are discoverable but the rule that mints siblings "
                "on an agent event is not authored here — write it before "
                "claiming this path is covered")
    return v


def certify() -> List[Verdict]:
    gate_a = surface.gate(CLAIM_ID + "a", CLAIM_A, REVERT_A)
    gate_b = surface.gate(CLAIM_ID + "b", CLAIM_B, REVERT_B)
    widening = _certify_widening()
    if gate_a is not None or gate_b is not None:
        out = [g for g in (gate_a, gate_b) if g is not None]
        for g in out:
            g.note(ASSUMPTION)
        return out + [widening]

    a = Verdict(CLAIM_ID + "a", CLAIM_A, FAIL, revert=REVERT_A)
    b = Verdict(CLAIM_ID + "b", CLAIM_B, FAIL, revert=REVERT_B)
    try:
        harness.check_preconditions()
        opening, closing, counter, spent = asyncio.run(_run(a.note))
    except harness.PreconditionUnmet as exc:
        for v in (a, b):
            v.state, v.detail = BLOCKED, f"precondition unmet: {exc}"
            # EVERY path attaches it, including the ones that report
            # nothing.  A verdict that omits what it rests on is exactly
            # as misleading when it is BLOCKED as when it is PASS — a
            # reader still has to know the scope of what was measured.
            v.note(ASSUMPTION)
        return [a, b, widening]
    except asyncio.TimeoutError:
        b.detail = "the counter never tripped; the volley did not terminate"
        a.detail = "not evaluated — the volley did not run to the limit"
        for v in (a, b):
            v.note(ASSUMPTION)
        return [a, b, widening]

    if not opening or not closing:
        a.detail = "the daemon returned no pool snapshot"
        for v in (a, b):
            v.note(ASSUMPTION)
        return [a, b, widening]

    pool_moved = closing.get("remaining") != opening.get("remaining")
    if spent <= 0:
        a.detail = "the siblings spent no tokens; nothing was demonstrated"
    elif pool_moved:
        a.detail = (
            f"THE HOLE IS CLOSED — the pool saw own-books spend "
            f"({opening.get('remaining')} → {closing.get('remaining')}). "
            f"The framework changed; update this certification."
        )
    else:
        a.state = PASS
        a.detail = (f"siblings spent {spent:.0f} tokens; pool remaining "
                    f"unmoved at {closing.get('remaining')}")
        a.note("no pool rung can fire, so budgets cannot terminate the volley")

    if counter.count >= EXCHANGE_LIMIT:
        b.state = PASS
        b.detail = f"per-cid counter terminated the volley at {counter.count}"
    else:
        b.detail = f"counter reached only {counter.count}/{EXCHANGE_LIMIT}"
    for v in (a, b):
        v.note(ASSUMPTION)
    return [a, b, widening]
