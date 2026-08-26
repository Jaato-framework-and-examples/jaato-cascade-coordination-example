"""Live plumbing shared by the certifications.

Only the published facade: ``jaato_sdk.IPCClient`` plus the workspace
conventions ``jaato-scaffold`` generates.  Nothing here knows how the
daemon works inside, and that is the point.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional

from jaato_sdk import ClientType, EventType, IPCClient

from certify.contract import SIBLING_NAME_FIELD

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The daemon reads read-only framework config (profiles, agents) from
#: here.  It must point AT ``.jaato``, not at the workspace root:
#: profile discovery scans ``<config_root>/profiles``, so a config_root
#: of the workspace root looks for ``<workspace>/profiles`` and finds
#: nothing — with no error, because an empty profile set is legal.
CONFIG_ROOT = os.path.join(REPO, ".jaato")

ENV_FILE = os.path.join(REPO, ".env")

#: AF_UNIX caps a socket path at 108 bytes and does not truncate
#: gracefully.  Scratchpad-style paths overflow it, so the socket lives
#: somewhere deliberately short.  Asserted, not hoped for.
#:
#: A daemon of OUR OWN, with its own pidfile, started out of band:
#:
#:     python -m server --ipc-socket /tmp/jc.sock \
#:                      --pid-file  /tmp/jc.pid --daemon
#:
#: Not autostarted, because ``auto_start=True`` on a non-default socket
#: is unsafe while any other daemon runs: ``IPCClient.DEFAULT_PID_FILE``
#: is a module constant (``/tmp/jaato.pid``) and the autostart command
#: passes ``--ipc-socket`` but never ``--pid-file``.  The client reads
#: THAT daemon's pidfile, finds the PID alive but its own socket silent,
#: concludes "stale daemon / reused PID", and unlinks another tenant's
#: pidfile before launching.
#:
#: Not the shared default socket either: a long-running daemon holds the
#: code it was STARTED with.  Reusing one that predated the feature under
#: test made C5a fail against eight-hour-old code — a false defect, and
#: the framework's own doctor prescribes reuse without saying that the
#: daemon's vintage is part of "fit".
SOCKET = "/tmp/jc.sock"
SOCKET_MAX = 108

#: Refuse to run against a daemon older than the code being certified.
#: A stale daemon does not error — it silently answers with the behaviour
#: of whenever it started.
PID_FILE = "/tmp/jc.pid"

#: The profiles interpolate this and say nothing about where it comes
#: from.  Choosing the FORM — a plain value, or a secret URI resolved by
#: an out-of-tree resolver — is ``.env``'s job, and ``.env`` is
#: gitignored.  So this module names the VARIABLE and never a form:
#: presuming one would both break a public checkout and put a
#: premium-only URI in a committed file.
CREDENTIAL_ENV = "JAATO_OPENROUTER_API_KEY"


class TurnAttributionUnavailable(RuntimeError):
    """Turn events cannot be tied to a sibling from the cascade bus."""


class PreconditionUnmet(RuntimeError):
    """The environment cannot host a live run.  Report, do not improvise."""


def check_socket_path(path: str = SOCKET) -> None:
    if len(path.encode()) > SOCKET_MAX:
        raise PreconditionUnmet(
            f"--ipc-socket path is {len(path.encode())} bytes; AF_UNIX caps "
            f"at {SOCKET_MAX} and fails opaquely past it: {path}"
        )


def _configured_credential() -> Optional[str]:
    """The RAW value ``.env`` binds to the credential variable, or None.

    Raw on purpose: a secret URI is not a secret, and a plain value is
    never inspected beyond "is it non-empty".  Nothing here prints,
    copies or exports what it reads.
    """
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(f"{CREDENTIAL_ENV}="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def _secret_uri_resolves(uri: str) -> Optional[bool]:
    """Can the local resolver reach ``uri``?  ``None`` if it cannot be asked.

    Only the ``pass`` scheme is checkable from here — the others resolve
    inside the daemon.  Returns a flag, never the secret.
    """
    import subprocess
    scheme, _, path = uri.partition(":" + "/" * 2)
    if scheme != "pass":
        return None            # daemon-side scheme; the doctor is the witness
    try:
        proc = subprocess.run(["pass", "show", path],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


#: Where the framework this example certifies actually lives.  Read from
#: the installed package rather than assumed, so a different install
#: reports on ITSELF.
#: The commit a run certifies, pinned at start.  Without this the guard
#: compares against a MOVING HEAD: the framework side merged four times
#: while runs were in flight, and a suite that takes ten minutes against a
#: branch that changes every twenty can never finish — every run would
#: abort on staleness that appeared after it began.
#:
#: Pinning also makes the result attributable.  "These claims hold" is not
#: a statement about a repository; it is a statement about a commit.
_PINNED: dict = {}


def framework_root() -> str:
    """The framework checkout this repository is certifying against.

    Derived from the INSTALLED sdk rather than a configured path, so it
    names the tree actually in use.  A configured second copy could
    disagree with what is imported, and the disagreement would be silent.
    """
    import jaato_sdk
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(jaato_sdk.__file__))))


def pin_framework_head() -> Optional[str]:
    """Record the commit this run certifies.  Call once, at run start."""
    import subprocess
    repo = framework_root()
    try:
        out = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%ct %h"],
                             capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    when, _, sha = out.stdout.strip().partition(" ")
    _PINNED["time"], _PINNED["sha"] = float(when), sha
    return sha


def pinned_sha() -> Optional[str]:
    return _PINNED.get("sha")


def check_head_is_mainline() -> None:
    """Refuse to certify a commit that is not on the mainline.

    A DIFFERENT fault from certifying a stale commit, and invisible to
    the staleness guard: a run once pinned the tip of an unmerged
    feature branch, because this checkout is SHARED and someone else
    checked that branch out in it.  HEAD genuinely was that commit and
    the daemon genuinely was newer than it, so every consistency check
    passed — the verdict was faithfully attributed to a commit that
    existed on no mainline and would later exist on none at all.

    "Attributable" and "authoritative" are different properties.  The
    pin gives the first; only this gives the second.

    Skips (rather than fails) when there is no ``origin/main`` to
    compare against — a checkout without a remote is a legitimate
    configuration, not a fault, and this must not become a second way
    to be blocked for reasons unrelated to the claims.
    """
    import subprocess
    import jaato_sdk
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(jaato_sdk.__file__))))

    def _git(*args) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", repo, *args],
                              capture_output=True, text=True, timeout=15)

    try:
        if _git("rev-parse", "--verify", "origin/main").returncode != 0:
            return
        if _git("merge-base", "--is-ancestor", "HEAD", "origin/main").returncode == 0:
            return
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        head = _git("rev-parse", "--short", "HEAD").stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    raise PreconditionUnmet(
        f"the framework checkout is at {head} on '{branch}', which is NOT an "
        f"ancestor of origin/main — this would certify unmerged code and "
        f"stamp the verdict with a commit that may exist on no branch later"
    )


def _framework_head_time() -> Optional[float]:
    """Commit time of the pinned commit, or of HEAD when unpinned."""
    if "time" in _PINNED:
        return _PINNED["time"]
    import subprocess
    import jaato_sdk
    repo = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(jaato_sdk.__file__))))
    try:
        out = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%ct"],
                             capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return float(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def _daemon_start_time() -> Optional[float]:
    """When the daemon behind PID_FILE started, or None."""
    import subprocess
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return None
    # ELAPSED SECONDS, not a formatted date: `ps -o lstart=` renders
    # month and day names in the daemon's locale ("lun ago 24 ..." here),
    # so parsing it silently returned None and the check skipped itself
    # on the very machine it was written for — a guard that cannot fire
    # is the failure mode this suite exists to catch, so it must not be
    # one of its own preconditions.
    try:
        out = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    raw = out.stdout.strip()
    if not raw.isdigit():
        return None
    import time
    return time.time() - int(raw)


def check_daemon_is_newer_than_the_code() -> None:
    """Refuse to certify current code against a daemon that predates it.

    A long-running daemon holds whatever it was STARTED with and says
    nothing about its vintage.  Certifying against one produced the only
    false result this suite has reported: a merged feature read as
    missing, which would have been filed as a framework defect.

    Locale-dependent ``ps`` output and a non-git install both yield None,
    and None SKIPS the check rather than failing it — this guards against
    a specific known trap, and must not become a second way to be blocked
    for reasons unrelated to the claims.
    """
    started = _daemon_start_time()
    head = _framework_head_time()
    if started is None or head is None:
        return
    if started < head:
        import datetime as _dt
        fmt = lambda t: _dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
        raise PreconditionUnmet(
            f"the daemon started {fmt(started)} but the framework HEAD is "
            f"{fmt(head)} — it is running code older than what is being "
            f"certified, and will answer with the behaviour of whenever it "
            f"started.  Restart it: python -m server --pid-file {PID_FILE} "
            f"--stop, then relaunch."
        )


def check_preconditions() -> None:
    """Everything a live certification needs, checked before it starts."""
    check_socket_path()
    check_head_is_mainline()
    check_daemon_is_newer_than_the_code()
    if not os.path.isdir(os.path.join(CONFIG_ROOT, "profiles")):
        raise PreconditionUnmet(
            f"no profiles under {CONFIG_ROOT}/profiles — config_root must "
            f"point at .jaato, not at the workspace root"
        )
    configured = _configured_credential()
    if not configured:
        raise PreconditionUnmet(
            f"{CREDENTIAL_ENV} is empty in {ENV_FILE}; a live certification "
            f"needs a credential (a plain value or a secret URI — see "
            f".env.example)"
        )
    if ":" + "/" * 2 in configured:
        resolved = _secret_uri_resolves(configured)
        if resolved is False:
            raise PreconditionUnmet(
                f"{CREDENTIAL_ENV} is a secret URI that does not resolve "
                f"locally; `pass show` failed for it"
            )


def new_client() -> IPCClient:
    """The known-good client knobs, from the scaffolded cascade archetype."""
    return IPCClient(
        SOCKET,
        client_type=ClientType.API,   # load-bearing: keeps signal_completion
        auto_start=False,             # never clobber another tenant's pidfile
        env_file=ENV_FILE,            # never None — the handshake crashes on None
        workspace_path=REPO,
        config_root=CONFIG_ROOT,      # AT .jaato; see the note above
    )


def new_cascade_id() -> str:
    """One tenant id per cascade.  Peers are siblings inside it."""
    return uuid.uuid4().hex


@dataclass
class SiblingHandle:
    """A named sibling session inside a cascade."""

    name: str
    session_id: str


async def create_sibling(client: Any, cascade_id: str, name: str,
                      profile: str, agent: Optional[str] = None) -> SiblingHandle:
    """Create one addressable sibling in ``cascade_id``.

    ``sibling_name`` is the address, and it is the SAME identifier another
    session passes to ``send_to_sibling`` — the string set here is the
    string used there, with no translation table.  That matters because
    the address usually has to be written by hand in a persona or a
    profile, where a second name for the same thing is unusable.

    Unique within the cid rather than globally, so two cascades may both
    hold a 'reviewer' without colliding — which is what lets a profile
    hardcode the name it answers to.
    """
    sid = await client.create_session(
        profile=profile, agent=agent,
        cascade_driver_id=cascade_id, timeout=60.0,
        **{SIBLING_NAME_FIELD: name},
    )
    if not sid:
        raise PreconditionUnmet(f"sibling '{name}' failed to spawn")
    return SiblingHandle(name=name, session_id=sid)


class EventLog:
    """Everything the driver OBSERVED, with nothing it sent.

    Populated from ``cascade_events`` — the read-only cascade bus — so a
    certification's evidence comes from a witness that is not also a
    participant.
    """

    def __init__(self) -> None:
        self.events: List[Any] = []

    def record(self, ev: Any) -> None:
        self.events.append(ev)

    def of_type(self, wire_type: str) -> List[Any]:
        return [e for e in self.events
                if getattr(getattr(e, "type", None), "value", None) == wire_type
                or getattr(e, "type", None) == wire_type]

    def for_session(self, session_id: str) -> List[Any]:
        """Events attributable to one session.

        Only events that actually DECLARE ``session_id`` can be matched;
        turn events cannot (see :meth:`sessions_that_ran`).  Kept narrow
        rather than silently returning [] for the ones that cannot.
        """
        return [e for e in self.events
                if getattr(e, "session_id", None) == session_id]

    def sessions_that_ran(self) -> set:
        """Sessions observed taking a turn — NOT DERIVABLE from the bus.

        ``TurnCompletedEvent`` declares no ``session_id`` and none is
        attached in transit (verified live: its keys are agent_id,
        duration_seconds, finish_reason, formatted_text, function_calls,
        timestamp, turn_number, type, usage).  The only identifier is
        ``agent_id``, which is ``"main"`` for every top-level session —
        so two siblings in one cascade are indistinguishable.

        Attribution arrived in framework #603 — but the check stays,
        because the failure it guards was silent.  Before it, this read
        ``e.session_id``, found nothing, and returned an empty set every
        time: C1 would have reported "sibling-b never took a turn" no
        matter what happened, and C4a would have read a WOKEN sibling as
        still cold and PASSED.

        So the field's presence is verified rather than assumed.  If it
        ever goes away the answer becomes an exception again, not a
        confident empty set — an unanswerable question must not return
        a plausible answer.
        """
        from jaato_sdk.events import TurnCompletedEvent
        if "session_id" not in (getattr(TurnCompletedEvent,
                                        "model_fields", {}) or {}):
            raise TurnAttributionUnavailable(
                "TurnCompletedEvent no longer declares session_id — turn "
                "events cannot be attributed to a sibling, and an empty "
                "result here would read as 'nobody ran'"
            )
        return {getattr(e, "session_id", None)
                for e in self.of_type(EventType.TURN_COMPLETED.value)
                if getattr(e, "session_id", None)}

    def turns_completed(self, session_id: str) -> int:
        """How many turns have COMPLETED on one session.

        A COUNT, because :meth:`sessions_that_ran` is a set and a set
        cannot answer "did it run AGAIN".  C4a needs exactly that: its
        idle sibling is prompted at creation, so it is already a member
        before the send, and `after - before` is empty however many turns
        it takes afterwards.

        Both failures are the same defect wearing opposite signs — a
        witness whose value does not depend on the thing under test.  The
        cumulative membership test could never fail; the set difference
        could never succeed; and the second was written to fix the first.
        Counting is what the question actually was.
        """
        self.sessions_that_ran()      # reuse the attribution guard
        return sum(1 for e in self.of_type(EventType.TURN_COMPLETED.value)
                   if getattr(e, "session_id", None) == session_id)


# --- reading events, safely -----------------------------------------------
#: Every event field the certifications read, declared once.
#:
#: Guarded by R10 against the SDK's own event classes, because a
#: ``getattr(ev, "output", None)`` on a field that does not exist returns
#: None and reads exactly like "the tool produced nothing".  Two probes
#: shipped with that bug and reported "no result observed" while the tool
#: under test was being called and failing every run.  A typo in a field
#: name must fail loudly at guard time, not quietly at certification time.
#: Keyed on the PUBLIC event CLASS, not on a wire string: the SDK's
#: wire-type -> class map is private (``_EVENT_CLASSES``), and reaching
#: for it would break the same facade rule this repository enforces on
#: everything else.  Naming the class also means a class rename breaks
#: loudly here instead of silently resolving to nothing.
EVENT_FIELDS = {
    "ToolCallStartEvent": ("tool_name", "call_id", "tool_args"),
    "ToolCallEndEvent": ("tool_name", "call_id", "success",
                         "is_error_result", "error_message"),
    # session_id landed on the base Event and is populated centrally at
    # the fan-out chokepoint, so a routed event cannot arrive unstamped.
    "TurnCompletedEvent": ("session_id", "agent_id", "usage",
                           "duration_seconds", "function_calls"),
    "SessionWokenEvent": ("session_id",),
    "PermissionRequestedEvent": ("request_id",),
    "PermissionResolvedEvent": ("request_id", "granted", "method"),
    "SystemMessageEvent": ("message",),
    "HistoryEvent": ("history", "agent_id"),
    "ErrorEvent": ("error", "error_type", "recoverable"),
    # Read by examples/common.py — the guards cover the whole repository,
    # not just the certifications, so the example cannot drift onto a
    # field that does not exist either.
    "AgentCompletedEvent": ("payload", "success", "agent_id", "error"),
}

#: Tool RESULT BODIES do not travel on the event stream at all — the
#: call_end event carries only success/error metadata, and no
#: ``tool.output`` is emitted for them.  The body reaches a consumer as a
#: ``function_response`` part in ``request_history``.  This is the ONLY
#: route; the certifications share it rather than each rediscovering it.
async def fetch_history(client: Any, agent_id: str = "main",
                        timeout: float = 20.0) -> List[Any]:
    """The calling session's conversation, where tool bodies live."""
    from jaato_sdk import EventType
    got: List[Any] = []
    unsub = client.subscribe(
        EventType.HISTORY,
        lambda ev: got.append(getattr(ev, "history", None) or []))
    try:
        await client.request_history(agent_id=agent_id)
        deadline = asyncio.get_event_loop().time() + timeout
        while not got and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.25)
    finally:
        unsub()
    return got[0] if got else []


