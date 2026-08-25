#!/usr/bin/env python3
"""Three sessions write and review one chapter of a Python guide.

    writer   owns the chapter markdown           ─┐ same cascade,
    coder    owns snippets/, only ever creates    ─┤ working at the
    reviewer reads both, returns a verdict        ─┘ same time

The point of the example is WHERE each mechanism belongs:

  siblings talk WITHIN a phase.  The writer needs a code example in the
  middle of a paragraph.  Routing that through the driver would mean
  ending its turn, reporting, being re-prompted and resuming — a whole
  turn round-trip to ask one question.  ``send_to_sibling`` is a tool
  call; the turn continues.

  the driver sequences BETWEEN phases.  It starts the write phase, ends
  it, runs the review, and stops.  Those handoffs happen exactly at turn
  boundaries, where a driver costs nothing and already holds the typed
  payload it needs.

  the driver is also the TERMINATOR, and that is not a style choice.  A
  participant cannot end the loop it is in: the writer cannot know when
  the coder should stop, and the coder cannot see the chapter.  Only
  something outside both can.

Run it:  python3 -m examples.chapter_cascade
"""
from __future__ import annotations

import asyncio
import os
import sys

from examples import common

CHAPTER = "chapter.md"

#: One ceiling for all three sessions, declared on the cascade — NOT in
#: the profiles.  A profile that declares its own ``budget_control`` is
#: accounted separately and skipped by the cascade pool, so three such
#: profiles would leave this ceiling watching nothing.
#: MEASURED, not guessed.  One turn costs ~5,700 prompt tokens with the
#: inherited instruction layer suppressed (it was ~30,000 before — see
#: any _base_*.yaml).  A run is roughly: writer 10-20 turns, coder a few,
#: reviewer a few, so ~40 turns of headroom.
#:
#: The first version set 120,000 by feel.  The writer alone exceeded it,
#: the pool hit zero, and the framework refused to spawn the reviewer —
#: correctly.  A ceiling you did not measure is not a budget, it is a
#: number that will stop your cascade somewhere you did not choose.
BUDGET = {"tokens": 400_000}

#: The reduced example runs one review round.  The reviewer may name a
#: next chapter; recording it is all that happens here.
MAX_REVISIONS = 1

WRITER_BRIEF = (
    f"Write {CHAPTER}: one short chapter introducing Python list "
    f"comprehensions for someone who already knows loops.\n\n"
    f"FIRST, before writing any prose, request your two code examples so "
    f"the coder can work while you draft. Call send_to_sibling twice, now:\n"
    f"  send_to_sibling(sibling_name='coder', message=\"snippet "
    f"id=square-loop: squaring a list with a for loop\")\n"
    f"  send_to_sibling(sibling_name='coder', message=\"snippet "
    f"id=square-comprehension: the same thing as a list comprehension\")\n"
    f"Call the tools; do not describe calling them. THEN write the chapter, "
    f"leaving <!-- snippet: <id> --> where each example belongs."
)
CODER_BRIEF = (
    "Stand by for snippet requests from the writer. They arrive as "
    "'snippet id=<id>: <what it must demonstrate>'. For each one, write "
    "snippets/<id>.py and reply to the writer with send_to_sibling naming "
    "the id and the path. Do not stop until you are told to."
)
# No STOP_CODER: the coder cannot report a stop, having no completion
# schema and therefore no signal_completion.  The driver ends it.


def _write_transcript(cascade: str) -> None:
    """Render what the siblings actually did, and say where it is.

    The driver's stdout reports OUTCOMES.  The transcript shows the
    exchange that produced them — including the writer's mid-turn
    requests and the coder's replies, which are two events in two
    different sessions and appear together nowhere else.
    """
    from examples.render_cascade import render

    out = os.path.join(common.REPO, "cascade-transcript.md")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(render(cascade))
    except OSError as exc:
        print(f"(could not write the transcript: {exc})", flush=True)
        return
    print(f"transcript: {out}", flush=True)


async def run() -> int:
    client = common.new_client()
    if not await client.connect(timeout=60.0):
        print("no daemon on", common.SOCKET, "— see examples/README.md",
              flush=True)
        return 1

    # The writer<->coder channel is framework step 5.  Say so plainly and
    # up front rather than failing somewhere in the middle of a turn.
    if not await common.tool_available(client, "send_to_sibling"):
        # Refuse, rather than run a reduced version that looks like it
        # worked.  An earlier draft ran the writer alone when the verb was
        # missing: it produced a plausible chapter AND the snippets, because
        # with no coder to ask, the writer wrote them itself — silently
        # breaking the one technical claim this example makes, that the two
        # sessions never touch each other's files.  A demo that appears to
        # succeed while demonstrating nothing is worse than one that stops.
        print("subagent.send_to_sibling is not in this framework build.\n"
              "The writer reaches the coder mid-turn; without that verb this\n"
              "example has nothing to show, so it stops here rather than\n"
              "producing a chapter that proves nothing.", flush=True)
        await client.disconnect()
        return 2
    print("sibling channel: available", flush=True)

    cascade = common.new_cascade_id()
    await client.cascade_budget_set(cascade, limits=BUDGET)
    print(f"cascade {cascade[:8]} — ceiling {BUDGET}", flush=True)

    try:
        return await _cascade(client, cascade)
    finally:
        # ALWAYS — a run that failed is the one whose transcript is worth
        # reading, and an earlier version wrote it only on the two success
        # paths.  The driver prints outcomes; the transcript shows the
        # exchange that produced them.
        _write_transcript(cascade)
        await client.disconnect()


