"""Break each guard on purpose and require it to notice.

House rule: a guard nobody has watched fail is not a guard.  Two of the
framework side's own tests passed this week while the code they claimed
to protect was deleted, so "it went green" is not evidence.

Every guard in this repository that can be reverted mechanically is
reverted here, in-process, and its complaint is required.  A guard that
stays quiet under its own reversion fails THIS suite — which is the
only way the suite can claim its greens mean anything.
"""
from __future__ import annotations

import os
import tempfile
from typing import Callable, List, Tuple

from certify.facade_guard import (
    DriverLeash, LeashBroken, check_imports, check_publishable,
)
from certify.surface import REQUIRED_TOOLS, SurfaceReport
from certify.verdict import BLOCKED, FAIL, PASS, Report, Verdict


def _revert_import_guard() -> Tuple[bool, str]:
    """Plant a server-internal import; the guard must name it."""
    with tempfile.TemporaryDirectory() as tmp:
        planted = os.path.join(tmp, "reverted.py")
        with open(planted, "w", encoding="utf-8") as fh:
            # Precisely the import that let the in-process harness see
            # one tool path and miss the other.
            fh.write("from shared.message_queue import SourceType\n")
        violations = check_imports(tmp)
        if not violations:
            return False, ("check_imports stayed silent on a planted "
                           "'from shared.message_queue import ...'")
        return True, f"guard reported: {violations[0]}"


def _revert_leash() -> Tuple[bool, str]:
    """Relay through the driver; the leash must raise."""

    class _FakeClient:
        def __init__(self) -> None:
            self.sent: List[str] = []

        async def send_message(self, text: str) -> None:
            self.sent.append(text)

        def subscribe(self, *_a, **_kw):
            return lambda: None

    client = _FakeClient()
    leash = DriverLeash(client)
    try:
        leash.send_message("relay this to the other sibling")
    except LeashBroken as exc:
        if client.sent:
            return False, "leash raised but the call still reached the client"
        return True, f"leash raised: {str(exc).splitlines()[0][:80]}…"
    return False, "leash allowed 'send_message' while the driver was leashed"


def _revert_leash_allows_observation() -> Tuple[bool, str]:
    """The inverse: observation must NOT be blocked, or C1 cannot watch.

    A leash that forbids everything would 'pass' every no-relay claim
    vacuously by making the run impossible.
    """

    class _FakeClient:
        def subscribe(self, *_a, **_kw):
            return "unsub"

    leash = DriverLeash(_FakeClient())
    got = leash.subscribe("x")
    if got != "unsub":
        return False, "leash blocked read-only observation"
    if leash.calls != ["subscribe"]:
        return False, f"leash did not record the observation: {leash.calls}"
    return True, "observation passes through and is recorded"


class _Ev:
    """Minimal stand-in for a bus event — type, session, tool name."""

    def __init__(self, type_, session_id=None, tool_name=None):
        self.type = type_
        self.session_id = session_id
        self.tool_name = tool_name


def _revert_receipt_ordering() -> Tuple[bool, str]:
    """Make `accepted` mean "processed"; the ordering check must catch it.

    The word cannot tell you which meaning it has, so C1 reads it off the
    clock: a receipt that lands only AFTER the target's turn completed is
    a blocking call wearing a receipt's name.  Both orderings are driven
    here — a check that only ever saw the good order would pass on a
    framework that had quietly started blocking.
    """
    from certify.c1_no_driver_in_the_loop import _receipt_precedes_turn
    from certify.contract import SEND_TO_SIBLING
    from certify.harness import EventLog

    class _Peer:
        session_id = "sess-b"

    good = EventLog()
    good.record(_Ev("tool.call_end", "sess-a", SEND_TO_SIBLING))
    good.record(_Ev("turn.completed", "sess-b"))
    ok, note = _receipt_precedes_turn(good, _Peer())
    if not ok:
        return False, f"rejected a correctly-ordered receipt: {note}"

    # The reversion: send_to_sibling awaits the sibling's turn before returning.
    blocked = EventLog()
    blocked.record(_Ev("turn.completed", "sess-b"))
    blocked.record(_Ev("tool.call_end", "sess-a", SEND_TO_SIBLING))
    ok, note = _receipt_precedes_turn(blocked, _Peer())
    if ok:
        return False, ("accepted a receipt that arrived after the sibling's "
                       "turn — 'accepted' could come to mean 'processed' "
                       "and C1 would not notice")
    return True, "accepts handed-to-queue ordering, rejects blocking ordering"