def tool_bodies(history: List[Any], tool_name: str) -> List[str]:
    """Every result body ``tool_name`` produced, as text."""
    out: List[str] = []
    for entry in history or ():
        if entry.get("role") != "tool":
            continue
        for part in entry.get("parts") or ():
            if (part.get("type") == "function_response"
                    and part.get("name") == tool_name):
                out.append(str(part.get("response", "")))
    return out


def tool_calls_from_disk(cascade_id: str, tool_name: str) -> List[dict]:
    """Every call to ``tool_name`` by any session of one cascade.

    Read from the workspace's own ``.jaato/sessions/*.json`` rather than
    from ``request_history``, because that endpoint serves the client's
    CURRENT session and a session that has finished can no longer be
    asked.  Probes that fetched after the work was done kept reporting
    "no result" for tools that had demonstrably run — the sender's own
    transcript showed the call while the probe saw nothing.

    Not a reach into the framework: these are workspace files the
    consumer owns, under the config_root it passed in, and the
    framework's own transcript tooling reads exactly the same files.
    """
    import json
    out: List[dict] = []
    sessions = os.path.join(CONFIG_ROOT, "sessions")
    if not os.path.isdir(sessions):
        return out
    for name in sorted(os.listdir(sessions)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions, name), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("cascade_driver_id") != cascade_id:
            continue
        who = doc.get("sibling_name")
        for entry in doc.get("history") or ():
            if entry.get("role") != "tool":
                continue
            for part in entry.get("parts") or ():
                if (part.get("type") == "function_response"
                        and part.get("name") == tool_name):
                    # The on-disk transcript names the body `result`; the
                    # wire (request_history) names the same thing
                    # `response`.  Two serialisers, two keys, one value —
                    # so a consumer that learns one shape and applies it
                    # to the other reads None and cannot tell that from
                    # "the tool returned nothing".  Accept both rather
                    # than pick, and say why.
                    body = part.get("result")
                    if body is None:
                        body = part.get("response")
                    out.append({"sibling": who,
                                "response": body,
                                "is_error": part.get("is_error"),
                                "untrusted": part.get("untrusted")})
    return out