async def _cascade(client, cascade: str) -> int:

    # ---- phase 1: write ---------------------------------------------------
    # Both sessions live at once.  Neither writes to the other's files, so
    # "working on the same chapter" needs no locking: the coder only ever
    # CREATES snippets/<id>.py and the writer owns the markdown.
    coder_id = await common.start_session(client, sibling_name="coder",
                                          profile="coder", agent="coder",
                                          cascade_id=cascade)
    await client.send_message(CODER_BRIEF)
    writer_id = await common.start_session(client, sibling_name="writer",
                                           profile="writer", agent="writer",
                                           cascade_id=cascade)
    await client.send_message(WRITER_BRIEF)
    print("write phase: writer and coder both live", flush=True)

    # Generous, because the writer legitimately ends turns WITHOUT
    # completing while it waits for the coder's reply to wake it.  Silence
    # here is the normal state of a session that is waiting, not a stall.
    written = await common.await_completion(client, writer_id, timeout=900.0)
    if not written:
        print("the writer did not finish in time", flush=True)
        return 1
    print(f"writer: {written.get('outcome')} — {written.get('summary','')}",
          flush=True)

    # The driver ends the write phase.  The writer reported; it does not
    # get to stop its sibling.
    # End the write phase by UNLOADING the coder, not deleting it.
    #
    # Attaching away detaches this client and unloads the session it
    # left: the coder stops consuming, its transcript is saved, and it
    # remains listed as `cold`.  delete_session would also stop it — and
    # DESTROY the record of what it did.  The first version deleted, and
    # when the chapter came out with no snippets there was no coder
    # transcript left to explain why.  A driver that erases a
    # participant erases its own evidence.
    # TWO steps, and both are needed.  attach_session unloads the session
    # the client LEAVES — so to unload the coder, the coder must be the
    # current session first.  Attaching straight to the writer (which was
    # already current, being created last) left nothing, and the coder's
    # transcript stayed a stale snapshot taken before it ever ran.
    await client.attach_session(coder_id)     # make it current...
    await client.attach_session(writer_id)    # ...then leave it: unload + save
    await asyncio.sleep(12)
    snippets_dir = os.path.join(common.REPO, "snippets")
    made = sorted(f for f in os.listdir(snippets_dir)
                  if f.endswith(".py")) if os.path.isdir(snippets_dir) else []
    print(f"coder: stopped by the driver, {len(made)} snippet(s)", flush=True)

    # ---- phase 2: review --------------------------------------------------
    for attempt in range(MAX_REVISIONS + 1):
        reviewer_id = await common.start_session(
            client, sibling_name=f"reviewer-{attempt}",
            profile="reviewer", agent="reviewer", cascade_id=cascade)
        await client.send_message(
            f"Review the chapter at {written.get('chapter_path', CHAPTER)}.")
        verdict = await common.await_completion(client, reviewer_id,
                                                timeout=420.0)
        if not verdict:
            # Distinguish "slow" from "never started".  A spawn refused
            # for budget produces silence that looks exactly like a slow
            # turn, and an earlier version waited fifteen minutes on a
            # session the framework had already declined to create.
            await client.cascade_budget_get(cascade)
            await asyncio.sleep(3)
            print("the reviewer produced no verdict — check the cascade "
                  "budget above; a refused spawn is silent from here",
                  flush=True)
            return 1
        print(f"reviewer: {verdict.get('verdict')} "
              f"({len(verdict.get('reasons', []))} reason(s))")

        if verdict.get("verdict") == "accept":
            if verdict.get("next_chapter"):
                print(f"  suggested next chapter: {verdict['next_chapter']}",
                      flush=True)
            print("accepted — cascade complete", flush=True)
            return 0
        if attempt == MAX_REVISIONS:
            break

        # One revision round: the reviewer's reasons go back to the writer.
        # They are replayed VERBATIM because the writer cannot see the
        # reviewer's context — a handoff must carry its own meaning.
        reasons = "\n".join(f"- {r}" for r in verdict.get("reasons", []))
        await client.send_message(
            f"The reviewer asked for changes to "
            f"{written.get('chapter_path', CHAPTER)}:\n{reasons}\n"
            f"Revise the chapter and signal ready_for_review again.")
        # Generous, because the writer legitimately ends turns WITHOUT
    # completing while it waits for the coder's reply to wake it.  Silence
    # here is the normal state of a session that is waiting, not a stall.
    written = await common.await_completion(client, writer_id, timeout=900.0) or written

    print("still needs revision after the review round — stopping", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