def _revert_surface_gate_can_pass() -> Tuple[bool, str]:
    """The gate must OPEN when the verbs exist.

    A gate that can never open is the same disease as a guard that can
    never fail, and this one nearly had it: the probe originally asked
    for a `sibling` plugin that was never going to be built, so every claim
    would have sat at BLOCKED forever while the verbs worked fine.  The
    host plugin now predates the verbs, so "plugin present" proves
    nothing — only the verbs do.  Both directions are checked here
    because checking either alone would have missed that.
    """
    from certify.surface import REQUIRED_CAPABILITIES, UnknownClaim

    caps_absent = {c: False for c in REQUIRED_CAPABILITIES}
    caps_present = {c: True for c in REQUIRED_CAPABILITIES}
    absent = SurfaceReport(plugin_present=True,
                           tools_present={t: False for t in REQUIRED_TOOLS},
                           capabilities=caps_absent)
    present = SurfaceReport(plugin_present=True,
                            tools_present={t: True for t in REQUIRED_TOOLS},
                            capabilities=caps_present)

    # An id nothing declares must RAISE, not report "no gaps" — that
    # would open the gate for a claim whose surface is missing.
    try:
        absent.missing_for("C99-nonexistent")
    except UnknownClaim:
        pass
    else:
        return False, ("an undeclared claim id reported no gap; its gate "
                       "would open unconditionally")

    for claim in ("C1", "C2", "C3a", "C3b", "C4a", "C4b", "C5a", "C5b"):
        if not absent.missing_for(claim):
            return False, f"{claim} reported no gap while both verbs were absent"
        if present.missing_for(claim):
            return False, (f"{claim} still reports a gap with both verbs "
                           f"present: {present.missing_for(claim)} — the gate "
                           f"cannot open and every claim stays BLOCKED forever")
    return True, "gate closes with the verbs absent and OPENS when they land"


def _revert_c5a_independence() -> Tuple[bool, str]:
    """C5a must unblock on `sibling_name` ALONE, before the verbs exist.

    `sibling_name` ships in framework step 3, `list_siblings` later.  If C5a
    were gated on the roster too, a claim that had become certifiable
    would sit BLOCKED behind an unrelated absence — the suite would
    under-report, which is quieter than over-reporting and just as
    wrong.  Driven here as the step-3 world: name field present, both
    verbs still missing.
    """
    from certify.surface import REQUIRED_CAPABILITIES, SurfaceReport

    step3 = SurfaceReport(
        plugin_present=True,
        tools_present={t: False for t in REQUIRED_TOOLS},   # verbs absent
        capabilities={c: True for c in REQUIRED_CAPABILITIES},  # sibling_name landed
    )
    if step3.missing_for("C5a"):
        return False, (f"C5a still blocked at step 3 on "
                       f"{step3.missing_for('C5a')} — it is gated on the "
                       f"roster it does not need")
    if not step3.missing_for("C5b"):
        return False, "C5b unblocked without list_siblings; the gate is too loose"
    return True, "C5a opens on sibling_name alone while C5b stays blocked"


