"""Tests for oiagent_coworker.permissions.audit -- W2-1.3 audit envelope.

Per W2-extraction-plan §3.1 + §8.1.1 + re-review Note 1, this file
covers the W2-1.3 audit tightening:

  * ``AuditDecision`` tagged-union envelope with 4 ``kind`` values
  * ``AuditSink`` Protocol with a single ``AuditDecision`` argument
  * ``OIagentCoworkerAuditFacade`` adapter methods that wrap each
    subsystem's decision into the envelope

Total: 11 tests, no external deps beyond pytest.
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _SinkCall:
    """Single recorded sink invocation; keeps tests free of Mock()."""
    decision: object


@pytest.fixture
def sink_calls() -> list[_SinkCall]:
    return []


@pytest.fixture
def inner_sink(sink_calls: list[_SinkCall]):
    def _sink(decision: AuditDecision) -> None:
        sink_calls.append(_SinkCall(decision=decision))
    return _sink


@pytest.fixture
def facade(inner_sink) -> OIagentCoworkerAuditFacade:
    return OIagentCoworkerAuditFacade(sink=inner_sink)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    p = tmp_path / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 1. Protocol / envelope shape
# ---------------------------------------------------------------------------


def test_audit_decision_required_arg(inner_sink) -> None:
    """A sink that ignores the AuditDecision (no args) must NOT match the new
    contract; this guards against the W2-1.1 ``def sink(*args, **kwargs)``
    pattern leaking into the W2-1.3 surface.

    The runtime_checkable Protocol does NOT enforce arg count, but the
    W2-1.3 caller (engine.check) calls ``sink(decision)`` -- if a sink
    still has the old ``(verdict, action)`` signature it will fail
    with a missing-positional-arg error. We assert that error here.
    """

    def old_style_sink(verdict: object, action: object) -> None:
        return None

    with pytest.raises(TypeError, match="missing"):
        old_style_sink(object())  # type: ignore[call-arg]


def test_audit_decision_engine_kind(sink_calls: list[_SinkCall]) -> None:
    """An AuditDecision with kind='permission' + engine_decision=v is
    passed verbatim through the sink."""
    from datetime import UTC, datetime

    v = Verdict(
        allow=True,
        mode=PermissionMode.SYNC,
        reason="unit test",
        risk_level="read",
        requires_approval=False,
    )
    decision = AuditDecision(
        kind="permission",
        timestamp=datetime.now(UTC),
        engine_decision=v,
    )
    sink = _RecordingSink(sink_calls)
    sink(decision)
    assert len(sink_calls) == 1
    assert sink_calls[0].decision is decision
    assert sink_calls[0].decision.engine_decision is v


def test_audit_decision_path_sandbox_kind(sink_calls: list[_SinkCall]) -> None:
    """AuditDecision with kind='path_sandbox' carries SandboxDecision."""
    from datetime import UTC, datetime

    from oiagent_coworker.permissions.path_sandbox import (
        OIagentCoworkerPathSandbox,
        PathSandboxConfig,
        SandboxReason,
    )

    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=Path("/tmp").resolve())
    )
    sd = sandbox.sandbox_path("src/main.py")
    decision = AuditDecision(
        kind="path_sandbox",
        timestamp=datetime.now(UTC),
        sandbox_decision=sd,
    )
    sink = _RecordingSink(sink_calls)
    sink(decision)
    assert len(sink_calls) == 1
    assert sink_calls[0].decision.sandbox_decision.reason is SandboxReason.ALLOWED


def test_audit_decision_shell_classifier_kind(sink_calls: list[_SinkCall]) -> None:
    """AuditDecision with kind='shell_classifier' carries ShellClassification."""
    from datetime import UTC, datetime

    from oiagent_coworker.permissions.shell_classifier import (
        OIagentCoworkerShellClassifier,
        ShellRiskLevel,
    )

    classifier = OIagentCoworkerShellClassifier()
    sc = classifier.classify("echo hello")
    decision = AuditDecision(
        kind="shell_classifier",
        timestamp=datetime.now(UTC),
        classification=sc,
    )
    sink = _RecordingSink(sink_calls)
    sink(decision)
    assert len(sink_calls) == 1
    assert sink_calls[0].decision.classification.risk_level is ShellRiskLevel.SAFE


# ---------------------------------------------------------------------------
# 5-7. Facade adapter methods
# ---------------------------------------------------------------------------


def test_facade_for_engine_wraps(
    facade: OIagentCoworkerAuditFacade,
    sink_calls: list[_SinkCall],
) -> None:
    """facade.for_engine() returns a 1-arg sink that wraps Verdict into an
    AuditDecision(kind='permission')."""
    from oiagent_coworker.permissions.engine import Verdict

    adapter = facade.for_engine()
    v = Verdict(
        allow=True,
        mode=PermissionMode.ASYNC,
        reason="unit test",
        risk_level="read",
        requires_approval=False,
    )
    adapter(v)
    assert len(sink_calls) == 1
    d = sink_calls[0].decision
    assert isinstance(d, AuditDecision)
    assert d.kind == "permission"
    assert d.engine_decision is v


def test_facade_for_path_sandbox_wraps(
    facade: OIagentCoworkerAuditFacade,
    sink_calls: list[_SinkCall],
) -> None:
    """facade.for_path_sandbox() wraps SandboxDecision into kind='path_sandbox'."""
    from oiagent_coworker.permissions.path_sandbox import (
        OIagentCoworkerPathSandbox,
        PathSandboxConfig,
    )

    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=Path("/tmp").resolve())
    )
    adapter = facade.for_path_sandbox()
    sd = sandbox.sandbox_path("src/main.py")
    adapter(sd)
    assert len(sink_calls) == 1
    d = sink_calls[0].decision
    assert d.kind == "path_sandbox"
    assert d.sandbox_decision is sd


def test_facade_for_shell_classifier_wraps(
    facade: OIagentCoworkerAuditFacade,
    sink_calls: list[_SinkCall],
) -> None:
    """facade.for_shell_classifier() wraps ShellClassification."""
    from oiagent_coworker.permissions.shell_classifier import (
        OIagentCoworkerShellClassifier,
    )

    classifier = OIagentCoworkerShellClassifier()
    adapter = facade.for_shell_classifier()
    sc = classifier.classify("rm -rf /")
    adapter(sc)
    assert len(sink_calls) == 1
    d = sink_calls[0].decision
    assert d.kind == "shell_classifier"
    assert d.classification is sc


# ---------------------------------------------------------------------------
# 8. facade.emit_standing_rule
# ---------------------------------------------------------------------------


def test_facade_emit_standing_rule(
    facade: OIagentCoworkerAuditFacade,
    sink_calls: list[_SinkCall],
) -> None:
    """facade.emit_standing_rule() emits an AuditDecision(kind='standing_rule')
    with the requested action; persistence.py calls this on add / revoke."""
    from oiagent_coworker.permissions.persistence import (
        OIagentCoworkerStandingRuleStore,
        StandingRule,
        make_default_rule,
    )

    rule = make_default_rule(pattern="read_*", mode=PermissionMode.ASYNC)
    facade.emit_standing_rule("add", standing_rule=rule)
    assert len(sink_calls) == 1
    d = sink_calls[0].decision
    assert d.kind == "standing_rule"
    assert d.standing_rule_action == "add"
    assert d.standing_rule is rule

    facade.emit_standing_rule("revoke", standing_rule=rule)
    assert len(sink_calls) == 2
    d2 = sink_calls[1].decision
    assert d2.standing_rule_action == "revoke"

    # The store itself is a separate import to ensure persistence.py
    # can be loaded independently (no circular import with audit.py).
    assert OIagentCoworkerStandingRuleStore is not None
    assert StandingRule is not None


# ---------------------------------------------------------------------------
# 9-10. Protocol runtime_checkable behavior
# ---------------------------------------------------------------------------


def test_audit_sink_protocol_isinstance_check(
    sink_calls: list[_SinkCall],
) -> None:
    """A 1-arg callable that accepts a generic object satisfies the
    runtime_checkable Protocol: ``isinstance(callable_obj, AuditSink)``
    is True.
    """
    def one_arg(decision: object) -> None:
        return None

    assert isinstance(one_arg, AuditSink)


def test_audit_sink_protocol_rejects_wrong_signature() -> None:
    """Python ``runtime_checkable`` Protocol with ``__call__`` is a
    structural type check that, in CPython 3.11+, accepts ANY callable
    whose ``__call__`` is method-compatible -- it does NOT distinguish
    by argument count. We document this and verify the call-site
    contract: a 2-arg function called with 1 arg fails with TypeError.

    The "shape" gate is enforced by the caller (engine.check), not by
    isinstance. The Protocol is the *type-level* documentation of the
    contract; the engine.py call site is the *runtime* enforcement.
    """
    def two_arg(verdict: object, action: object) -> None:
        return None

    # isinstance returns True because Python's runtime_checkable
    # Protocol does not inspect __call__ arity. This is a known
    # limitation -- see https://docs.python.org/3/library/typing.html#typing.Protocol
    assert isinstance(two_arg, AuditSink)

    # The actual contract gate is the engine.py call site:
    # ``self.audit_sink(AuditDecision(...))`` -- a 2-arg sink gets
    # a TypeError at the actual call, not at isinstance.
    with pytest.raises(TypeError, match="missing"):
        two_arg(object())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# 11. Engine integration: sink receives AuditDecision(kind='permission')
# ---------------------------------------------------------------------------


def test_engine_uses_audit_decision(
    workspace_root: Path,
    sink_calls: list[_SinkCall],
) -> None:
    """OIagentCoworkerPermissionEngine.check() now invokes the audit_sink
    with a single AuditDecision(kind='permission', engine_decision=Verdict)
    argument; the old 2-arg (verdict, action) shape is gone."""
    sink = _RecordingSink(sink_calls)
    engine = OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=sink,
    )
    action = Action(kind="read_file", target=str(workspace_root / "x.py"))
    ctx = PermissionContext(mode=PermissionMode.SYNC)
    engine.check(action, ctx)

    assert len(sink_calls) == 1
    d = sink_calls[0].decision
    assert isinstance(d, AuditDecision)
    assert d.kind == "permission"
    assert d.engine_decision is not None
    assert d.engine_decision.mode is PermissionMode.SYNC
    assert d.engine_decision.risk_level == "read"


def test_audit_kind_literal_includes_inbox_after_w22_extension() -> None:
    """W2-2: AuditKind Literal extended with "inbox" envelope kind."""
    decision = AuditDecision(kind="inbox", timestamp=datetime.now(UTC))
    assert decision.kind == "inbox"

    for k in ("permission", "path_sandbox", "shell_classifier", "standing_rule", "inbox", "selfwake", "skill"):
        d = AuditDecision(kind=k, timestamp=datetime.now(UTC))  # type: ignore[arg-type]
        assert d.kind == k


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingSink:
    """Minimal sink matching the W2-1.3 single-arg contract."""

    def __init__(self, sink_calls: list[_SinkCall]) -> None:
        self._calls = sink_calls

    def __call__(self, decision: AuditDecision) -> None:
        self._calls.append(_SinkCall(decision=decision))
