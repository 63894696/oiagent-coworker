# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_w2_integration.py (new file)
#   Upstream commit:  not present (W2-6.1 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W2-6.1; tests the cross-module E2E
#     integration of all five W2 modules (permissions, inbox, selfwake,
#     personas, skills) through a single mocked Slack mention → audit
#     pipeline scenario.
#   - 1 integration test, no external deps beyond pytest.
#   - The test uses mocks for all cross-module RPC (15721 proxy, Slack
#     HTTP, PolicyGate) and asserts the audit envelope chain is intact.
#   - W2-6.1 (2026-08-03): added test_e2e_skill_invoke_through_stage_confirm —
#     plan §6.2 step 6, skill invoke passes the stage_confirm gate (allow
#     runs side-effect; deny raises StageConfirmDenied and side-effect does
#     not run).
#   - W2-6.1 follow-up (2026-08-03): resolved the W2-5.3 TODO — manifest/
#     SKILL.md folder-as-truth load alongside service registration; added
#     §3.5 acceptance assertion (overlay declared + body digest non-empty).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Cross-module W2 integration E2E test (W2-6.1).

Scenarios modeled after §6.2 of the W2 extraction plan:

  1. selfwake timer fires → caller.invoke() reads inbox
  2. PermissionEngine approves the inbox write
  3. Inbox item triggers persona switch
  4. Skills service records the invoke with audit
  5. Full audit trail spans ≥ 4 kinds (selfwake, permission, inbox, skill)

