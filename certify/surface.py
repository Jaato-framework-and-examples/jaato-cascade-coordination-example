"""Probe the framework for the surface these certifications need.

Runs offline, with no provider credential and no daemon.  Its whole job
is to turn "the API isn't built yet" into a precise, machine-checkable
statement, so a blocked certification says WHICH verb is missing instead
of dying in a stack trace.

Everything here goes through the public ``jaato-scaffold`` CLI or the
``jaato_sdk`` package.  No server-internal import.
"""
from __future__ import annotations

import inspect
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from certify.contract import (
    EXTENSION_ENTRY_POINT_GROUP, REACTOR_EXTENSION_NAME, REACTOR_RULE_DIRS,
    SIBLING_NAME_FIELD,
)

#: The sibling verbs live in the EXISTING ``subagent`` plugin rather than a
#: plugin of their own.  ``send_to_subagent`` is the precedent, and the
#: injection machinery + session registry already sit there; ``list_siblings``
#: is ``list_active_subagents`` with ``cascade_driver_id`` as the predicate
#: instead of ``owner_id``.
#:
#: Co-location is safe because a profile can scope a plugin to named tools
#: — ``subagent(mode:preload, tools:[list_siblings,send_to_sibling])`` grants the
#: two coordination verbs WITHOUT ``spawn_subagent``.  So probing for the
#: plugin is not enough: the plugin has always existed.  The verbs are what
#: this asks about.
SIBLING_PLUGIN = "subagent"

#: Tools the certifications call, and the claim each one unblocks.
REQUIRED_TOOLS = {
    "list_siblings": ("C1", "C5b"),
    "send_to_sibling": ("C1", "C2", "C3", "C4"),
}

#: Requirements that are not tools.  C5a certifies the sibling-name
#: constraint, which is a NEW ``session.new`` field, not a verb — gating
#: it on ``list_siblings`` would blame the wrong absence.  The field has no
#: agreed name yet, so it is tracked by what it does.
REQUIRED_CAPABILITIES = {
    # Framework step 3, which lands BEFORE either verb — so this gate
    # opens first and C5a runs while the roster claims are still blocked.
    SIBLING_NAME_FIELD: ("C5a",),
    # C3c needs a live reactor to mint the extra siblings.  Detected via
    # the entry-point group the daemon itself discovers through, so this
    # is a consumer's legitimate view and not a premium import.
    "reactor_rules": ("C3c",),
    # C1 must show that a SPECIFIC sibling ran; C4a must show that a
    # specific one did NOT.  Both need turn events attributable to a
    # session, and the bus does not carry that — see
    # harness.EventLog.sessions_that_ran.
    # C1 only.  C4a was moved off it: SessionWokenEvent carries
    # session_id, so the wake claim is attributable without turn events.
    "turn_attribution": ("C1",),
}

#: Arguments the certifications depend on, per tool.
REQUIRED_TOOL_ARGS = {
    # C4: a cold sibling must be QUEUED unless the caller explicitly says
    # otherwise, so the flag has to exist and default to not-waking.
    # `sibling_name` is the same identifier create_session sets — one
    # string, both ends, no translation table.
    "send_to_sibling": ("sibling_name",),
}


class UnknownClaim(KeyError):
    """A claim id that no requirement covers — its gate cannot be trusted."""


def _declares_requirement(claim_id: str) -> bool:
    """Does any requirement mention this claim (or a stem of it)?"""
    for claims in REQUIRED_TOOLS.values():
        if any(claim_id.startswith(c) for c in claims):
            return True
    for claims in REQUIRED_CAPABILITIES.values():
        if claim_id in claims:
            return True
    return False


class ScaffoldUnavailable(RuntimeError):
    """``jaato-scaffold`` is not on PATH — the framework is not installed."""


def _scaffold() -> str:
    exe = shutil.which("jaato-scaffold")
    if exe is None:
        raise ScaffoldUnavailable(
            "jaato-scaffold is not on PATH; install the framework "
            "(pip install -e jaato/jaato-sdk) before certifying"
        )
    return exe


def explain_plugin(name: str) -> dict:
    """``jaato-scaffold explain plugin <name> --json``.

    Returns the decoded document.  An unknown plugin comes back as
    ``{"error": "unknown plugin '<name>'"}`` — that is the CLI's own
    shape, not an exception, so absence reads as data.
    """
    out = subprocess.run(
        [_scaffold(), "explain", "plugin", name, "--json"],
        capture_output=True, text=True, check=False,
    )
    # The CLI writes an "[MCP] ..." banner to stderr; stdout is pure JSON.
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"error": f"unparseable explain output: {out.stdout[:200]!r}"}