def _revert_assumption_visibility() -> Tuple[bool, str]:
    """C3a/C3b must CARRY their assumption, in every state.

    "Driver-formed cascades only" is a deployment property, not an
    invariant.  A green C3b therefore means "the counter terminated the
    volley in a configuration where nothing else was minting siblings" —
    and an assumption that lives only in a conversation is invisible in a
    green run.  Reverted by deleting the note; this requires it back.
    """
    from certify import c3_own_books_budget_hole as c3
    from certify.c3_own_books_budget_hole import ASSUMPTION
    from certify import harness

    # Force the precondition path so the verdicts are produced WITHOUT a
    # live cascade.  Once send_to_sibling shipped, calling certify()
    # directly started spawning real sessions — a guard suite that costs
    # model turns stops being run, and a guard nobody runs is no guard.
    real = harness.check_preconditions
    harness.check_preconditions = lambda: (_ for _ in ()).throw(
        harness.PreconditionUnmet("selftest: not running live"))
    try:
        verdicts = {v.claim_id: v for v in c3.certify()}
    finally:
        harness.check_preconditions = real
    for cid in ("C3a", "C3b"):
        v = verdicts.get(cid)
        if v is None:
            return False, f"{cid} was not reported at all"
        if not any(ASSUMPTION in note for note in v.evidence):
            return False, (f"{cid} reported without its assumption attached; "
                           f"a reader cannot tell what the verdict rests on")
    if "C3c" not in verdicts:
        return False, ("C3c absent — the untested widening path would be "
                       "invisible rather than reported as untested")
    return True, "C3a/C3b carry the assumption; C3c reports the gap"


def _revert_publishability_guard() -> Tuple[bool, str]:
    """Commit a premium-only secret URI and a live key; both must be caught.

    Two hazards with one shape — a committed file promising something the
    reader's checkout cannot deliver:

    * a secret URI needs an out-of-tree resolver (jaato-premium).  A
      public checkout has none, so the reader gets a literal URI where a
      key should be.
    * a plaintext key is simply leaked.

    The profiles interpolate the VARIABLE and say nothing about its form;
    ``.env`` chooses that, and ``.env`` is gitignored.  Reverted here by
    planting both, and by confirming the guard does NOT fire on the
    gitignored file — a guard that flags .env would be untrainable noise.
    """
    import os
    scheme_uri = "pass" + ":" + "/" * 2 + "jaato/openrouter/api-key"
    live_key = "sk-" + "or-v1-" + "0" * 8
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "profile.yaml"), "w", encoding="utf-8") as fh:
            fh.write(f"    api_key: {scheme_uri}\n")
        # Prose may SHOW the URI form — a reader can see it is an example,
        # and nothing resolves it.  Flagging docs would push the next
        # author to stop documenting the safe option.
        with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(f"secret URI  JAATO_OPENROUTER_API_KEY={scheme_uri}\n")
        # ...but a KEY in prose is leaked exactly as thoroughly.
        with open(os.path.join(tmp, "leaky-doc.md"), "w", encoding="utf-8") as fh:
            fh.write(f"for example, {live_key}\n")
        with open(os.path.join(tmp, "leaked.yaml"), "w", encoding="utf-8") as fh:
            fh.write(f"    api_key: {live_key}\n")
        # The gitignored file may hold either; it never reaches the remote.
        with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as fh:
            fh.write(f"JAATO_OPENROUTER_API_KEY={scheme_uri}\n")

        found = check_publishable(tmp)
        paths = {os.path.basename(v.path) for v in found}
        if "profile.yaml" not in paths:
            return False, "a committed secret URI went unreported"
        if "leaked.yaml" not in paths:
            return False, "a committed live-looking key went unreported"
        if ".env" in paths:
            return False, (".env was flagged — it is gitignored and is the "
                           "one place the form is chosen; flagging it makes "
                           "the guard noise")
        if "README.md" in paths:
            return False, ("a secret URI in PROSE was flagged; nothing "
                           "resolves it and flagging it teaches the next "
                           "author to stop documenting the safe option")
        if "leaky-doc.md" not in paths:
            return False, ("a live-looking key in prose went unreported — "
                           "the key rule has no docs exemption")
    return True, (f"caught {len(found)} planted violation(s); left .env and "
                  f"prose URIs alone, still caught a key in prose")


