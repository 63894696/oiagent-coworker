# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_stage_confirm.py (new file)
#   Upstream commit:  not present (W2-5.2 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W2-5.2; tests the stage-confirm gate
#     (skills upload/invoke -> duck-typed PolicyGate.check()).
#   - 11 tests, no external deps beyond pytest. Stub gates record
#     check() calls and return canned Verdicts; real Verdict /
#     PermissionContext / Action from permissions.engine are used so
#     the __post_init__ invariant is exercised end-to-end.
#   - Mirrors the fixture patterns of test_policy_gate.py: stub engines,
#     list-buffer record capture, _REPO_ROOT sys.path idiom.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Tests for oiagent_coworker.skills.stage_confirm -- W2-5.2 acceptance.

Covers the stage-confirm contract: Verdict -> StageConfirmResult
mapping (allow / require-approval / hard-deny), the sole sanctioned
invoke path, action-shape construction, constructor validation, default
vs custom ctx_factory, and fail-closed gate-exception propagation.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No ``oiagent.approval`` / ``oiagent.policy`` imports; gates are
      duck-typed stubs.
    - No Tauri dialog; confirm UX is out of scope for these tests.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.engine import (
    Action,
    PermissionContext,
    PermissionMode,
    Verdict,
)
from oiagent_coworker.skills.stage_confirm import (
    OIagentCoworkerStageConfirm,
    StageConfirmDenied,
    StageConfirmResult,
    build_upload_action,
    invoke_skill_with_confirm,
)

# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------


class _StubGate:
    """Duck-typed gate stub: records check() calls, returns a canned Verdict."""

    def __init__(self, verdict: Verdict) -> None:
        self._verdict = verdict
        self.calls: list[tuple[Action, PermissionContext]] = []

    def check(self, action: Action, ctx: PermissionContext) -> Verdict:
        self.calls.append((action, ctx))
        return self._verdict


class _RaisingGate:
    """Gate stub that always raises from check() (fail-closed probe)."""

    def __init__(self, message: str = "gate exploded") -> None:
        self._message = message
        self.calls = 0

    def check(self, action: Action, ctx: PermissionContext) -> Verdict:
        self.calls += 1
        raise RuntimeError(self._message)


def _allow_verdict() -> Verdict:
    return Verdict(
        allow=True,
        mode=PermissionMode.SYNC,
        reason="stub gate allows",
        risk_level="read",
        requires_approval=False,
    )


def _approval_verdict() -> Verdict:
    return Verdict(
        allow=False,
        mode=PermissionMode.SYNC,
        reason="stub gate requires user approval",
        risk_level="write",
        requires_approval=True,
    )


def _hard_deny_verdict() -> Verdict:
    return Verdict(
        allow=False,
        mode=PermissionMode.SYNC,
        reason="stub gate hard-denies (read-only compaction window)",
        risk_level="write",
        requires_approval=False,
    )


@pytest.fixture
def allowing_gate() -> _StubGate:
    return _StubGate(_allow_verdict())


@pytest.fixture
def confirm(allowing_gate: _StubGate) -> OIagentCoworkerStageConfirm:
    return OIagentCoworkerStageConfirm(allowing_gate)


# ---------------------------------------------------------------------------
# Acceptance
# ---------------------------------------------------------------------------


def test_stage_confirm_via_policygate() -> None:
    """ACCEPTANCE: denying gate -> StageConfirmDenied, invoke NOT run;
    allowing gate -> invoke runs, gate consulted exactly once.

    The invoke path uses confirm_invoke, so the Action kind reaching the
    gate is "skill_invoke" (the upload half is covered separately).
    """
    # Denying half.
    denying_gate = _StubGate(_hard_deny_verdict())
    confirm = OIagentCoworkerStageConfirm(denying_gate)
    invoke_calls: list[str] = []

    with pytest.raises(StageConfirmDenied):
        invoke_skill_with_confirm(
            confirm,
            "demo_skill",
            target="skills/demo",
            invoke=lambda: invoke_calls.append("ran"),
        )
    assert invoke_calls == []
    assert len(denying_gate.calls) == 1
    denied_action, _ = denying_gate.calls[0]
    assert denied_action.kind == "skill_invoke"

    # Allowing half.
    allowing_gate = _StubGate(_allow_verdict())
    confirm = OIagentCoworkerStageConfirm(allowing_gate)
    out = invoke_skill_with_confirm(
        confirm,
        "demo_skill",
        target="skills/demo",
        invoke=lambda: "invoke-result",
    )
    assert out == "invoke-result"
    assert len(allowing_gate.calls) == 1
    allowed_action, _ = allowing_gate.calls[0]
    assert allowed_action.kind == "skill_invoke"
    assert allowed_action.metadata["skill_name"] == "demo_skill"
    assert allowed_action.metadata["stage"] == "stage_confirm"


# ---------------------------------------------------------------------------
# Action shape
# ---------------------------------------------------------------------------


def test_build_upload_action_shape() -> None:
    action = build_upload_action("demo_skill", target="skills/demo")
    assert action.kind == "upload"
    assert action.target == "skills/demo"
    assert action.metadata["skill_name"] == "demo_skill"
    assert action.metadata["stage"] == "stage_confirm"

    # Caller metadata merges; caller keys win on collision.
    action = build_upload_action(
        "demo_skill",
        target="skills/demo",
        metadata={"stage": "caller-override", "extra": 42},
    )
    assert action.metadata["stage"] == "caller-override"
    assert action.metadata["skill_name"] == "demo_skill"
    assert action.metadata["extra"] == 42


