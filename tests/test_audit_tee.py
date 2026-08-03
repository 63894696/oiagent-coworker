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
#   - New file; no upstream counterpart. Unit tests for the W3-3 two-leg
#     audit tee (FanoutAuditSink) contract and failure isolation.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Tests for oiagent_coworker.permissions.audit_tee -- W3-3 unit tests.

Covers the FanoutAuditSink two-leg tee contract: AuditSink Protocol
conformance, identity of the fanned-out envelope, primary-before-
secondary ordering, per-leg failure isolation (neither leg's exception
propagates; WARNING logged), construction-time TypeError on non-callable
legs, and acceptance by the sealed OIagentCoworkerAuditFacade.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No P2_10 import: the external leg is always a stub callable.
    - No vault-path resolution; no real clock read in tests.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.audit import (
    AuditDecision,
    AuditSink,
    OIagentCoworkerAuditFacade,
)
from oiagent_coworker.permissions.audit_tee import FanoutAuditSink


def _decision() -> AuditDecision:
    return AuditDecision(
        kind="permission",
        timestamp=datetime(2026, 8, 3, tzinfo=UTC),
    )


class _RecordingLeg:
    """Stub P2_10-style capturing sink (records received envelopes)."""

    def __init__(self) -> None:
        self.received: list[AuditDecision] = []

    def __call__(self, decision: AuditDecision) -> None:
        self.received.append(decision)


class _RaisingLeg:
    """Sink stub that always raises when invoked."""

    def __init__(self, label: str = "boom") -> None:
        self._label = label
        self.calls = 0

    def __call__(self, decision: AuditDecision) -> None:
        self.calls += 1
        raise RuntimeError(self._label)


# ----------------------------------------------------------------------
# 1. Tee conforms to AuditSink Protocol (@runtime_checkable)
# ----------------------------------------------------------------------


def test_01_tee_conforms_to_auditsink_protocol() -> None:
    tee = FanoutAuditSink(primary=_RecordingLeg(), secondary=_RecordingLeg())
    assert isinstance(tee, AuditSink)


# ----------------------------------------------------------------------
# 2. Both legs receive the SAME AuditDecision (identity)
# ----------------------------------------------------------------------


def test_02_both_legs_receive_same_decision_identity() -> None:
    primary = _RecordingLeg()
    secondary = _RecordingLeg()
    tee = FanoutAuditSink(primary=primary, secondary=secondary)
    decision = _decision()
    tee(decision)
    assert len(primary.received) == 1
    assert len(secondary.received) == 1
    assert primary.received[0] is decision
    assert secondary.received[0] is decision


# ----------------------------------------------------------------------
# 3. Primary invoked BEFORE secondary (record call order)
# ----------------------------------------------------------------------


def test_03_primary_invoked_before_secondary() -> None:
    order: list[str] = []

    def primary(decision: AuditDecision) -> None:
        order.append("primary")

    def secondary(decision: AuditDecision) -> None:
        order.append("secondary")

    tee = FanoutAuditSink(primary=primary, secondary=secondary)
    tee(_decision())
    assert order == ["primary", "secondary"]


# ----------------------------------------------------------------------
# 4. Primary raises -> secondary still receives + WARNING + no propagate
# ----------------------------------------------------------------------


def test_04_primary_raises_secondary_still_receives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secondary = _RecordingLeg()
    tee = FanoutAuditSink(primary=_RaisingLeg("primary-boom"), secondary=secondary)
    decision = _decision()
    with caplog.at_level(logging.WARNING):
        tee(decision)  # must NOT raise
    assert len(secondary.received) == 1
    assert secondary.received[0] is decision
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ----------------------------------------------------------------------
# 5. Secondary raises -> primary already received + WARNING + no propagate
# ----------------------------------------------------------------------


def test_05_secondary_raises_primary_already_received(
    caplog: pytest.LogCaptureFixture,
) -> None:
    primary = _RecordingLeg()
    tee = FanoutAuditSink(primary=primary, secondary=_RaisingLeg("secondary-boom"))
    decision = _decision()
    with caplog.at_level(logging.WARNING):
        tee(decision)  # must NOT raise
    assert len(primary.received) == 1
    assert primary.received[0] is decision
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ----------------------------------------------------------------------
# 6. Both raise -> neither propagates; two WARNINGs
# ----------------------------------------------------------------------


def test_06_both_raise_neither_propagates_two_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tee = FanoutAuditSink(
        primary=_RaisingLeg("primary-boom"),
        secondary=_RaisingLeg("secondary-boom"),
    )
    with caplog.at_level(logging.WARNING):
        tee(_decision())  # must NOT raise
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


# ----------------------------------------------------------------------
# 7. Non-callable primary -> TypeError at construction
# ----------------------------------------------------------------------


def test_07_non_callable_primary_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        FanoutAuditSink(primary=object(), secondary=_RecordingLeg())


# ----------------------------------------------------------------------
# 8. Non-callable secondary -> TypeError at construction
# ----------------------------------------------------------------------


def test_08_non_callable_secondary_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        FanoutAuditSink(primary=_RecordingLeg(), secondary=object())


# ----------------------------------------------------------------------
# 9. Tee accepted by OIagentCoworkerAuditFacade(sink=tee)
# ----------------------------------------------------------------------


def test_09_tee_accepted_by_facade() -> None:
    tee = FanoutAuditSink(primary=_RecordingLeg(), secondary=_RecordingLeg())
    facade = OIagentCoworkerAuditFacade(sink=tee)
    assert facade is not None