def _revert_daemon_vintage() -> Tuple[bool, str]:
    """Age the daemon past the code; the precondition must refuse.

    This guards the only FALSE RESULT this suite has produced: C5a run
    against a daemon eight hours older than the feature reported a
    merged capability as missing, which would have been filed as a
    framework defect.

    Both directions, and the skip path too — an earlier version read
    ``ps -o lstart=``, whose month names are localised, so it returned
    None and skipped itself on the machine it was written for. A
    precondition that cannot fire is worse than none: it reads as
    coverage.
    """
    from certify import harness

    real_start, real_head = harness._daemon_start_time, harness._framework_head_time
    try:
        harness._framework_head_time = lambda: 2_000_000.0

        harness._daemon_start_time = lambda: 1_000_000.0      # older than code
        try:
            harness.check_daemon_is_newer_than_the_code()
        except harness.PreconditionUnmet:
            pass
        else:
            return False, ("a daemon older than the code was accepted — this "
                           "is exactly how a merged feature reads as missing")

        harness._daemon_start_time = lambda: 3_000_000.0      # newer
        try:
            harness.check_daemon_is_newer_than_the_code()
        except harness.PreconditionUnmet:
            return False, "a NEWER daemon was refused; the check is inverted"

        harness._daemon_start_time = lambda: None             # unknowable
        try:
            harness.check_daemon_is_newer_than_the_code()
        except harness.PreconditionUnmet:
            return False, ("an unknowable start time BLOCKED the run; this "
                           "guards one specific trap and must not become a "
                           "second way to fail for unrelated reasons")
    finally:
        harness._daemon_start_time, harness._framework_head_time = real_start, real_head

    # And the live reading must be a real number here, or the guard is
    # skipping itself on this machine while looking like coverage.
    if harness._daemon_start_time() is None:
        return False, ("the live daemon start time is unreadable, so the "
                       "check silently skips — decoration, not a guard")
    return True, "refuses an older daemon, accepts a newer one, skips only when unknowable"


def _revert_event_fields_exist() -> Tuple[bool, str]:
    """Every event field the probes read must EXIST on the SDK event.

    ``getattr(ev, "output", None)`` on a field that does not exist
    returns None, which reads exactly like "the tool produced nothing".
    Three probes shipped with that bug: two reported "no result
    observed" while the tool was being called and failing, and one would
    have read a missing receipt as "no status declared" and passed over
    it.  None of the three announced anything.

    So the declared field names are checked against the SDK's own event
    classes, and a name the SDK does not define fails HERE — at guard
    time, loudly — instead of at certification time, silently.
    """
    import jaato_sdk.events as sdk_events
    from certify.harness import EVENT_FIELDS

    missing_classes, bad_fields = [], []
    for cls_name, fields in EVENT_FIELDS.items():
        cls = getattr(sdk_events, cls_name, None)
        if cls is None:
            missing_classes.append(cls_name)
            continue
        known = set(getattr(cls, "model_fields", {}) or {})
        for f in fields:
            if f not in known:
                bad_fields.append(f"{cls_name}.{f}")
    if missing_classes:
        return False, f"event class(es) the SDK does not export: {missing_classes}"
    if bad_fields:
        return False, (f"field(s) the SDK event does not define: {bad_fields} "
                       f"— reading one returns None and looks like absence")

    # And the reversion: a plausible-but-absent name must be caught.
    cls = sdk_events.ToolCallEndEvent
    if "output" in set(getattr(cls, "model_fields", {}) or {}):
        return False, ("ToolCallEndEvent now HAS an `output` field — the "
                       "bug this guards is no longer possible; simplify")
    return True, (f"all {sum(len(v) for v in EVENT_FIELDS.values())} declared "
                  f"fields exist; 'output' correctly absent from call_end")


