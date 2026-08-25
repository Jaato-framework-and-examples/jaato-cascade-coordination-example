"""Run every certification and report honestly.

Exit codes are three-valued on purpose:

  0  everything exercised and held
  1  a claim was exercised and VIOLATED
  2  a claim could not be exercised — the surface is not built yet

CI must not read 2 as success.  A suite that collapses "not built" into
green is the failure mode this repository was created to rule out.
"""
from __future__ import annotations

import sys
from typing import Callable, List

from certify import facade_guard, selftest, surface
from certify.verdict import FAIL, Report, Verdict, emit

from certify import (
    c1_no_driver_in_the_loop as c1,
    c2_sibling_cannot_approve as c2,
    c3_own_books_budget_hole as c3,
    c4_cold_sibling_is_queued as c4,
    c5_roster_is_data as c5,
)

CERTIFICATIONS: List[Callable[[], object]] = [
    c1.certify, c2.certify, c3.certify, c4.certify, c5.certify,
]


def run() -> Report:
    report = Report()

    # Pin the commit before anything runs.  The claims below certify THIS
    # commit; a merge that lands mid-run does not invalidate the result
    # and must not abort it.
    from certify import harness
    sha = harness.pin_framework_head()
    print(f"certifying framework commit {sha or '(unknown)'}\n")

    # The guards go first.  If a guard cannot notice its own reversion,
    # nothing downstream of it is evidence.
    print("— guard reversions —")
    guards = selftest.run()
    print(guards.render())
    print()
    if guards.counts()["FAIL"]:
        print("a guard failed its own reversion; certifications below are "
              "not evidence until that is fixed\n", file=sys.stderr)

    leaks = facade_guard.check_publishable(".")
    internals = facade_guard.check_imports(".")
    undefined = facade_guard.check_undefined_names(".")
    drift = facade_guard.check_spec_matches_contract("SURFACE.md")
    print("— publishable —")
    for v in list(internals) + list(leaks) + list(undefined) + list(drift):
        print(f"  ✘ {v}")
    print(f"  {len(internals)} server-internal import(s), "
          f"{len(leaks)} publishability violation(s), "
          f"{len(undefined)} undefined name(s), "
          f"{len(drift)} spec drift(s)")
    print()

    print("— surface —")
    print(surface.probe().render())
    print()

    print("— claims —")
    for fn in CERTIFICATIONS:
        # One claim must never take down the others.  A timeout in C1
        # aborted an entire twenty-minute run and reported NOTHING about
        # the seven claims after it — the suite lost more information to
        # its own fragility than any single claim could have carried.
        # An unexpected exception is a FAIL of that claim, with the cause
        # attached, and the run continues.
        try:
            result = fn()
        except BaseException as exc:  # noqa: BLE001 — isolation boundary
            name = getattr(fn, "__module__", "?").split(".")[-1]
            result = Verdict(
                claim_id=name.split("_")[0].upper(),
                claim=f"{name} raised instead of reporting",
                state=FAIL,
                detail=f"{type(exc).__name__}: {exc}",
            )
        for v in (result if isinstance(result, list) else [result]):
            report.add(v)
    return report


def main() -> int:
    report = run()
    code = emit(report)
    blocked = [v for v in report.verdicts if v.state == "BLOCKED"]
    if blocked:
        print()
        print("blocked claims are waiting on the framework, not on this "
              "repository.  They are written, they compile, and they will "
              "run unchanged the day the surface lands:")
        for v in blocked:
            print(f"  {v.claim_id}: {v.detail}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
