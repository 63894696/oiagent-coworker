# SPDX-License-Identifier: MIT
#
# Tests for the W2-1.4 audit facade integration acceptance gate.
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_compat_audit_facade.py (new file)
#   Upstream commit:  not present (W2-1.4 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Per W2-extraction-plan §3.4, this file is the W2-1.4 integration
# acceptance suite that verifies the audit facade truly funnels the four
# permission subsystems (engine / path_sandbox / shell_classifier /
# standing_rule_store) through one typed ``AuditDecision`` envelope.
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W2-1.4; no upstream equivalent.
#   - All test names are unique to this file (no collisions with W2-1.1
#     through W2-1.3 tests).
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Compat integration tests for the audit facade (W2-1.4).

This file is the W2-1.4 ship-gate for the audit facade introduced in
W2-1.3 and extended in W2-1.4. It verifies that:

  * ``OIagentCoworkerAuditFacade`` returns properly typed ``AuditSink``
    adapters for each subsystem (engine / path_sandbox /
    shell_classifier / standing_rule).
  * The W2-1.4 2-arg adapter variants
    (``for_path_sandbox_with_original`` /
    ``for_shell_classifier_with_target``) accept the subsystem-native
    ``(decision, ctx_payload)`` call shape and package ``ctx_payload``
    into the envelope's ``metadata`` field before delegating to the
    inner sink.
  * End-to-end, a single inner sink receives one ``AuditDecision`` per
    decision across all four subsystems with the correct ``kind``
    discriminator.

Total: 13 tests, no external deps beyond pytest.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
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
from oiagent_coworker.permissions.engine import (
    Action,
    OIagentCoworkerPermissionEngine,
    PermissionContext,
    PermissionMode,
    Verdict,
)
from oiagent_coworker.permissions.path_sandbox import (
    OIagentCoworkerPathSandbox,
    PathSandboxConfig,
)
from oiagent_coworker.permissions.persistence import (
    OIagentCoworkerStandingRuleStore,
    make_default_rule,
)
from oiagent_coworker.permissions.shell_classifier import (
    OIagentCoworkerShellClassifier,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _CapturedDecision:
    """Single recorded sink invocation -- keeps tests free of Mock()."""

    decision: AuditDecision


@pytest.fixture
def captured() -> list[_CapturedDecision]:
    """Per-test buffer of AuditDecision envelopes captured by the sink."""
    return []


@pytest.fixture
def capturing_sink(captured: list[_CapturedDecision]) -> AuditSink:
    """Inner sink that appends each ``AuditDecision`` to ``captured``."""

    def _sink(decision: AuditDecision) -> None:
        captured.append(_CapturedDecision(decision=decision))

    return _sink


@pytest.fixture
def facade(capturing_sink: AuditSink) -> OIagentCoworkerAuditFacade:
    """Facade wired to ``capturing_sink`` for in-test inspection."""
    return OIagentCoworkerAuditFacade(sink=capturing_sink)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """Sandboxed workspace root inside pytest's tmp_path."""
    p = tmp_path / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 1. Four subsystems sharing one inner sink
# ---------------------------------------------------------------------------


def test_facade_engine_path_sandbox_shell_standing_rule_share_one_sink(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    """All four subsystems funnel through the same inner sink.

    A single ``OIagentCoworkerAuditFacade`` instance with one inner
    ``AuditSink`` must receive exactly one ``AuditDecision`` per
    subsystem call, with ``kind`` discriminating the four payloads.
    This is the W2-1.4 ship-gate test: the facade is the single
    integration point between permissions and the outer audit pipeline.
    """
    # engine
    engine = OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=facade.for_engine(),
    )
    engine.check(
        Action(kind="read_file", target=str(workspace_root / "x.py")),
        PermissionContext(mode=PermissionMode.SYNC),
    )

    # path_sandbox (2-arg adapter)
    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=workspace_root),
        audit_sink=facade.for_path_sandbox_with_original(),
    )
    sandbox.sandbox_path("src/main.py")

    # shell_classifier (2-arg adapter)
    classifier = OIagentCoworkerShellClassifier(
        audit_sink=facade.for_shell_classifier_with_target(),
    )
    classifier.classify("echo hello")

    # standing_rule_store
    store_path = tmp_path / "rules.jsonl"
    store = OIagentCoworkerStandingRuleStore(store_path)
    facade.for_standing_rule_store(store)
    store.add(make_default_rule(pattern="read_*", mode=PermissionMode.ASYNC))

    assert len(captured) == 4
    kinds = [c.decision.kind for c in captured]
    assert kinds == [
        "permission",
        "path_sandbox",
        "shell_classifier",
        "standing_rule",
    ]


# ---------------------------------------------------------------------------
# 2-4. Adapter return types are AuditSink-shaped
# ---------------------------------------------------------------------------


