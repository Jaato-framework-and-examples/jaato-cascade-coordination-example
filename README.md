# jaato cascade coordination — examples

Two sessions in one cascade, talking to each other directly.

The framework calls them **siblings**: sessions sharing one
`cascade_driver_id`, each addressable by a name, able to reach one
another without the driver relaying. This repository shows what that is
for, how to build it with the SDK facade, and — in [`certify/`](certify/)
— how we know it behaves as designed.

---

## The example: a chapter, written by three sessions

[`examples/chapter_cascade.py`](examples/chapter_cascade.py)

```
writer   owns the chapter markdown          ─┐  one cascade,
coder    owns snippets/, only ever creates  ─┤  one budget,
reviewer reads both, returns a verdict      ─┘  three sessions
```

The writer drafts prose. When a paragraph needs a code example it asks
the coder **mid-turn** and keeps writing. The coder answers with a path.
Neither ever edits the other's files, so "working on the same chapter"
needs no locking. When the writer signals `ready_for_review`, the driver
stops the coder and runs the reviewer, which accepts or sends one round
of revisions back.

### What it is really demonstrating

**Where each mechanism belongs.** That is the whole lesson, and it is
easy to get backwards.

| | belongs to | because |
|---|---|---|
| writer ↔ coder | **`send_to_sibling`** | the handoff lands *mid-turn* |
| phase → phase | **the driver** | those handoffs are at turn boundaries |
| stopping | **the driver** | a participant cannot end the loop it is in |

A driver relay is not a workaround — for a strict alternation it is
*better*: it already holds the typed completion payload, and it is the
only participant that sees both sides. What it cannot do is carry a
question the sender did not schedule. The writer needs a snippet in the
middle of a sentence; routing that through the driver means ending the
turn, reporting, being re-prompted and resuming — a whole turn
round-trip to ask one question. `send_to_sibling` is a tool call and the
turn continues.

So: **the driver is right when the handoff coincides with a turn
boundary; siblings are right when it does not.**

### Three things worth copying

**Messages must stand alone.** A sibling cannot see your context and you
cannot see its. The writer asks for "a snippet demonstrating X", never
"the thing I just described". This is the multi-session form of a lesson
`prime-agents-vs-jaato` proves for one session: state that must survive
belongs in the message, not in a history that garbage collection may
summarise away.

**No file is written by two sessions.** The coder only ever *creates*
`snippets/<id>.py`; the writer owns the chapter and includes them by id.
Concurrency stops being a problem you solve and becomes one you do not
have.

**The budget is declared on the cascade, not in the profiles.** One
`cascade_budget_set()` call covers all three sessions. A profile that
declares its own `budget_control` is accounted separately and skipped by
the cascade pool — so three such profiles would leave the ceiling
watching nothing. This is not a style preference; it is
[C3](certify/c3_own_books_budget_hole.py).

### Running it

```bash
python3 -m examples.chapter_cascade      # the cascade
python3 -m examples.render_cascade       # read what the siblings did
```

You need a daemon and a provider credential — see **Setup** below.

---

## Reading what happened

[`examples/render_cascade.py`](examples/render_cascade.py) renders every
session of one cascade as a single document: prose, tool calls with
their arguments, results — plus a **handoff timeline** built from the
timestamps on those calls.

The timeline is the part no single transcript can show. A writer asking
for a snippet and a coder answering are two events in two different
sessions, and neither file alone contains the exchange.

It earns its keep immediately. From one run:

```
| time         | sibling  | call            | seconds |
| 23:36:53.782 | `writer` | `list_siblings` |    17.6 |
```

and in the transcript beneath it, the writer reasoning about a failure
the driver's stdout never mentioned. Derived from `tools/dump_turns.py`
in `prime-agents-vs-jaato`, extended for multiple sessions.

---

## Setup

```bash
cp .env.example .env        # then fill in the credential
python3 -m jaato_sdk.doctor --workspace . --env-file .env

python -m server --ipc-socket /tmp/jc.sock \
                 --pid-file  /tmp/jc.pid --daemon
```

Its own socket and its own **pid file**: `IPCClient`'s autostart passes
`--ipc-socket` but never `--pid-file`, so a client pointed at a custom
socket with `auto_start=True` reads *another* daemon's pidfile, decides
it is stale, and unlinks it. Two short gotchas that cost real time —
`AF_UNIX` caps a socket path at 108 bytes and fails opaquely past it,
and `config_root` must point **at** `.jaato`, not at the workspace root,
because profile discovery scans `<config_root>/profiles` and finds
nothing *without an error* when it is wrong.

### Credentials

The profiles interpolate `${JAATO_OPENROUTER_API_KEY}` and say **nothing**
about where it comes from. Choosing the form is `.env`'s job, and `.env`
is gitignored:

```
plain value    JAATO_OPENROUTER_API_KEY=sk-or-...
secret URI     JAATO_OPENROUTER_API_KEY=pass://jaato/openrouter/api-key
```

The daemon resolves secret URIs in `.env` *values*. Those resolvers ship
out of tree, so a URI in a committed file would make this example depend
on a product the reader does not have.

---

## `certify/` — how we know

The examples show the surface working. [`certify/`](certify/) is the
adversarial half: nine claims about what sibling coordination must
guarantee, each written to **fail** when the guarantee is violated.

It is deliberately unlike the examples — a leash that forbids the driver
from relaying, three-state verdicts where "not built yet" is never
"passed", and thirteen guards that are each reverted on purpose and
required to complain. Nobody learns the facade from it; that is what the
examples are for.

It found eleven framework defects during this repository's first day,
and five bugs in itself. See [`certify/README.md`](certify/README.md).

---

## Vocabulary

The coordination surface says **sibling**, not peer. "Peer" already
means *another jaato server* in the published protocol, and sibling is
accurate here: every cid-bearing session is top-level, because subagents
are runtime-level sessions carrying no `cascade_driver_id` and appearing
in no roster.
