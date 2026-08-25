"""End-to-end certifications for sibling-to-sibling agent coordination.

Every module here reaches jaato ONLY through the published SDK facade
(the ``jaato_sdk`` package and the ``jaato-scaffold`` CLI).  Reaching
into ``jaato-server/shared/`` or ``server/`` is what this repository
exists to make impossible: an in-process harness that imports internals
can mirror one production path and miss another, and did.  See
``certify/facade_guard.py`` — the rule is enforced, not just stated.
"""
