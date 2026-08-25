# certify — the adversarial half

Nine claims about what sibling coordination must guarantee. Each is
written to **fail** when the guarantee is violated, which is a different
job from the examples next door: nobody learns the facade from this
directory.

Run it:

```bash
python3 -m certify.run_all        # guards, surface probe, nine claims
python3 -m certify.selftest       # the guard reversions alone
python3 -m certify.surface        # what the framework offers today
```

## Verdicts are three-valued

| state | meaning | exit |
|---|---|---|
| `PASS` | exercised and held | `0` |
| `FAIL` | exercised and **violated** | `1` |
| `BLOCKED` | **nothing was exercised** — surface absent | `2` |

CI must not read `2` as success. Collapsing "not built yet" into green
is the vacuous pass this suite exists to rule out — and the reason every
claim here was written *before* the API it certifies.

## The claims

| | claim |
|---|---|
| C1 | a stage reaches a sibling **without the driver in the loop** |
| C2 | a sibling **cannot approve** another sibling's permission request |
| C3a | two siblings with their own `budget_control` **escape the cascade pool** |
| C3b | a **per-cid exchange counter** terminates what budgets cannot |
| C3c | the hole **widens** when a volley also mints siblings via a reactor |
| C4a | a **cold** sibling is never woken; an **idle** one is accepted and runs |
| C4b | a **deleted** sibling is distinguishable from a cold one |
| C5a | a hostile **`sibling_name`** is refused at `session.new` |
| C5b | a sibling-authored **description** is carried as data |

Each carries a `revert:` naming what to break to watch it fail, and the
agreed surface lives in [`contract.py`](contract.py) — apart from the
checks, so a change to what was agreed is a diff rather than a quiet
edit inside a test.

## Guards, each watched failing

A guard nobody has seen fail is not a guard. Every guard here that can
be reverted mechanically **is**, by [`selftest.py`](selftest.py), which
requires it to complain.

```
R1  plant a `from shared…` import        → the facade guard must catch it
R2  relay through the driver             → the leash must raise
R3  observe through the leash            → must still pass through
R4  drive the surface gate both ways     → must close when absent AND open when present
R5  reorder a receipt after the peer's turn → the ordering check must reject it
R6  simulate a half-shipped surface      → one claim must open while another stays shut
R7  delete a claim's assumption note     → it must refuse to report without it
R8  commit a secret URI and a live key   → both caught, `.env` untouched
R9  age the daemon past the code         → the run must refuse to start
R10 declare a field the SDK lacks        → must fail at guard time
R11 read a field without declaring it    → must be caught by AST
R12 use a name the module never binds    → must be caught by symtable
R13 point HEAD at an unmerged commit     → the run must refuse to certify it
```

**They have caught more of this suite's bugs than the framework's.**
R10 and R11 alone found four probes reading event fields that do not
exist — including `PermissionResolvedEvent.decision`, where the real
field is `granted`. Reading a missing field returns `None`, and this
suite scored `None` as "not an approval", so **C2 would have passed
while a sibling was granting permissions**: a security claim whose
failure mode was silent success.

R9 caught four runs against a stale daemon. R13 exists because a run
once certified the tip of an *unmerged branch* — the framework checkout
is shared, and pinning a commit makes a verdict attributable but not
authoritative.

## The pattern underneath all of it

**Absent and empty share a representation.**

`getattr(x, k, None)`, `dict.get(k)`, `entry_points(group=g)`,
`hasattr(fr, 'response')`, a fixed sleep that reads a file before it is
written — each collapses "there is no such thing" into "the thing is
empty", with no error and nothing to notice. A claim of absence built on
one of them is unfalsifiable.

The rule that works: **witness the key, not the value.** An attribute →
grep for an *assignment*. A namespace → is anything in it? A field →
check it against the class. And it does not generalise from being
learned once; it needs a mechanical check per instance, which is what
R10–R13 are.

## What it found

Eleven framework defects in this repository's first day (jaato
#600–#610), among them: a tool error reported as success; `is_error`
lost to two 2-tuple conventions sharing one representation — which was
also reporting domain failures to the reliability plugin as *successes*;
every event unattributable to a session; the untrusted-content mark
dropped at three rebuild sites; `request_history` sending Python reprs
instead of data; and cold siblings reading as nonexistent because an
omitted `workspace_path` meant "the default directory" rather than "all
of them".