def test_confirm_invoke_uses_skill_invoke_kind(
    allowing_gate: _StubGate,
) -> None:
    confirm = OIagentCoworkerStageConfirm(allowing_gate)
    result = confirm.confirm_invoke("demo_skill", target="skills/demo")
    assert result.action.kind == "skill_invoke"
    action, _ = allowing_gate.calls[0]
    assert action.kind == "skill_invoke"


# ---------------------------------------------------------------------------
# Verdict -> StageConfirmResult mapping
# ---------------------------------------------------------------------------


def test_verdict_allow_maps_to_allowed_true(allowing_gate: _StubGate) -> None:
    confirm = OIagentCoworkerStageConfirm(allowing_gate)
    result = confirm.confirm_upload("demo_skill", target="skills/demo")
    assert result.allowed is True
    assert result.verdict is allowing_gate._verdict  # passed verbatim
    assert isinstance(result, StageConfirmResult)
    assert result.action.kind == "upload"


def test_verdict_requires_approval_maps_to_denied_with_approval_flag() -> None:
    gate = _StubGate(_approval_verdict())
    confirm = OIagentCoworkerStageConfirm(gate)
    result = confirm.confirm_upload("demo_skill", target="skills/demo")
    assert result.allowed is False
    assert result.verdict.requires_approval is True

    with pytest.raises(StageConfirmDenied) as excinfo:
        invoke_skill_with_confirm(
            confirm,
            "demo_skill",
            target="skills/demo",
            invoke=lambda: pytest.fail("invoke must not run"),
        )
    assert excinfo.value.result.verdict.requires_approval is True


def test_verdict_hard_deny_maps_to_denied() -> None:
    gate = _StubGate(_hard_deny_verdict())
    confirm = OIagentCoworkerStageConfirm(gate)
    result = confirm.confirm_upload("demo_skill", target="skills/demo")
    assert result.allowed is False
    assert result.verdict.requires_approval is False

    with pytest.raises(StageConfirmDenied) as excinfo:
        invoke_skill_with_confirm(
            confirm,
            "demo_skill",
            target="skills/demo",
            invoke=lambda: pytest.fail("invoke must not run"),
        )
    assert excinfo.value.result.verdict.requires_approval is False


# ---------------------------------------------------------------------------
# Constructor + ctx_factory
# ---------------------------------------------------------------------------


def test_constructor_rejects_gate_without_check() -> None:
    with pytest.raises(TypeError):
        OIagentCoworkerStageConfirm(object())
    with pytest.raises(TypeError):
        OIagentCoworkerStageConfirm(None)

    class _NonCallableCheck:
        check = "not callable"

    with pytest.raises(TypeError):
        OIagentCoworkerStageConfirm(_NonCallableCheck())

    # Non-callable ctx_factory also rejected.
    with pytest.raises(TypeError):
        OIagentCoworkerStageConfirm(_StubGate(_allow_verdict()), ctx_factory=42)


def test_ctx_factory_default_is_sync(allowing_gate: _StubGate) -> None:
    confirm = OIagentCoworkerStageConfirm(allowing_gate)
    confirm.confirm_upload("demo_skill", target="skills/demo")
    _, ctx = allowing_gate.calls[0]
    assert ctx.mode is PermissionMode.SYNC


def test_custom_ctx_factory_used(allowing_gate: _StubGate) -> None:
    custom_ctx = PermissionContext(
        mode=PermissionMode.PLAN,
        task_id="task-123",
        user_id="user-abc",
        session_id="sess-xyz",
    )
    confirm = OIagentCoworkerStageConfirm(
        allowing_gate, ctx_factory=lambda: custom_ctx,
    )
    confirm.confirm_upload("demo_skill", target="skills/demo")
    _, ctx = allowing_gate.calls[0]
    assert ctx is custom_ctx
    assert ctx.mode is PermissionMode.PLAN
    assert ctx.task_id == "task-123"


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_gate_exception_propagates() -> None:
    """A raising gate propagates; it is NOT swallowed into a silent allow."""
    gate = _RaisingGate()
    confirm = OIagentCoworkerStageConfirm(gate)

    with pytest.raises(RuntimeError, match="gate exploded"):
        confirm.confirm_upload("demo_skill", target="skills/demo")

    with pytest.raises(RuntimeError, match="gate exploded"):
        invoke_skill_with_confirm(
            confirm,
            "demo_skill",
            target="skills/demo",
            invoke=lambda: pytest.fail("invoke must not run"),
        )
    assert gate.calls == 2


def test_contradictory_duck_typed_verdict_fails_closed() -> None:
    """A duck-typed verdict with allow=True AND requires_approval=True
    (bypassing Verdict.__post_init__) must still map to allowed=False:
    the `allow and not requires_approval` mapping fails closed even for
    a contradictory verdict.
    """
    contradictory = types.SimpleNamespace(
        allow=True,
        requires_approval=True,
        reason="contradictory duck-typed verdict",
    )
    gate = _StubGate(contradictory)  # type: ignore[arg-type]
    confirm = OIagentCoworkerStageConfirm(gate)

    result = confirm.confirm_upload("demo_skill", target="skills/demo")
    assert result.allowed is False

    invoke_calls: list[str] = []
    with pytest.raises(StageConfirmDenied):
        invoke_skill_with_confirm(
            confirm,
            "demo_skill",
            target="skills/demo",
            invoke=lambda: invoke_calls.append("ran"),
        )
    assert invoke_calls == []