@dataclass
class SurfaceReport:
    """What exists, what does not, and which claims that blocks."""

    plugin_present: bool = False
    tools_present: Dict[str, bool] = field(default_factory=dict)
    tool_args_present: Dict[str, Dict[str, bool]] = field(default_factory=dict)
    client_kwargs: Dict[str, bool] = field(default_factory=dict)
    capabilities: Dict[str, bool] = field(default_factory=dict)
    #: The reactor mechanism, tracked apart from whether any rule uses it.
    reactor_mechanism: bool = False
    notes: List[str] = field(default_factory=list)

    def missing_for(self, claim_id: str) -> List[str]:
        """Which required verbs are absent, for one claim.

        Raises ``UnknownClaim`` when the id declares no requirement at
        all.  Returning "no gaps" for an unrecognised claim would open
        the gate for it — the precise vacuous pass this suite exists to
        prevent, and it is not hypothetical: retargeting ``list_siblings``
        from ``C5`` to ``C5b`` orphaned the bare ``C5`` stem, and R4
        caught it before anything called the orphaned gate.
        """
        if not _declares_requirement(claim_id):
            raise UnknownClaim(
                f"claim {claim_id!r} declares no required surface; a gate "
                f"for it would open unconditionally.  Add it to "
                f"REQUIRED_TOOLS or REQUIRED_CAPABILITIES."
            )
        gaps: List[str] = []
        if not self.plugin_present:
            gaps.append(f"plugin '{SIBLING_PLUGIN}'")
        for tool, claims in REQUIRED_TOOLS.items():
            # 'C3a' / 'C3b' are sub-probes of claim C3; match on the stem
            # so a blocked sub-probe still names the verb it is waiting on.
            wanted = any(claim_id.startswith(c) for c in claims)
            if wanted and not self.tools_present.get(tool):
                gaps.append(f"{SIBLING_PLUGIN}.{tool}")
        for cap, claims in REQUIRED_CAPABILITIES.items():
            if claim_id in claims and not self.capabilities.get(cap):
                gaps.append(cap)
        return gaps

    def render(self) -> str:
        lines = [f"surface probe — plugin '{SIBLING_PLUGIN}': "
                 f"{'present' if self.plugin_present else 'ABSENT'}"]
        for tool, ok in sorted(self.tools_present.items()):
            lines.append(f"  {'✓' if ok else '○'} {SIBLING_PLUGIN}.{tool}")
            for arg, arg_ok in sorted(self.tool_args_present.get(tool, {}).items()):
                lines.append(f"      {'✓' if arg_ok else '○'} arg '{arg}'")
        for kwarg, ok in sorted(self.client_kwargs.items()):
            lines.append(f"  {'✓' if ok else '○'} IPCClient.create_session({kwarg}=...)")
        for cap, ok in sorted(self.capabilities.items()):
            lines.append(f"  {'✓' if ok else '○'} {cap}")
        for note in self.notes:
            lines.append(f"  · {note}")
        return "\n".join(lines)