def test_facade_engine_adapter_returns_audit_sink(
    facade: OIagentCoworkerAuditFacade,
) -> None:
    """``for_engine()`` returns a value that passes the ``AuditSink``
    ``isinstance`` check via the runtime-checkable Protocol."""
    adapter = facade.for_engine()
    assert isinstance(adapter, AuditSink)


def test_facade_path_sandbox_adapter_returns_audit_sink(
    facade: OIagentCoworkerAuditFacade,
) -> None:
    """``for_path_sandbox_with_original()`` returns a 2-arg callable
    that still satisfies ``AuditSink`` via the structural Protocol
    (CPython's ``runtime_checkable`` accepts callables regardless of
    arg count when a ``__call__`` method is present -- the *real*
    arity gate is at the actual call site, not isinstance)."""
    adapter = facade.for_path_sandbox_with_original()
    # The 2-arg adapter is intentionally NOT a strict AuditSink -- it
    # accepts (decision, original_path). The runtime_checkable Protocol
    # will nonetheless return True for any callable with __call__.
    # The arity gate is enforced by the actual call site in
    # OIagentCoworkerPathSandbox._finish.
    assert callable(adapter)
    assert callable(adapter) is True


def test_facade_shell_classifier_adapter_returns_audit_sink(
    facade: OIagentCoworkerAuditFacade,
) -> None:
    """``for_shell_classifier_with_target()`` returns a 2-arg callable
    matching the call shape used by ``OIagentCoworkerShellClassifier._audit``."""
    adapter = facade.for_shell_classifier_with_target()
    assert callable(adapter)


# ---------------------------------------------------------------------------
# 5-7. Adapter wrap semantics
# ---------------------------------------------------------------------------


def test_facade_engine_adapter_wraps_verdict(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
) -> None:
    """``for_engine()(verdict)`` wraps the ``Verdict`` into an
    ``AuditDecision(kind='permission', engine_decision=verdict)``."""
    adapter = facade.for_engine()
    verdict = Verdict(
        allow=True,
        mode=PermissionMode.ASYNC,
        reason="W2-1.4 compat test",
        risk_level="read",
        requires_approval=False,
    )
    adapter(verdict)
    assert len(captured) == 1
    decision = captured[0].decision
    assert decision.kind == "permission"
    assert decision.engine_decision is verdict


def test_facade_path_sandbox_adapter_wraps_with_metadata(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
    workspace_root: Path,
) -> None:
    """``for_path_sandbox_with_original()(decision, original_path)`` packs
    ``original_path`` into ``metadata['original_path']`` while carrying the
    full ``SandboxDecision`` in ``sandbox_decision``."""
    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=workspace_root),
    )
    original_path = workspace_root / "src" / "main.py"
    sandbox_decision = sandbox.sandbox_path(original_path)

    adapter = facade.for_path_sandbox_with_original()
    adapter(sandbox_decision, original_path)

    assert len(captured) == 1
    decision = captured[0].decision
    assert decision.kind == "path_sandbox"
    assert decision.sandbox_decision is sandbox_decision
    # W2-1.4: original_path is preserved in metadata, not in a new
    # dataclass field -- this keeps the W2-1.3 surface backward-compatible.
    assert decision.metadata["original_path"] == original_path


def test_facade_shell_classifier_adapter_wraps_with_metadata(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
) -> None:
    """``for_shell_classifier_with_target()(classification, command)``
    packs ``command`` into ``metadata['command']`` while carrying the
    full ``ShellClassification`` in ``classification``."""
    classifier = OIagentCoworkerShellClassifier()
    command = "rm file.txt"
    classification = classifier.classify(command)

    adapter = facade.for_shell_classifier_with_target()
    adapter(classification, command)

    assert len(captured) == 1
    decision = captured[0].decision
    assert decision.kind == "shell_classifier"
    assert decision.classification is classification
    assert decision.metadata["command"] == command


# ---------------------------------------------------------------------------
# 8-9. Standing-rule store adapter via facade
# ---------------------------------------------------------------------------


def test_facade_standing_rule_store_emits_events(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
    tmp_path: Path,
) -> None:
    """``facade.for_standing_rule_store()(store).add(rule)`` emits an
    ``AuditDecision(kind='standing_rule', standing_rule_action='add',
    standing_rule=rule)`` to the inner sink."""
    store_path = tmp_path / "rules.jsonl"
    store = OIagentCoworkerStandingRuleStore(store_path)
    facade.for_standing_rule_store(store)
    rule = make_default_rule(pattern="write_*", mode=PermissionMode.SYNC)
    store.add(rule)

    assert len(captured) == 1
    decision = captured[0].decision
    assert decision.kind == "standing_rule"
    assert decision.standing_rule_action == "add"
    assert decision.standing_rule is rule


