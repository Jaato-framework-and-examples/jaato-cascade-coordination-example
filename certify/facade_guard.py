"""Keep this repository on the public surface — mechanically.

Two independent guards, for the two ways an example quietly stops being
an example:

``check_imports``   nothing here may import a server internal.  The
                    motivating incident: an in-process harness that
                    imported ``jaato-server/shared/`` mirrored only the
                    SEQUENTIAL tool path and silently missed the default
                    PARALLEL one.  A consumer that can only see the
                    facade cannot make that mistake — provided it really
                    cannot see anything else, which is what this checks.

``DriverLeash``     a client wrapper that FORBIDS outbound calls rather
                    than counting them afterwards.  C1's claim is that a
                    stage reaches a sibling with the driver out of the loop;
                    asserting that after the fact proves nothing, because
                    a relay the author forgot about still satisfies "a
                    message arrived".  Under the leash a relay raises.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Sequence, Set

#: Public surface.  ``jaato_sdk`` is the SDK package; ``jaato`` is its
#: ``jaato.session(...)`` facade.  Nothing else in the framework is API.
PUBLIC_ROOTS = frozenset({"jaato_sdk", "jaato"})

#: Server internals.  Importable from a co-located checkout, which is
#: exactly why the ban has to be enforced and not merely intended.
PRIVATE_ROOTS = frozenset({"shared", "server", "jaato_server"})


@dataclass
class ImportViolation:
    path: str
    lineno: int
    module: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} imports server internal '{self.module}'"


def _iter_sources(root: str) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "__pycache__", ".jaato"}]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _module_roots(tree: ast.AST) -> Iterable[tuple]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) is this package's own.
            if node.level == 0 and node.module:
                yield node.lineno, node.module


def check_imports(root: str = ".") -> List[ImportViolation]:
    """Every server-internal import under ``root``.  Empty list = clean."""
    found: List[ImportViolation] = []
    for path in _iter_sources(root):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), path)
        except SyntaxError as exc:
            found.append(ImportViolation(path, exc.lineno or 0,
                                         f"<unparseable: {exc.msg}>"))
            continue
        for lineno, module in _module_roots(tree):
            if module.split(".")[0] in PRIVATE_ROOTS:
                found.append(ImportViolation(path, lineno, module))
    return found


#: Secret-URI schemes resolved by out-of-tree resolver plugins
#: (jaato-premium).  Usable locally, never publishable: a public
#: checkout has no resolver, so a committed ``pass://`` makes the
#: example depend on a product the reader does not have.
SECRET_URI_SCHEMES = tuple(
    f"{scheme}:{'/' * 2}"
    for scheme in ("pass", "vault", "awssm", "sops", "keyring")
)

#: Files that never reach the remote.  ``.env`` is where the credential
#: FORM is chosen, and it is gitignored precisely so that choice stays local.
NOT_PUBLISHED = frozenset({".env"})

#: Files the FRAMEWORK reads as configuration.  The secret-URI rule
#: applies only here, and the distinction is not a softening:
#:
#: * a URI in a profile is read by the daemon, so on a checkout without
#:   the resolver it silently becomes a literal string where a key
#:   should be — the reader's run breaks and the reason is invisible;
#: * a URI in prose is read by a person, who can see it is an example.
#:
#: The LIVE-KEY rule has no such exemption: a key in a README is leaked
#: exactly as thoroughly as a key in a profile.
CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env")


@dataclass
class PublishViolation:
    path: str
    lineno: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} {self.detail}"


def check_publishable(root: str = ".") -> List[PublishViolation]:
    """Nothing committable may carry a secret URI or a live credential.

    Two separate hazards, one check:

    * a ``pass://`` in a committed profile makes the example unusable
      without jaato-premium — the reader gets a literal URI where a key
      should be, and no resolver to turn it into one;
    * a plaintext key in a committed file is a leaked credential.

    The profiles therefore interpolate ``${JAATO_OPENROUTER_API_KEY}``
    and say nothing about where it comes from.  Choosing the form is
    ``.env``'s job, and ``.env`` is gitignored.
    """
    found: List[PublishViolation] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", "__pycache__", "logs"}]
        for fn in filenames:
            if fn in NOT_PUBLISHED:
                continue
            path = os.path.join(dirpath, fn)
            if fn.endswith((".png", ".jpg", ".pyc")):
                continue
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            is_config = fn.endswith(CONFIG_SUFFIXES)
            for i, line in enumerate(text.splitlines(), 1):
                # A doc line may NAME a scheme; only an assignment binds one.
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("*"):
                    continue
                if is_config:
                    for scheme in SECRET_URI_SCHEMES:
                        if scheme in line and ("=" in line
                                               or ":" in line.split(scheme)[0]):
                            found.append(PublishViolation(
                                path, i,
                                f"binds a {scheme} secret URI in a file the "
                                f"framework READS — needs an out-of-tree "
                                f"resolver the public checkout lacks"))
                if any(("sk-" + tail) in line for tail in ("or-v1-", "ant-")):
                    found.append(PublishViolation(
                        path, i, "looks like a live API key"))
    return found


class LeashBroken(AssertionError):
    """The driver made an outbound call it had promised not to make."""


class DriverLeash:
    """Wrap an SDK client so forbidden outbound calls raise.

    ``allow`` is the whitelist of method names the driver may still
    call.  Anything outside it — ``send_message``, ``inject_prompt``,
    any other way of pushing work at a session — raises
    :class:`LeashBroken` at the call site, naming the method.

    Read-only observation (``cascade_events``, ``subscribe``) is what a
    driver is SUPPOSED to keep doing, so those stay allowed by default;
    a certification narrows the list further when it needs to.
    """

    #: Observation, not participation.  Allowed unless overridden.
    OBSERVER_METHODS = frozenset({
        "cascade_events", "cascade_register", "subscribe", "subscribe_once",
        "subscribe_all", "subscribe_many", "open_event_stream", "events",
        "drain_events", "connect", "disconnect", "is_connected",
        "connection_state", "session_id", "client_id", "server_version",
        "cascade_budget_get",
    })

    def __init__(self, client: Any, *, allow: Sequence[str] = ()):
        self._client = client
        self._allow: Set[str] = set(self.OBSERVER_METHODS) | set(allow)
        self.calls: List[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr
        if name in self._allow:
            return self._record(name, attr)
        def _forbidden(*_a: Any, **_kw: Any):
            raise LeashBroken(
                f"driver called '{name}' while leashed — the claim under "
                f"certification is that the driver is NOT in the loop, so "
                f"a relay through the driver must break the run, not pass it"
            )
        return _forbidden

    def _record(self, name: str, attr: Callable) -> Callable:
        def _wrapped(*a: Any, **kw: Any):
            self.calls.append(name)
            return attr(*a, **kw)
        return _wrapped


if __name__ == "__main__":
    violations = check_imports(os.path.dirname(os.path.dirname(__file__)) or ".")
    for v in violations:
        print(v)
    print(f"{len(violations)} server-internal import(s)")


# --- reading event fields -------------------------------------------------
#: Names that are read off an event-shaped variable but are NOT event
#: model fields: SDK envelope attributes and plain dict keys.
NON_FIELD_READS = frozenset({"type", "value", "timestamp"})


@dataclass
class FieldRead:
    path: str
    lineno: int
    name: str

    def __str__(self) -> str:
        return (f"{self.path}:{self.lineno} reads undeclared event field "
                f"'{self.name}'")


def check_event_field_reads(root: str, declared: frozenset) -> List[FieldRead]:
    """Find ``getattr(ev, "name", ...)`` reads of undeclared event fields.

    R10 checks that the DECLARED names exist.  This checks the converse —
    that nothing reads a name it never declared — because the declaration
    is what R10 validates, and a read that bypasses it bypasses the guard
    entirely.  ``getattr`` on a missing field returns the default, so the
    bug is silent in exactly the way this whole repository is about.

    Matched on the AST, and only where the object is an event-shaped
    variable (``ev`` / ``event``), so a dict ``.get`` or an unrelated
    ``getattr`` is not swept in.
    """
    found: List[FieldRead] = []
    for path in _iter_sources(root):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2):
                continue
            target, attr = node.args[0], node.args[1]
            if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
                continue
            base = target
            while isinstance(base, ast.Call) and base.args:
                base = base.args[0]          # getattr(getattr(ev, ...), ...)
            if not (isinstance(base, ast.Name) and base.id in {"ev", "event"}):
                continue
            name = attr.value
            if name in declared or name in NON_FIELD_READS:
                continue
            found.append(FieldRead(path, node.lineno, name))
    return found


# --- undefined names ------------------------------------------------------
@dataclass
class UndefinedName:
    path: str
    scope: str
    name: str

    def __str__(self) -> str:
        return f"{self.path}: {self.scope}() uses undefined global '{self.name}'"


def check_undefined_names(root: str = ".") -> List[UndefinedName]:
    """Names used inside a function that resolve to nothing at module level.

    A missing ``import asyncio`` inside a helper is invisible to both the
    import check and the test suite until that exact branch runs — and a
    branch that only runs when a claim is UNBLOCKED can sit undiscovered
    for as long as the claim is blocked.  One did: it crashed the first
    full live run of the suite, hours after it was written.

    ``symtable`` does the scope analysis, so this is real resolution
    rather than a grep: a name is reported only when the compiler says
    the function reads it as a GLOBAL and the module binds no such name.
    """
    import builtins
    import symtable

    #: Module dunders the interpreter provides; symtable does not see them
    #: as bound, so they read as undefined globals without this.
    provided = {"__file__", "__name__", "__doc__", "__package__",
                "__spec__", "__loader__", "__builtins__"}

    found: List[UndefinedName] = []
    for path in _iter_sources(root):
        try:
            src = open(path, encoding="utf-8").read()
            top = symtable.symtable(src, path, "exec")
        except (SyntaxError, OSError):
            continue
        module_names = {s.get_name() for s in top.get_symbols()
                        if s.is_assigned() or s.is_imported()
                        or s.is_namespace()}

        def _walk(table) -> None:
            for child in table.get_children():
                for sym in child.get_symbols():
                    if not sym.is_global() or sym.is_assigned():
                        continue
                    name = sym.get_name()
                    if (name in module_names or name in provided
                            or hasattr(builtins, name)):
                        continue
                    found.append(UndefinedName(path, child.get_name(), name))
                _walk(child)

        _walk(top)
    return found


# --- the spec against the contract ------------------------------------
#: Words the surface once specified and no longer has.  A spec is a
#: SECOND COPY of a fact that has an owner, and it drifts silently
#: because nothing executes it: `wake=False` was designed and declined,
#: `delivered` was deleted in framework #612, and `role`/`owner` were
#: removed from the roster — and all four sat in a public SURFACE.md
#: reading as a description of what shipped.
#:
#: Runtime was correct throughout.  Only the thing humans read was
#: wrong, so nothing failed and nothing could.
#: Matched as PARAMETERS or FIELDS (``wake=``, ``role:``), never as bare
#: words — the corrected spec has to be able to SAY "there is no `wake`
#: argument" without tripping the guard that keeps it out. A check that
#: cannot tell a mention from a declaration would force the document to
#: stay silent about what it withdrew, which is the opposite of the fix.
WITHDRAWN_FROM_SURFACE = (r"\bwake\s*[=:]", r"\bdelivered\b\s*[,}=:]",
                          r"\brole\s*[,}:]", r"\bowner\s*[,}:]")


@dataclass
class SpecDrift:
    detail: str

    def __str__(self) -> str:
        return f"SURFACE.md: {self.detail}"


def check_spec_matches_contract(spec_path: str = "SURFACE.md") -> List[SpecDrift]:
    """The documented verb signatures must match ``certify/contract.py``.

    The contract is not another document — it is what the probes call and
    assert on, and the claims exercise it against a live framework.  So
    anchoring the spec to it means the spec is checked against something
    that is itself checked, rather than against my memory of a decision.
    """
    from certify import contract

    try:
        text = open(spec_path, encoding="utf-8").read()
    except OSError as exc:
        return [SpecDrift(f"unreadable: {exc}")]

    # The fenced block in §1 that states the two SIGNATURES — matched on
    # the call form `verb(`, not on the bare names, because the plugin
    # scope example (`subagent(tools:[list_siblings,send_to_sibling])`)
    # also mentions both and is not a signature.
    blocks = [b for b in text.split("```")
              if f"{contract.SEND_TO_SIBLING}(" in b
              and f"{contract.LIST_SIBLINGS}(" in b]
    if not blocks:
        return [SpecDrift("no fenced block states both verb signatures")]
    sig = blocks[0]

    drift: List[SpecDrift] = []
    if contract.SIBLING_NAME_ARG not in sig:
        drift.append(SpecDrift(
            f"the send signature does not name {contract.SIBLING_NAME_ARG!r}"))
    for status in sorted(contract.RECEIPT_STATUSES):
        if status not in sig:
            drift.append(SpecDrift(f"receipt status {status!r} is undocumented"))
    for field in (contract.FIELD_YOU, contract.FIELD_SIBLINGS,
                  contract.FIELD_PROFILE_NAME, contract.FIELD_DESCRIPTION):
        if field not in sig:
            drift.append(SpecDrift(f"roster field {field!r} is undocumented"))
    import re
    for pattern in WITHDRAWN_FROM_SURFACE:
        hit = re.search(pattern, sig)
        if hit:
            drift.append(SpecDrift(
                f"the signature still declares {hit.group(0)!r}, which the "
                f"shipped surface does not have"))
    return drift


def check_spec_matches_framework(spec_path: str = "SURFACE.md") -> List[SpecDrift]:
    """The documented parameters must match what the framework publishes.

    A second anchor, added when ``explain plugin --json`` gained a
    ``parameters`` block (framework #615).  It is not a replacement for
    :func:`check_spec_matches_contract`: that one anchors the spec to
    what the PROBES call, which the claims exercise live.  This one
    anchors it to what the FRAMEWORK declares.

    Both matter, and they can disagree — a probe calling an argument the
    framework does not declare would pass the first check and fail this
    one, which is precisely the drift worth catching.

    Returns [] when the framework publishes no parameters, rather than
    inventing a verdict: absent and empty must not read alike here of
    all places.
    """
    import json
    import subprocess
    from certify import contract

    exe = shutil.which("jaato-scaffold")
    if exe is None:
        return []
    out = subprocess.run(
        [exe, "explain", "plugin", contract.HOST_PLUGIN, "--json"],
        capture_output=True, text=True, check=False)
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    tools = {t.get("name"): t for t in doc.get("tools", [])}
    tool = tools.get(contract.SEND_TO_SIBLING)
    if not tool:
        return []
    params = (tool.get("parameters") or {}).get("properties")
    if not params:
        return []          # nothing published — assert nothing

    try:
        text = open(spec_path, encoding="utf-8").read()
    except OSError:
        return []
    blocks = [b for b in text.split("```")
              if f"{contract.SEND_TO_SIBLING}(" in b]
    if not blocks:
        return [SpecDrift("no fenced block states the send signature")]
    sig = blocks[0]

    drift: List[SpecDrift] = []
    for name in params:
        if name not in sig:
            drift.append(SpecDrift(
                f"the framework declares parameter {name!r} and the spec "
                f"does not document it"))
    return drift


def check_inject_vocabulary_matches_framework() -> List[SpecDrift]:
    """``contract.INJECT_*`` must match ``shared.message_delivery``.

    The contract is a SECOND COPY of a fact the framework owns, and a
    second copy rots unless something executes the comparison.  The copy
    that rots is always the one that cannot fail — so this compares the
    two sets rather than trusting that a human kept them aligned.

    Anchored to the framework MODULE, not to prose about it: the module
    is what the daemon runs, so a rename there breaks this immediately
    instead of at the next live run that reads a status no longer sent.

    Returns [] when the module is absent (a framework predating #619)
    rather than inventing a verdict — a missing module and a mismatched
    one are different facts and must not render alike.
    """
    from certify import contract, harness

    path = os.path.join(harness.framework_root(), "jaato-server",
                        "shared", "message_delivery.py")
    if not os.path.isfile(path):
        return []

    ns: dict = {}
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    # Read the constants without importing: the module imports framework
    # internals, and this repository must not.
    for line in source.splitlines():
        if re.match(r"^[A-Z_]+ = ", line):
            exec(line, {"frozenset": frozenset}, ns)   # noqa: S102 — literals only

    drifts: List[SpecDrift] = []
    theirs = {k: v for k, v in ns.items() if isinstance(v, str)}
    mine = {
        "QUEUED": contract.INJECT_QUEUED,
        "ACCEPTED": contract.INJECT_ACCEPTED,
        "TERMINATED": contract.INJECT_TERMINATED,
        "NO_SESSION": contract.INJECT_NO_SESSION,
        "UNREACHABLE": contract.INJECT_UNREACHABLE,
        "BUSY": contract.INJECT_BUSY,
    }
    for name, value in mine.items():
        if name not in theirs:
            drifts.append(SpecDrift(
                f"contract pins {name}={value!r} but message_delivery.py "
                f"no longer defines it"))
        elif theirs[name] != value:
            drifts.append(SpecDrift(
                f"contract pins {name}={value!r}; the framework says "
                f"{theirs[name]!r}"))

    delivered = ns.get("DELIVERED")
    if delivered is not None and set(delivered) != set(contract.INJECT_DELIVERED):
        drifts.append(SpecDrift(
            f"DELIVERED disagrees: contract {sorted(contract.INJECT_DELIVERED)} "
            f"vs framework {sorted(delivered)}"))
    return drifts