def _revert_undeclared_field_reads() -> Tuple[bool, str]:
    """Nothing may read an event field it did not declare.

    R10 proves the DECLARED names exist.  This proves nothing bypasses
    the declaration — otherwise a probe could read ``ev.output`` again
    tomorrow and R10 would still pass, because R10 never sees it.
    """
    import os
    from certify.facade_guard import check_event_field_reads
    from certify.harness import EVENT_FIELDS

    declared = frozenset(f for fields in EVENT_FIELDS.values() for f in fields)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    live = check_event_field_reads(root, declared)
    if live:
        return False, f"undeclared read(s) in the suite: {[str(v) for v in live]}"

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "regressed.py"), "w", encoding="utf-8") as fh:
            # The exact line that shipped in two probes and read as
            # "the tool produced nothing" on every run.
            fh.write('x = getattr(ev, "output", None)\n')
        planted = check_event_field_reads(tmp, declared)
        if not planted:
            return False, ('a planted getattr(ev, "output", None) went '
                           'unreported — the guard cannot see a regression')
    return True, "no undeclared reads; a planted one is caught"


def _revert_undefined_names() -> Tuple[bool, str]:
    """A name used in a function must resolve at module level.

    The bug this guards crashed the FIRST full live run of the suite: a
    helper used ``asyncio`` and the module never imported it.  Neither
    importing the module nor running the guards caught it, because the
    line only executes on a path that was unreachable while the claim was
    BLOCKED — so it sat undiscovered for exactly as long as the claim did.

    That is the family's worst variant: broken code inside a branch that
    cannot run yet, in a suite whose whole subject matter is claims that
    cannot run yet.
    """
    import os
    from certify.facade_guard import check_undefined_names

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    live = check_undefined_names(root)
    if live:
        return False, f"undefined name(s) in the suite: {[str(v) for v in live]}"

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "regressed.py"), "w", encoding="utf-8") as fh:
            fh.write("def helper():\n    return asyncio.sleep(1)\n")
        planted = check_undefined_names(tmp)
        if not planted:
            return False, ("a planted use of an unimported module went "
                           "unreported — the guard cannot see the bug that "
                           "crashed the first live run")
    return True, "no undefined names; a planted missing import is caught"


def _revert_mainline_check() -> Tuple[bool, str]:
    """Certifying an unmerged commit must be refused.

    Distinct from staleness, and invisible to R9: a run pinned the tip
    of an unmerged feature branch because this checkout is SHARED and
    another process checked that branch out in it.  HEAD really was that
    commit and the daemon really was newer, so every consistency check
    agreed — and the verdict was attributed to a commit that existed on
    no mainline.  Attributable is not the same as authoritative.

    Driven both ways, plus the no-remote case, which must SKIP: a
    checkout without origin/main is a legitimate configuration and must
    not become a second way to be blocked for unrelated reasons.
    """
    import subprocess
    from certify import harness

    real = subprocess.run
    calls = {"n": 0}

    def fake(args, **kw):
        # args: ["git", "-C", repo, <verb>, ...]
        verb = args[3] if len(args) > 3 else ""
        rc = 0
        if verb == "merge-base":
            rc = calls["outcome"]
        out = "feature/x" if verb == "rev-parse" and "--abbrev-ref" in args else "abc1234"
        return subprocess.CompletedProcess(args, rc, out, "")

    subprocess.run = fake
    try:
        calls["outcome"] = 1          # HEAD is NOT an ancestor
        try:
            harness.check_head_is_mainline()
        except harness.PreconditionUnmet:
            pass
        else:
            return False, ("an unmerged HEAD was accepted — the suite would "
                           "certify code that is on no mainline")

        calls["outcome"] = 0          # HEAD IS an ancestor
        try:
            harness.check_head_is_mainline()
        except harness.PreconditionUnmet:
            return False, "a mainline HEAD was refused; the check is inverted"
    finally:
        subprocess.run = real
    return True, "refuses an unmerged HEAD, accepts a mainline one"