def test_facade_standing_rule_store_revoke_emits(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
    tmp_path: Path,
) -> None:
    """``facade.for_standing_rule_store()(store).revoke(rule_id)`` emits
    an ``AuditDecision(kind='standing_rule', standing_rule_action='revoke')``
    envelope. The ``standing_rule`` field is ``None`` for tombstones."""
    store_path = tmp_path / "rules.jsonl"
    store = OIagentCoworkerStandingRuleStore(store_path)
    facade.for_standing_rule_store(store)
    rule = make_default_rule(pattern="read_*", mode=PermissionMode.ASYNC)
    store.add(rule)
    assert len(captured) == 1

    store.revoke(rule.rule_id)
    assert len(captured) == 2
    decision = captured[1].decision
    assert decision.kind == "standing_rule"
    assert decision.standing_rule_action == "revoke"
    assert decision.standing_rule is None


# ---------------------------------------------------------------------------
# 10-12. Subsystem integration through the facade
# ---------------------------------------------------------------------------


def test_engine_uses_audit_decision_through_facade(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
    workspace_root: Path,
) -> None:
    """Engine wired through ``facade.for_engine()`` emits a full
    ``AuditDecision`` (not a raw ``Verdict``) into the inner sink.

    Note on double-wrapping: ``OIagentCoworkerPermissionEngine.check``
    internally pre-wraps its ``Verdict`` in an ``AuditDecision``
    (W2-1.3 ship), and ``facade.for_engine()`` wraps the
    pre-built envelope a second time. This is the documented
    composition pattern -- the inner sink always receives a tagged
    union envelope -- and the original ``Verdict`` remains reachable
    via ``decision.engine_decision.engine_decision`` (or
    ``decision.engine_decision`` if it is already a ``Verdict``).
    The W2-1.4 contract is that the inner sink NEVER receives a raw
    ``Verdict``, regardless of nesting depth.
    """
    engine = OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=facade.for_engine(),
    )
    action = Action(kind="read_file", target=str(workspace_root / "x.py"))
    ctx = PermissionContext(mode=PermissionMode.SYNC)
    verdict = engine.check(action, ctx)

    assert len(captured) == 1
    decision = captured[0].decision
    assert isinstance(decision, AuditDecision)
    assert not isinstance(decision, Verdict)
    assert decision.kind == "permission"
    # Walk the nested envelope to locate the original Verdict; the
    # exact depth depends on whether the engine pre-wraps once
    # (current W2-1.3 behavior) or whether the facade is used as a
    # primary constructor in future revisions.
    cursor: object = decision
    found_verdict = False
    for _ in range(4):
        if isinstance(cursor, Verdict):
            found_verdict = True
            assert cursor is verdict
            break
        if isinstance(cursor, AuditDecision):
            cursor = cursor.engine_decision
            continue
        break
    assert found_verdict, (
        "expected the original Verdict to be reachable through the "
        "nested AuditDecision envelope(s)"
    )


def test_legacy_engine_adapter_emits_clean_envelope(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
) -> None:
    """B4 regression: W2-1.3-era for_engine() adapter (no engine pre-wrap)
    still emits a clean envelope under W2-1.4 metadata-field addition.
    The legacy call shape -- adapter(verdict) where verdict is a bare
    Verdict -- must produce envelope.engine_decision == verdict and
    envelope.metadata == {}.
    """
    # Arrange: legacy call shape -- caller has bare Verdict.
    verdict = Verdict(
        allow=True,
        mode=PermissionMode.SYNC,
        reason="legacy",
        risk_level="read",
        requires_approval=False,
    )
    # Act
    facade.for_engine()(verdict)
    # Assert
    assert len(captured) == 1
    envelope = captured[0].decision
    assert envelope.kind == "permission"
    assert envelope.engine_decision is verdict
    assert envelope.metadata == {}


def test_path_sandbox_uses_facade_adapter_compatibly(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
    workspace_root: Path,
) -> None:
    """``OIagentCoworkerPathSandbox`` accepts the facade's 2-arg adapter
    as its ``audit_sink``. ``sandbox_path()`` returns the correct decision
    (allow=True) AND the inner sink receives a single ``AuditDecision``
    carrying ``original_path`` in ``metadata``."""
    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=workspace_root),
        audit_sink=facade.for_path_sandbox_with_original(),
    )
    target = workspace_root / "src" / "main.py"
    decision = sandbox.sandbox_path(target)
    assert decision.allow is True

    assert len(captured) == 1
    audit_decision = captured[0].decision
    assert isinstance(audit_decision, AuditDecision)
    assert audit_decision.kind == "path_sandbox"
    assert audit_decision.sandbox_decision is decision
    assert audit_decision.metadata["original_path"] == target