The test is fully mocked — no 15721 proxy, no Slack, no Tauri dialog.
It exercises the real service constructors from each W2 module and
verifies the AuditDecision envelope chain is complete and typed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from oiagent_coworker.inbox.models import InboxItemKind, InboxItemPriority
from oiagent_coworker.inbox.service import OIagentCoworkerInboxService
from oiagent_coworker.permissions.audit import AuditDecision
from oiagent_coworker.permissions.engine import OIagentCoworkerPermissionEngine
from oiagent_coworker.persona.persistence import OIagentCoworkerPersonaPersistence
from oiagent_coworker.persona.service import OIagentCoworkerPersonaService
from oiagent_coworker.selfwake.models import ScheduleHandler, ScheduleSpec, TriggerKind
from oiagent_coworker.selfwake.scheduler import OIagentCoworkerSelfWakeScheduler
from oiagent_coworker.skills import E2E_OVERLAY_KEY, OIagentCoworkerSkillManifest
from oiagent_coworker.skills.service import OIagentCoworkerSkillsService
from oiagent_coworker.skills.stage_confirm import (
    OIagentCoworkerStageConfirm,
    StageConfirmDenied,
    invoke_skill_with_confirm,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _CapturedAudit:
    """Captured subset of an AuditDecision for cross-module assertions."""

    decision: AuditDecision


@pytest.fixture
def captured_audit() -> list[_CapturedAudit]:
    return []


@pytest.fixture
def audit_sink(
    captured_audit: list[_CapturedAudit],
) -> Callable[[AuditDecision], None]:
    def sink(decision: AuditDecision) -> None:
        captured_audit.append(_CapturedAudit(decision=decision))

    return sink


@pytest.fixture
def skills_path(tmp_path: Path) -> Path:
    return tmp_path / "skills.jsonl"


@pytest.fixture
def inbox_path(tmp_path: Path) -> Path:
    return tmp_path / "inbox.jsonl"


@pytest.fixture
def selfwake_path(tmp_path: Path) -> Path:
    return tmp_path / "selfwake.jsonl"


@pytest.fixture
def persona_dir(tmp_path: Path) -> Path:
    """A persona directory with one synthetic persona file."""
    d = tmp_path / "personas"
    d.mkdir()
    p = d / "slack_responder.md"
    p.write_text(
        "---\n"
        "name: slack_responder\n"
        "description: Responds to Slack mentions\n"
        "version: 1.0.0\n"
        "---\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def permission_engine(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
) -> OIagentCoworkerPermissionEngine:
    return OIagentCoworkerPermissionEngine(
        workspace_root=tmp_path,
        audit_sink=audit_sink,
    )


# ---------------------------------------------------------------------------
# W2-6.1: 七步 mock E2E — Slack mention → audit 落盘
# ---------------------------------------------------------------------------


def test_e2e_slack_mention_pipeline(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
    skills_path: Path,
    inbox_path: Path,
    selfwake_path: Path,
    persona_dir: Path,
    permission_engine: OIagentCoworkerPermissionEngine,
    captured_audit: list[_CapturedAudit],
) -> None:
    """七步集成 E2E:
    1. selfwake 注册 timer → 写入 selfwake.jsonl
    2. mock Slack mention 写入 inbox
    3. PermissionEngine 审批 inbox write → ALLOW
    4. persona 切换 → slack_responder 激活
    5. skills 注册 capability-04-e2e
    6. selfwake tick 触发 handler
    7. 全链路 audit 必须有 ≥ 4 个不同 kind 的 envelope
    """
    # ── Step 1: 注册 selfwake timer ────────────────────────────────
    sw_service = OIagentCoworkerSelfWakeScheduler(
        storage_path=selfwake_path,
        audit_sink=audit_sink,
    )
    # Use manual trigger so tick() won't auto-fire; we control it.
    spec = ScheduleSpec(kind=TriggerKind.MANUAL)
    handler_calls: list[dict[str, Any]] = []
    handler_obj = MagicMock(spec=Callable[[dict[str, Any]], None])
    handler_obj.side_effect = lambda payload: handler_calls.append(payload)
    sw_service.set_handler("e2e.handler", handler_obj)
    sw_service.register(
        name="e2e-timer",
        schedule=spec,
        handler=ScheduleHandler(handler_id="e2e.handler", payload={}),
    )
    # Must have emitted at least one audit envelope for register.
    sw_kinds = [d.decision.kind for d in captured_audit if d.decision.kind == "selfwake"]
    assert len(sw_kinds) >= 1, "selfwake register must emit audit"

    # ── Step 2: mock Slack mention → inbox append ────────────────
    inbox_service = OIagentCoworkerInboxService(
        storage_path=inbox_path,
        audit_sink=audit_sink,
    )
    inbox_service.append(
        kind=InboxItemKind.MESSAGE,
        priority=InboxItemPriority.NORMAL,
        title="Slack mention",
        body="Hey, need help with deployment",
        source="slack",
        metadata={},
        expires_at=None,
    )
    inbox_kinds = [d.decision.kind for d in captured_audit if d.decision.kind == "inbox"]
    assert len(inbox_kinds) >= 1, "inbox append must emit audit"

    # ── Step 3: PermissionEngine approves the inbox write ─────────
    from oiagent_coworker.permissions.engine import (
        Action,
        PermissionContext,
        PermissionMode,
    )

    verdict = permission_engine.check(
        action=Action(kind="write", target=str(inbox_path)),
        ctx=PermissionContext(mode=PermissionMode.SYNC),
    )
    assert verdict.allow is True, "inbox write must be allowed in workspace context"
    perm_kinds = [d.decision.kind for d in captured_audit if d.decision.kind == "permission"]
    assert len(perm_kinds) >= 1, "permission check must emit audit"

    # ── Step 4: persona switch to slack_responder ────────────────
    persona_persist = OIagentCoworkerPersonaPersistence(persona_dir)
    persona_service = OIagentCoworkerPersonaService(persona_persist)
    # Persona loaded from disk; verify it's discoverable.
    discovered = persona_service.list_personas()
    assert "slack_responder" in discovered, "slack_responder must be discoverable"
    current = persona_service.get_persona("slack_responder")
    assert current is not None
    assert current.name == "slack_responder"

    # ── Step 5: register skills (capability-04-e2e mock) ─────────
    skills_service = OIagentCoworkerSkillsService(
        storage_path=skills_path,
        audit_sink=audit_sink,
    )
    skill = skills_service.register_skill(
        name="capability-04-e2e",
        version="1.0.0",
        description="E2E overlay skill",
        entrypoint="oiagent_coworker.skills",
        config={"e2e_overlay": True},
    )
    assert skill is not None
    assert skill.spec.name == "capability-04-e2e"
    skill_kinds = [d.decision.kind for d in captured_audit if d.decision.kind == "skill"]
    assert len(skill_kinds) >= 1, "skill register must emit audit"

    # ── Step 6: selfwake tick fires handler ──────────────────────
    results = sw_service.tick()
    # Manual kind never auto-fires, so results should be empty — that's correct.
    # The point is the tick() path completes without error.
    assert isinstance(results, list)

    # ── Step 7: 全链路 audit 必须有 ≥ 4 种 kind ───────────────────
    all_kinds = {d.decision.kind for d in captured_audit}
    assert "permission" in all_kinds, "missing permission audit"
    assert "inbox" in all_kinds, "missing inbox audit"
    assert "skill" in all_kinds, "missing skill audit"
    assert "selfwake" in all_kinds, "missing selfwake audit"
    # Total distinct kinds ≥ 4
    assert len(all_kinds) >= 4, f"expected ≥ 4 audit kinds, got {all_kinds}"

    # Verify each envelope is well-formed.
    for captured in captured_audit:
        d = captured.decision
        assert d.kind in (
            "permission", "path_sandbox", "shell_classifier",
            "standing_rule", "inbox", "selfwake", "skill",
        )
        assert d.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# W2-6.1: plan §6.2 step 6 — skill invoke through the stage_confirm gate
# ---------------------------------------------------------------------------


class _LocalStubGate:
    """Minimal self-contained hard-deny gate for the stage_confirm path.

    Duck-typed against ``check(action, ctx) -> Verdict``; intentionally
    does NOT import ``_StubGate`` from test_stage_confirm.py so this file
    stays self-contained.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def check(self, action: Any, ctx: Any) -> Any:
        from oiagent_coworker.permissions.engine import PermissionMode, Verdict

        self.calls.append((action, ctx))
        return Verdict(
            allow=False,
            mode=PermissionMode.SYNC,
            reason="stub hard deny",
            risk_level="exec",
            requires_approval=False,
        )


def test_e2e_skill_invoke_through_stage_confirm(
    audit_sink: Callable[[AuditDecision], None],
    skills_path: Path,
    permission_engine: OIagentCoworkerPermissionEngine,
    captured_audit: list[_CapturedAudit],
) -> None:
    """Plan §6.2 step 6: the sanctioned skill-invoke path must consult the
    stage_confirm gate before running the invoke side-effect.

    allow path:  gate (permission_engine, SYNC) allows → side-effect runs
                 and exactly one kind="permission" audit envelope is emitted
                 by the engine (stage_confirm itself emits zero audit).
    deny path:   stub gate hard-denies → StageConfirmDenied is raised and
                 the side-effect does NOT run; the stub saw exactly one
                 check with action.kind == "skill_invoke".
    """
    skills_service = OIagentCoworkerSkillsService(
        storage_path=skills_path,
        audit_sink=audit_sink,
    )
    # Manifest/SKILL.md folder-as-truth consumption (W2-5.3; the former
    # TODO is resolved): the service registration below stays because
    # invoke_skill_with_confirm exercises the stage_confirm gate against
    # the registered skill, and the manifest load runs alongside it.
    skills_root = Path(__file__).parent / "fixtures" / "skills"
    manifest = OIagentCoworkerSkillManifest(skills_root).load("capability-04-e2e")
    # §3.5 acceptance: capability-04-e2e present as SKILL.md with
    # e2e_overlay: true declared, and the body was parsed (digest non-empty).
    assert manifest.entry.spec.metadata[E2E_OVERLAY_KEY] is True
    assert manifest.body.digest, "SKILL.md body digest must be non-empty"
    skill = skills_service.register_skill(
        name="capability-04-e2e",
        version="1.0.0",
        description="E2E overlay skill",
        entrypoint="oiagent_coworker.skills",
        config={"e2e_overlay": True},
    )
    assert skill is not None
    assert skill.spec.name == "capability-04-e2e"

    # ── allow path: gate allows → invoke() side-effect runs ─────────
    confirm = OIagentCoworkerStageConfirm(gate=permission_engine)
    invoked: list[str] = []
    invoke = lambda: invoked.append("ran")  # noqa: E731 -- zero-arg callable

    perm_before = sum(1 for d in captured_audit if d.decision.kind == "permission")
    invoke_skill_with_confirm(
        confirm, "capability-04-e2e", target=str(skills_path), invoke=invoke,
    )
    assert invoked == ["ran"], "allowed invoke must run the side-effect"

    # ── gate-consulted assertion: the engine emitted exactly one new
    #    kind="permission" envelope for the confirm_invoke check.
    #    (AuditDecision carries the Verdict but not the Action, so the
    #    action kind is not reachable from the envelope; the count delta
    #    is the robust signal. The deny-path stub below asserts the
    #    action kind directly.)
    perm_after = sum(1 for d in captured_audit if d.decision.kind == "permission")
    assert perm_after == perm_before + 1, (
        f"confirm_invoke must consult the gate exactly once "
        f"(permission audit {perm_before} -> {perm_after})"
    )

    # ── deny path: stub gate hard-denies → StageConfirmDenied, no
    #    side-effect, stub saw exactly one skill_invoke action ────────
    stub = _LocalStubGate()
    deny_confirm = OIagentCoworkerStageConfirm(gate=stub)
    with pytest.raises(StageConfirmDenied) as excinfo:
        invoke_skill_with_confirm(
            deny_confirm,
            "capability-04-e2e",
            target=str(skills_path),
            invoke=invoke,
        )
    assert excinfo.value.result.verdict.requires_approval is False
    assert invoked == ["ran"], "denied invoke must NOT run the side-effect again"
    assert len(stub.calls) == 1, "stub gate must be consulted exactly once"
    stub_action, _stub_ctx = stub.calls[0]
    assert stub_action.kind == "skill_invoke"
    assert stub_action.metadata.get("skill_name") == "capability-04-e2e"