def probe() -> SurfaceReport:
    """Ask the installed framework what it actually offers today."""
    rep = SurfaceReport()

    doc = explain_plugin(SIBLING_PLUGIN)
    rep.plugin_present = "error" not in doc
    declared = {t.get("name"): t for t in doc.get("tools", [])}

    for tool in REQUIRED_TOOLS:
        present = tool in declared
        rep.tools_present[tool] = present
        wanted = REQUIRED_TOOL_ARGS.get(tool, ())
        if wanted:
            # `explain plugin --json` lists names + descriptions, not
            # parameter schemas.  Absent a public schema view, an
            # argument can only be confirmed live, from a session that
            # holds the tool.  Record the gap rather than guessing.
            rep.tool_args_present[tool] = {
                arg: (present and _arg_visible(declared[tool], arg))
                for arg in wanted
            }

    # Driver-side: cid-scoped session naming (framework step 3).  The
    # keyword already exists; that its uniqueness is scoped PER CASCADE
    # rather than globally is a runtime property, certified live by C1.
    sig = _create_session_signature()
    rep.client_kwargs = {
        "name": "name" in sig,
        "cascade_driver_id": "cascade_driver_id" in sig,
    }

    # The sibling name is a NEW create_session field, deliberately not
    # `name` (which defaults to "Session 2026-08-24 14:15" and would
    # break every existing session if constrained retroactively).
    # Probed by its actual identifier — an earlier version asked "is
    # there any field beyond the seven that exist today", which would
    # have gone green on any new field anyone added for any reason.
    import os
    workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import jaato_sdk.events as _ev
    rep.capabilities = {
        SIBLING_NAME_FIELD: SIBLING_NAME_FIELD in sig,
        # Declared-schema check: verified live too — no session_id is
        # attached in transit either.
        "turn_attribution": "session_id" in set(
            getattr(_ev.TurnCompletedEvent, "model_fields", {}) or {}),
        # C3c needs a rule that actually mints siblings.  The mechanism
        # being present is NOT the same fact and must not stand in for it.
        "reactor_rules": bool(authored_reactor_rules(workspace)),
    }
    rep.reactor_mechanism = reactor_extension_present()

    if not rep.plugin_present:
        rep.notes.append(
            f"'{SIBLING_PLUGIN}' is unknown to the installed framework — "
            "every claim needing a sibling verb is BLOCKED, not failing"
        )
    elif not all(rep.tools_present.values()):
        # The host plugin predates the verbs, so its presence proves
        # nothing.  Say that plainly, or a reader sees a present plugin
        # and assumes the surface landed.
        rep.notes.append(
            f"'{SIBLING_PLUGIN}' is present but ships neither sibling verb — the "
            "plugin predates them; presence of the host is not the surface"
        )
    rep.notes.append(
        "SourceType.SIBLING (framework step 2) is deliberately NOT visible "
        "to a consumer; it is certified behaviourally by C2, which is "
        "the only view a consumer has of it"
    )
    if not rep.capabilities.get("reactor_rules"):
        rep.notes.append(
            f"reactor MECHANISM "
            f"{'present' if rep.reactor_mechanism else 'absent'}; no rule "
            f"files authored in {', '.join(REACTOR_RULE_DIRS)} — so C3c "
            f"cannot be constructed TODAY, but the path is not closed: a "
            f"rule file makes it live with no code change"
        )
    if not rep.capabilities.get("turn_attribution"):
        rep.notes.append(
            "TurnCompletedEvent carries no session_id (only agent_id, "
            "'main' for every top-level session), so a cascade observer "
            "cannot tell WHICH sibling took a turn — C1 needs that"
        )
    if not rep.capabilities.get(SIBLING_NAME_FIELD):
        rep.notes.append(
            f"create_session has no {SIBLING_NAME_FIELD!r} field — C5a is "
            f"blocked on THAT (framework step 3), not on a missing verb; "
            f"it unblocks before the roster claims do"
        )
    return rep


def _arg_visible(tool_doc: dict, arg: str) -> bool:
    """Is ``arg`` observable in whatever the CLI publishes for this tool?

    Today ``explain plugin --json`` publishes name + description only, so
    the honest answer for a parameter is "not visible from here" unless
    the CLI grows a schema view.  Checked against the document rather
    than assumed, so this starts reporting the truth the day it does.
    """
    params = tool_doc.get("parameters") or tool_doc.get("schema")
    if isinstance(params, dict):
        props = params.get("properties", params)
        return arg in props
    return False


def reactor_extension_present() -> bool:
    """Is the reactor MECHANISM installed?

    Read from ``jaato.extensions`` — the group the daemon itself mounts
    extensions from — so this is a consumer view, not a premium import.
    """
    import importlib.metadata as md
    try:
        names = {e.name for e in
                 md.entry_points(group=EXTENSION_ENTRY_POINT_GROUP)}
    except Exception:       # noqa: BLE001 — absence is data, not an error
        return False
    return REACTOR_EXTENSION_NAME in names


def authored_reactor_rules(workspace: str) -> List[str]:
    """Rule FILES present, which is a separate fact from the mechanism.

    Rules load from directories rather than entry points, so an empty
    result means "nothing authored here yet" — NOT "the path cannot
    happen".  A rule file appearing tomorrow makes the path live with no
    code change anywhere, which is why C3c states the distinction rather
    than collapsing it into "inert".
    """
    import os
    found: List[str] = []
    for d in (os.path.expanduser("~/.jaato/reactors"),
              os.path.join(workspace, ".jaato", "reactors")):
        if os.path.isdir(d):
            found.extend(os.path.join(d, f) for f in sorted(os.listdir(d))
                         if not f.startswith("."))
    return found


def _create_session_signature() -> Dict[str, inspect.Parameter]:
    from jaato_sdk import IPCClient
    return dict(inspect.signature(IPCClient.create_session).parameters)


if __name__ == "__main__":
    print(probe().render())


def gate(claim_id: str, claim: str, revert: str):
    """Return a BLOCKED Verdict if this claim's surface is absent, else None.

    Called first by every certification, so an unbuilt API produces a
    precise "which verb is missing" verdict instead of a stack trace —
    and, crucially, never produces a pass.
    """
    from certify.verdict import BLOCKED, Verdict

    rep = probe()
    gaps = rep.missing_for(claim_id)
    if not gaps:
        return None
    v = Verdict(claim_id=claim_id, claim=claim, state=BLOCKED, revert=revert)
    v.detail = "surface absent: " + ", ".join(gaps)
    v.note("nothing was exercised — this is NOT a pass")
    return v
