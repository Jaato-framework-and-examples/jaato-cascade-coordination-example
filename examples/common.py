"""The small set of facade calls a cascade driver actually needs.

Derived from ``certify/harness.py`` by deletion.  What stayed is the part
that is hard to rediscover: the client knobs, the two path rules, and the
one route by which a tool's result reaches a consumer.  What went is
everything that exists to make a claim FAIL — leashes, three-state
verdicts, reversion hooks.  An example wants none of that.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Dict, List, Optional

from jaato_sdk import ClientType, EventType, IPCClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Read-only framework config.  Points AT ``.jaato`` — profile discovery
#: scans ``<config_root>/profiles``, so the workspace root finds nothing,
#: and finds it *without an error*, because an empty profile set is legal.
CONFIG_ROOT = os.path.join(REPO, ".jaato")
ENV_FILE = os.path.join(REPO, ".env")

#: Short on purpose: AF_UNIX caps a socket path at 108 bytes and fails
#: opaquely past it.
SOCKET = os.environ.get("JAATO_IPC_SOCKET", "/tmp/jc.sock")


def new_client() -> IPCClient:
    """The known-good knobs, and why each one is here."""
    return IPCClient(
        SOCKET,
        client_type=ClientType.API,   # keeps signal_completion; TERMINAL/WEB strip it
        auto_start=False,             # start your own daemon; see README
        env_file=ENV_FILE,            # never None — the handshake crashes on None
        workspace_path=REPO,
        config_root=CONFIG_ROOT,
    )


def new_cascade_id() -> str:
    """One id per cascade.  It buys three things and only three:

    a single budget ceiling, sibling addressing by name, and one
    observable event stream.
    """
    return uuid.uuid4().hex


async def start_session(client: Any, *, sibling_name: str, profile: str,
                        agent: str, cascade_id: str) -> str:
    """Create one addressable session in the cascade.

    ``sibling_name`` is the address siblings use.  It is a slug
    (``^[a-z0-9][a-z0-9_-]{0,31}$``), unique within the cascade, and
    rejected at session.new if it is neither.
    """
    sid = await client.create_session(
        profile=profile, agent=agent, sibling_name=sibling_name,
        cascade_driver_id=cascade_id, timeout=60.0,
    )
    if not sid:
        raise RuntimeError(f"session '{sibling_name}' did not start")
    return sid


async def await_completion(client: Any, session_id: str,
                           timeout: float = 900.0) -> Optional[Dict]:
    """Wait for ONE session's typed completion payload.

    A session's only exit is ``signal_completion``, and declaring a
    ``completion_payload_schema`` is what exposes that tool at all.  The
    payload is validated against the schema before it reaches here, so a
    driver can branch on it without re-checking its shape.

    ``session_id`` is not optional, and that is the point.  An earlier
    version took the FIRST AgentCompletedEvent to arrive — which, with a
    writer and a coder both live, is whichever finished first.  The run
    that exposed it printed

        writer: stopped

    where `stopped` is the CODER's completion vocabulary; the writer's
    schema cannot even produce that word.  The driver had read one
    sibling's exit as another's and carried on.

    Correlating on the session the event names is what makes a
    concurrent cascade observable at all.  Every event has carried
    ``session_id`` since jaato #603.
    """
    done: asyncio.Future = asyncio.get_event_loop().create_future()

    def _on(ev: Any) -> None:
        if getattr(ev, "session_id", None) != session_id:
            return
        if not done.done():
            done.set_result(getattr(ev, "payload", None) or {})

    unsub = client.subscribe(EventType.AGENT_COMPLETED, _on)
    try:
        return await asyncio.wait_for(done, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        unsub()


async def tool_available(client: Any, name: str) -> bool:
    """Is a tool present in this framework build?

    Asked through the scaffold CLI rather than guessed, so the example
    can say WHICH verb is missing instead of failing mid-run.
    """
    import json
    import shutil
    import subprocess
    exe = shutil.which("jaato-scaffold")
    if exe is None:
        return False
    out = subprocess.run([exe, "explain", "plugin", "subagent", "--json"],
                         capture_output=True, text=True, check=False)
    try:
        doc = json.loads(out.stdout)
    except json.JSONDecodeError:
        return False
    return any(t.get("name") == name for t in doc.get("tools", []))