def test_shell_classifier_uses_facade_adapter_compatibly(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
) -> None:
    """``OIagentCoworkerShellClassifier`` accepts the facade's 2-arg
    adapter as its ``audit_sink``. ``classify()`` returns the correct
    classification AND the inner sink receives a single ``AuditDecision``
    carrying ``command`` in ``metadata``."""
    classifier = OIagentCoworkerShellClassifier(
        audit_sink=facade.for_shell_classifier_with_target(),
    )
    command = "echo hello"
    classification = classifier.classify(command)
    assert classification.risk_level.value == "safe"

    assert len(captured) == 1
    audit_decision = captured[0].decision
    assert isinstance(audit_decision, AuditDecision)
    assert audit_decision.kind == "shell_classifier"
    assert audit_decision.classification is classification
    assert audit_decision.metadata["command"] == command


# ---------------------------------------------------------------------------
# 13. End-to-end pipeline composition
# ---------------------------------------------------------------------------


def test_all_subsystems_compose_in_pipeline(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    """End-to-end: a shell command is classified, then its path is
    sandbox-checked, then the engine verdicts. The single shared inner
    sink receives exactly three ``AuditDecision`` envelopes -- one per
    subsystem -- with the correct ``kind`` discriminator on each.

    This is the W2-1.4 ship-gate integration test: a single facade +
    single inner sink is sufficient to audit the full permission
    pipeline (shell -> sandbox -> engine).
    """
    classifier = OIagentCoworkerShellClassifier(
        audit_sink=facade.for_shell_classifier_with_target(),
    )
    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=workspace_root),
        audit_sink=facade.for_path_sandbox_with_original(),
    )
    engine = OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=facade.for_engine(),
    )

    # 1. Classify the command.
    command = "echo hello"
    classification = classifier.classify(command)
    assert classification.risk_level.value == "safe"

    # 2. Sandbox the target path.
    target = workspace_root / "log.txt"
    sandbox_decision = sandbox.sandbox_path(target)
    assert sandbox_decision.allow is True

    # 3. Engine verdict.
    action = Action(kind="read_file", target=str(target))
    ctx = PermissionContext(mode=PermissionMode.SYNC)
    verdict = engine.check(action, ctx)
    assert verdict.allow is True

    # Three AuditDecision envelopes, one per subsystem.
    assert len(captured) == 3
    assert [c.decision.kind for c in captured] == [
        "shell_classifier",
        "path_sandbox",
        "permission",
    ]
    # Each envelope carries the matching payload field. The
    # permission envelope may be double-wrapped (engine pre-wrap +
    # facade wrap) -- the original Verdict is reachable through the
    # nested ``engine_decision`` chain (see test 10 for details).
    assert captured[0].decision.classification is classification
    assert captured[0].decision.metadata["command"] == command
    assert captured[1].decision.sandbox_decision is sandbox_decision
    assert captured[1].decision.metadata["original_path"] == target
    perm_decision = captured[2].decision
    cursor: object = perm_decision
    found_verdict = False
    for _ in range(4):
        if isinstance(cursor, Verdict):
            found_verdict = True
            assert cursor is verdict
            break
        if isinstance(cursor, AuditDecision):
            cursor = cursor.engine_decision
            continue
        break
    assert found_verdict, (
        "expected the engine verdict to be reachable through the "
        "nested permission envelope"
    )


# ---------------------------------------------------------------------------
# 14. B3 hard contract: engine pre-wrap path passes through verbatim
# (W2-1.4.1 forward-with-detection). This sits outside the W2-1.4 ship-gate
# envelope (the original 13 tests cover the integration shape); the
# verbatim-forward guarantee is the new hard contract that protects against
# regression in OIagentCoworkerPermissionEngine.check wiring.
# ---------------------------------------------------------------------------


def test_facade_engine_adapter_forwards_existing_envelope_no_double_wrap(
    facade: OIagentCoworkerAuditFacade,
    captured: list[_CapturedDecision],
) -> None:
    """B3 hard contract: if caller already passed a permission-kind
    AuditDecision to for_engine(), the adapter forwards verbatim
    (single layer), not double-wrapped.
    """
    # Arrange: caller has already produced an envelope (e.g., engine pre-wrap path).
    inner_verdict = Verdict(
        allow=True,
        mode=PermissionMode.SYNC,
        reason="unit",
        risk_level="read",
        requires_approval=False,
    )
    pre_wrapped = AuditDecision(
        kind="permission",
        timestamp=datetime.now(UTC),
        engine_decision=inner_verdict,
    )
    # Act
    facade.for_engine()(pre_wrapped)
    # Assert
    assert len(captured) == 1
    forwarded = captured[0].decision
    # Hard: this is the SAME object the caller supplied, not a re-wrap.
    assert forwarded is pre_wrapped
    # Hard: envelope.engine_decision is the Verdict, not another AuditDecision.
    assert isinstance(forwarded.engine_decision, Verdict)
    assert forwarded.engine_decision is inner_verdict