def _revert_spec_drift() -> Tuple[bool, str]:
    """The public spec must match the contract the probes exercise.

    This suite checks the FRAMEWORK's behaviour rigorously and, until
    now, checked its own SPEC against nothing.  SURFACE.md shipped
    publicly documenting `wake=False`, `delivered`, and a roster shape
    with `role`/`owner` — a flag that was designed and declined, a status
    word deleted upstream, and two fields removed by agreement. Runtime
    was right the whole time; only the document a reader trusts first was
    wrong, so nothing failed and nothing could.
    """
    import os
    from certify.facade_guard import check_spec_matches_contract

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    live = check_spec_matches_contract(os.path.join(root, "SURFACE.md"))
    if live:
        return False, f"spec drift: {[str(d) for d in live]}"

    with tempfile.TemporaryDirectory() as tmp:
        planted = os.path.join(tmp, "SURFACE.md")
        with open(planted, "w", encoding="utf-8") as fh:
            fh.write("```\nlist_siblings() -> [...]\n"
                     "send_to_sibling(name, message, wake=False)"
                     " -> {delivered, status}\n```\n")
        if not check_spec_matches_contract(planted):
            return False, ("the exact signature this repository published "
                           "went unreported — the guard cannot see the drift "
                           "it exists to catch")
    # Second anchor, available since framework #615 published tool
    # parameters: the spec must also match what the FRAMEWORK declares.
    # Not a replacement — the contract is what the probes CALL, and the
    # two can disagree, which is the drift worth catching.
    from certify.facade_guard import check_spec_matches_framework
    live_fw = check_spec_matches_framework(os.path.join(root, "SURFACE.md"))
    if live_fw:
        return False, f"spec vs framework: {[str(d) for d in live_fw]}"

    return True, ("spec matches the contract AND the framework's published "
                  "parameters; the drift this repo published is caught")


#: (id, what is reverted, the reversion)
REVERSIONS: List[Tuple[str, str, Callable[[], Tuple[bool, str]]]] = [
    ("R1", "facade import guard (check_imports)", _revert_import_guard),
    ("R2", "driver leash forbids relays (DriverLeash)", _revert_leash),
    ("R3", "driver leash still permits observation", _revert_leash_allows_observation),
    ("R4", "surface gate opens when the verbs land", _revert_surface_gate_can_pass),
    ("R5", "'accepted' still means queued, not processed", _revert_receipt_ordering),
    ("R6", "C5a unblocks on sibling_name alone, before the verbs", _revert_c5a_independence),
    ("R7", "C3a/C3b carry their deployment assumption", _revert_assumption_visibility),
    ("R8", "publishability guard (no premium-only URI, no live key)", _revert_publishability_guard),
    ("R9", "refuse a daemon older than the code under test", _revert_daemon_vintage),
    ("R10", "every event field the probes read exists on the SDK event", _revert_event_fields_exist),
    ("R11", "nothing reads an event field it did not declare", _revert_undeclared_field_reads),
    ("R12", "no function uses a name the module never binds", _revert_undefined_names),
    ("R13", "refuse to certify a commit that is not on the mainline", _revert_mainline_check),
    ("R14", "the public spec matches the contract the probes exercise", _revert_spec_drift),
]


def run() -> Report:
    report = Report()
    for rid, what, revert in REVERSIONS:
        v = Verdict(
            claim_id=rid,
            claim=f"reverting {what} is detected",
            state=BLOCKED,
            revert="this IS the reversion",
        )
        try:
            noticed, note = revert()
        except Exception as exc:  # a guard that crashes has not noticed
            v.state = FAIL
            v.detail = f"reversion raised {type(exc).__name__}: {exc}"
        else:
            v.state = PASS if noticed else FAIL
            v.note(note)
            if not noticed:
                v.detail = "the guard did not notice its own reversion"
        report.add(v)
    return report


if __name__ == "__main__":
    from certify.verdict import emit
    raise SystemExit(emit(run()))
