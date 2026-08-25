"""Three-state verdicts.

A certification written before its API exists has three outcomes, and
collapsing them to pass/fail is what makes such a suite lie:

``PASS``     the claim was exercised and held.
``FAIL``     the claim was exercised and was violated.  A real defect.
``BLOCKED``  the surface the claim needs does not exist yet, so nothing
             was exercised.  NOT a pass.  A suite that reports BLOCKED
             as green is the vacuous-pass failure mode this repository
             is meant to avoid.

``BLOCKED`` is expected today for every claim that needs ``list_siblings``
or ``send_to_sibling``.  It must become ``PASS`` or ``FAIL`` — never stay
BLOCKED quietly — once the framework ships those.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import List, Optional

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"

_GLYPH = {PASS: "✓", FAIL: "✘", BLOCKED: "○"}


@dataclass
class Verdict:
    """The outcome of one certification, with its evidence attached."""

    claim_id: str
    claim: str
    state: str
    detail: str = ""
    evidence: List[str] = field(default_factory=list)
    # What you REVERT to watch this guard fail.  A guard nobody has seen
    # fail is not a guard; house rule, and it has caught four bad tests.
    revert: str = ""

    def note(self, line: str) -> None:
        self.evidence.append(line)

    def render(self) -> str:
        head = f"{_GLYPH[self.state]} {self.claim_id} {self.state}: {self.claim}"
        body = [head]
        if self.detail:
            body.append(f"    {self.detail}")
        for line in self.evidence:
            body.append(f"      | {line}")
        return "\n".join(body)


@dataclass
class Report:
    """A run of several certifications."""

    verdicts: List[Verdict] = field(default_factory=list)

    def add(self, v: Verdict) -> Verdict:
        self.verdicts.append(v)
        return v

    def counts(self) -> dict:
        out = {PASS: 0, FAIL: 0, BLOCKED: 0}
        for v in self.verdicts:
            out[v.state] += 1
        return out

    def render(self) -> str:
        lines = [v.render() for v in self.verdicts]
        c = self.counts()
        lines.append("")
        lines.append(
            f"{c[PASS]} passed, {c[FAIL]} failed, {c[BLOCKED]} blocked "
            f"(blocked = surface absent; NOT a pass)"
        )
        return "\n".join(lines)

    def exit_code(self) -> int:
        """FAIL is 1.  BLOCKED is 2 — distinct, so CI cannot read a
        not-yet-built surface as success."""
        c = self.counts()
        if c[FAIL]:
            return 1
        if c[BLOCKED]:
            return 2
        return 0


def emit(report: Report, stream=sys.stdout) -> int:
    print(report.render(), file=stream)
    return report.exit_code()
