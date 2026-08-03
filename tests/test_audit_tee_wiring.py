# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W3-3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Wiring + observation-window
#     acceptance tests for the W3-3 audit tee -> stream -> analyzer chain.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Tests for W3-3 audit-tee wiring + observation-window acceptance.

Constructs the full deployer wiring recipe on ``tmp_path`` with a STUB
P2_10 capturing sink (NEVER imports the real P2_10 -- it is outside this
repo), then drives a REAL ``OIagentCoworkerPermissionEngine.check()`` to
prove the chain tee -> file -> reader -> analyzer. The analyzer's ``now``
is injected (the analyzer stays pure); only the anchor VALUE in test_12
comes from the wall clock, because the engine stamps the heartbeat
envelope with ``datetime.now(UTC)`` (engine.py:333) and the anchor must
be at/after it for ``total_compared >= 1`` to hold.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No P2_10 import: the external leg is always a stub callable.
    - No vault-path resolution; all roots are tmp_path fixtures.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.audit import (
    AuditDecision,
    OIagentCoworkerAuditFacade,
)
from oiagent_coworker.permissions.audit_stream import (
    AuditStreamSink,
    read_audit_stream,
)
from oiagent_coworker.permissions.audit_tee import FanoutAuditSink
from oiagent_coworker.permissions.consistency import analyze_consistency
from oiagent_coworker.permissions.engine import (
    Action,
    OIagentCoworkerPermissionEngine,
    PermissionContext,
    PermissionMode,
    Verdict,
)


class _StubP2_10:
    """Stub P2_10 capturing sink (external leg; NEVER the real P2_10)."""

    def __init__(self) -> None:
        self.received: list[AuditDecision] = []

    def __call__(self, decision: AuditDecision) -> None:
        self.received.append(decision)


def _build_engine(tmp_path: Path) -> tuple[
    OIagentCoworkerPermissionEngine,
    _StubP2_10,
    Path,
]:
    """Construct the full deployer wiring recipe on tmp_path.

    Returns ``(engine, stub_p2_leg, stream_path)``.
    """
    stream_path = tmp_path / "logs" / "audit" / "permission_stream.jsonl"
    stream_leg = AuditStreamSink(stream_path)      # durable local leg (first)
    p2_leg = _StubP2_10()                          # external leg (second)
    tee = FanoutAuditSink(primary=stream_leg, secondary=p2_leg)
    facade = OIagentCoworkerAuditFacade(sink=tee)
    engine = OIagentCoworkerPermissionEngine(
        workspace_root=tmp_path,
        audit_sink=facade.for_engine(),
    )
    return engine, p2_leg, stream_path


def _drive_check(engine: OIagentCoworkerPermissionEngine) -> Verdict:
    return engine.check(
        Action(kind="read_file", target="hello.txt"),
        PermissionContext(mode=PermissionMode.SYNC),
    )


# ----------------------------------------------------------------------
# 10. Full recipe fan-out: one real engine check() reaches both the stub
#     P2_10 and the stream file.
# ----------------------------------------------------------------------


def test_10_full_recipe_fanout_reaches_both_legs(tmp_path: Path) -> None:
    engine, p2_leg, stream_path = _build_engine(tmp_path)
    _drive_check(engine)
    # External P2_10 leg received the heartbeat.
    assert len(p2_leg.received) == 1
    assert p2_leg.received[0].kind == "permission"
    # Durable local stream leg wrote the same heartbeat to disk.
    assert stream_path.exists()
    assert len(stream_path.read_text(encoding="utf-8").splitlines()) == 1


# ----------------------------------------------------------------------
# 11. Stream file round-trips: read_audit_stream yields the decision.
# ----------------------------------------------------------------------


def test_11_stream_file_roundtrips(tmp_path: Path) -> None:
    engine, _p2_leg, stream_path = _build_engine(tmp_path)
    _drive_check(engine)
    envelopes, stats = read_audit_stream(stream_path)
    envelopes = list(envelopes)
    assert stats.envelopes_yielded >= 1
    assert len(envelopes) >= 1
    assert envelopes[0]["kind"] == "permission"


# ----------------------------------------------------------------------
# 12. Analyzer consumes it: total_compared >= 1 (window-start marker).
#     The analyzer's `now` is injected (analyzer stays pure); only the
#     anchor VALUE comes from the wall clock (see inline note below).
# ----------------------------------------------------------------------


def test_12_analyzer_consumes_stream_total_compared(tmp_path: Path) -> None:
    engine, _p2_leg, stream_path = _build_engine(tmp_path)
    _drive_check(engine)
    fixed_now = datetime.now(UTC)  # engine stamps the heartbeat with datetime.now(UTC) (engine.py:333); anchor the analyzer at/after it. The analyzer stays pure (now is injected); only the anchor value comes from the wall clock.
    envelopes, _stats = read_audit_stream(stream_path)
    report = analyze_consistency(
        envelopes,
        now=fixed_now,
        window=timedelta(days=7),
    )
    assert report.total_compared >= 1


# ----------------------------------------------------------------------
# 13. Stream-leg failure (unwritable path) still delivers to stub P2_10
#     and does NOT break check().
# ----------------------------------------------------------------------


def test_13_stream_leg_failure_still_delivers_to_p2(tmp_path: Path) -> None:
    # A stream path whose parent is a FILE (unwritable as a directory):
    # AuditStreamSink.mkdir will raise OSError -> primary leg fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    stream_path = blocker / "logs" / "audit" / "permission_stream.jsonl"

    stream_leg = AuditStreamSink(stream_path)
    p2_leg = _StubP2_10()
    tee = FanoutAuditSink(primary=stream_leg, secondary=p2_leg)
    facade = OIagentCoworkerAuditFacade(sink=tee)
    engine = OIagentCoworkerPermissionEngine(
        workspace_root=tmp_path,
        audit_sink=facade.for_engine(),
    )

    verdict = _drive_check(engine)  # must NOT raise
    # check() completed and the external P2_10 leg still received it.
    assert verdict is not None
    assert len(p2_leg.received) == 1
    assert p2_leg.received[0].kind == "permission"