def body_reports_error(body: str) -> bool:
    """Does a result BODY describe a failure?

    Read from the body, never from the envelope flags: a forwarded tool
    round-trips its ``(ok, payload)`` tuple through JSON, which has no
    tuple type, so the pair arrives as a list, the ok-flag demotes to
    data, and the envelope reports ``success=True`` for a call that
    failed.  Both consumer-side error signals go blind together; the
    text is the only thing that still tells the truth.
    """
    flat = body.replace(" ", "").replace("'", '"')
    return '"status":"error"' in flat or '"error":' in flat


# --- waiting ---------------------------------------------------------
async def wait_for(predicate, timeout: float = 180.0,
                   poll: float = 0.5) -> bool:
    """Wait until ``predicate()`` is true, or ``timeout``.  True if it held.

    For POSITIVE facts — a turn completed, a receipt appeared, a tool
    call ended.  Every one of those has a signal, so waiting a fixed
    interval for them is pure waste: the suite spent the overwhelming
    majority of its wall-clock asleep, at roughly one provider call every
    two to three minutes, which is what made twenty-minute runs cost
    about two dollars.  Slow and expensive are different problems and the
    fixed sleeps were only ever the first.

    NEGATIVE facts are different and this helper does not apply to them.
    "The cold sibling was NOT woken" cannot be established by an event —
    the absence IS the finding, so a real window has to elapse.  Those
    stay as explicit sleeps, and say so where they appear.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return False


async def save_session(client: Any, session_id: str) -> None:
    """Flush a LIVE session's transcript to disk (framework #617).

    Transcripts are written on SAVE, so the evidence a claim reads —
    a receipt body, a tool call — is simply not on disk while the
    session is still loaded.  ``session.save`` writes it by ID, does
    not disturb a running turn, and does not unload anything.

    Before this verb existed the only way to force the write was to
    attach elsewhere and let the unload do it: a side effect standing
    in for an interface, and one that SILENTLY DID NOTHING when the
    client was already attached elsewhere.  That is exactly how C1
    came to abstain on an assertion it believed it was making — the
    hack ran, reported nothing, and left the disk empty, which reads
    identically to "the sibling never sent".

    Because it does not unload, a session can be read WHILE IT IS
    MID-TURN — which is what lets C2 inspect a sibling that is sitting
    on an unresolved permission request without destroying the very
    state it is there to observe.

    The verb answers: ``SystemMessageEvent`` on success, or
    ``SessionSaveError`` when the target is not loaded — an unloaded
    session is already on disk.  Callers here do not read that answer;
    they wait for the EVIDENCE to appear and fail if it does not, so
    a save that quietly did nothing cannot pass as one that worked.
    """
    await client.execute_command("session.save", [session_id])


def session_text(cascade_id: str, sibling_name: str) -> str:
    """Everything one sibling's saved transcript holds, as flat text.

    For proving a payload ARRIVED — which is a different fact from a
    receipt saying it was accepted for delivery.  ``accepted`` is the
    framework's word about its own queue; only the TARGET's transcript
    shows what its model was actually handed, and that is the gap
    between "the attack was attempted" and "the attack was delivered
    intact and still refused".

    Call ``save_session`` first: this reads the file, and the file is
    written on save.
    """
    import json
    chunks: List[str] = []
    sessions = os.path.join(CONFIG_ROOT, "sessions")
    if not os.path.isdir(sessions):
        return ""
    for name in sorted(os.listdir(sessions)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions, name), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("cascade_driver_id") != cascade_id:
            continue
        if doc.get("sibling_name") != sibling_name:
            continue
        for entry in doc.get("history") or ():
            for part in entry.get("parts") or ():
                for key in ("text", "result", "response"):
                    val = part.get(key)
                    if isinstance(val, str):
                        chunks.append(val)
                    elif val is not None:
                        chunks.append(repr(val))
        for val in doc.get("user_inputs") or ():
            if isinstance(val, str):
                chunks.append(val)
    return "\n".join(chunks)


def turn_count_from_disk(cascade_id: str, sibling_name: str) -> Optional[int]:
    """One sibling's own ``turn_count``, or None when it has no transcript.

    A SECOND WITNESS for "did it run", independent of the cascade bus.

    C4a needs to know whether an idle sibling took a turn on a sibling's
    message.  Read off the bus, that fact went missing twice: the daemon
    log showed a 42-second model call while ``turn.completed`` never
    reached the observer, and moving the observer to its own client did
    not change it.  The session's own counter does not depend on any of
    that — and since #617 it can be read WITHOUT unloading the session,
    so observing no longer disturbs the thing observed.

    ``None`` means no transcript exists, which is NOT zero turns: a
    sibling that has never been saved and one that has never run must
    not read alike.  Callers save first and treat None as "cannot say".
    """
    import json
    sessions = os.path.join(CONFIG_ROOT, "sessions")
    if not os.path.isdir(sessions):
        return None
    for name in sorted(os.listdir(sessions)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions, name), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if (doc.get("cascade_driver_id") == cascade_id
                and doc.get("sibling_name") == sibling_name):
            return doc.get("turn_count")
    return None


def replied_after_sibling_message(cascade_id: str, sibling_name: str) -> bool:
    """Did this sibling produce a model turn AFTER a sibling message arrived?

    The claim is that an idle peer RUNS on a sibling's message, and a
    turn counter is a proxy for that.  A weak one: the framework saves at
    turn start and not again, so the on-disk count LAGS — a run that read
    ``0 -> 1`` was watching true values of ``1 -> 2``.  The direction was
    right, but a lagging counter can move for a reason other than the one
    under test, and this claim has already been certified twice by
    witnesses that moved for the wrong reason.

    So this reads the behaviour instead of a proxy: find the entry that
    carries the sibling's untrusted-content marker, and require a MODEL
    entry after it.  That is the claim in its own terms.
    """
    import json
    sessions = os.path.join(CONFIG_ROOT, "sessions")
    if not os.path.isdir(sessions):
        return False
    for name in sorted(os.listdir(sessions)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(sessions, name), encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if (doc.get("cascade_driver_id") != cascade_id
                or doc.get("sibling_name") != sibling_name):
            continue
        seen_sibling_message = False
        for entry in doc.get("history") or ():
            texts = " ".join(
                str(p.get("text") or "") for p in (entry.get("parts") or ()))
            if entry.get("role") == "user" and "source=sibling:" in texts:
                seen_sibling_message = True
            elif seen_sibling_message and entry.get("role") == "model":
                return True
    return False


async def release_siblings(client: Any, *handles: Any) -> None:
    """Delete the sessions a claim created, so the NEXT claim starts clean.

    Every claim here used to leave its siblings loaded for the rest of
    the run.  Each loaded session holds a runner process, so the suite
    grew its own resource floor as it went and the LAST claims paid for
    all the earlier ones: measured 17 runners and 2.6 GB free by the end
    of a run that started with 4.5 GB, with C2 and C4 failing on timing
    that passed comfortably when run alone.

    A claim that fails because an earlier claim was still holding memory
    is reporting on the suite, not on the framework — and it says so in
    the language of a framework defect, which is the expensive kind of
    wrong.

    Deletes unconditionally: these are sessions this process created and
    still holds handles for, so a failure to delete is a real fault and
    should raise rather than be swallowed.  Pass only handles the claim
    owns and has not already deleted.
    """
    for handle in handles:
        if handle is not None:
            await client.delete_session(handle.session_id)
