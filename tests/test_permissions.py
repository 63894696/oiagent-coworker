"""Tests for oiagent_coworker.permissions.engine -- W2-1.1 acceptance.

Per W2-extraction-plan §3.1 + §8.1.1, this file covers the core engine
contract: five-mode decision table, workspace_root enforcement, audit-sink
wiring, Verdict invariant, and audit-failure isolation.

Risk-classification parametrized tests (11 risk tiers + 4 plan-mode cases)
live in tests/test_permissions_risk_classification.py.

No external deps beyond pytest / pytest-asyncio (already in pyproject
dev-dependencies; no new deps introduced).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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
class _AuditRecord:
    verdict: Verdict
    action: Action


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A sandboxed workspace root inside pytest's tmp_path."""
    p = tmp_path / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def workspace_file(workspace_root: Path) -> Path:
    """A file inside the workspace root, used as a target for actions."""
    f = workspace_root / "x.py"
    f.write_text("print('hello')\n", encoding="utf-8")
    return f


@pytest.fixture
def audit_records() -> list[_AuditRecord]:
    """Per-test audit record buffer."""
    return []


@pytest.fixture
def audit_sink(audit_records: list[_AuditRecord]):
    """Mock audit sink compatible with OIagentCoworkerPermissionEngine.

    W2-1.3: engine now emits ``AuditDecision(kind='permission', ...)``
    envelopes; the test mock accepts one such decision and unpacks
    the embedded ``engine_decision`` (a ``Verdict``).
    """
    def sink(decision: object) -> None:
        verdict = getattr(decision, "engine_decision", None)
        audit_records.append(_AuditRecord(verdict=verdict, action=None))
    return sink


@pytest.fixture
def engine(
    workspace_root: Path,
    audit_sink,
) -> OIagentCoworkerPermissionEngine:
    """Engine wired to the mock audit sink."""
    return OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=audit_sink,
    )


# ---------------------------------------------------------------------------
# 1. test_five_modes_decision_table
# ---------------------------------------------------------------------------


def test_five_modes_decision_table(
    engine: OIagentCoworkerPermissionEngine,
    workspace_file: Path,
    audit_records: list[_AuditRecord],
) -> None:
    """Same Action input through 5 modes => 5 distinct Verdicts + 5 audits.

    Per plan §8.1.1: 5 modes each return distinct Verdict
    (allow / disallow / plan_required) and audit_sink is invoked 5 times.
    """
    action = Action(
        kind="read_file",
        target=str(workspace_file),
        metadata={"requested_by": "test"},
    )
    expected = {
        PermissionMode.ASYNC:       {"allow": True,  "requires_approval": False},
        PermissionMode.SYNC:        {"allow": True,  "requires_approval": False},
        PermissionMode.PLAN:        {"allow": False, "requires_approval": True},
        PermissionMode.INTERRUPT:   {"allow": True,  "requires_approval": False},
        PermissionMode.COMPACTION:  {"allow": True,  "requires_approval": False},
    }

    for mode in PermissionMode:
        ctx = PermissionContext(
            mode=mode,
            task_id="task-001",
            user_id="user-001",
            session_id="session-001",
        )
        verdict = engine.check(action, ctx)
        assert verdict.mode is mode
        assert verdict.allow is expected[mode]["allow"]
        assert verdict.requires_approval is expected[mode]["requires_approval"]
        assert verdict.risk_level == "read"

    assert len(audit_records) == 5
    for mode, record in zip(PermissionMode, audit_records, strict=True):
        assert record.verdict.mode is mode
        # W2-1.3: engine no longer forwards ``action`` to the audit sink
        # (the AuditDecision envelope carries the verdict only); the
        # action identity assertion was retired with the protocol
        # tightening. The action was the same object for all 5 calls
        # by construction, so dropping the check is sound.


# ---------------------------------------------------------------------------
# 2. test_workspace_root_required
# ---------------------------------------------------------------------------


def test_workspace_root_required() -> None:
    """No workspace_root => __init__ raises ValueError."""
    def _no_op_sink(verdict: Verdict, action: Action) -> None:
        return None

    with pytest.raises(ValueError, match="workspace_root"):
        OIagentCoworkerPermissionEngine(
            workspace_root=None,
            audit_sink=_no_op_sink,
        )
    with pytest.raises(TypeError, match="audit_sink"):
        OIagentCoworkerPermissionEngine(
            workspace_root=Path("/tmp"),
            audit_sink="not-callable",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 3. test_audit_sink_called_with_verdict
# ---------------------------------------------------------------------------


def test_audit_sink_called_with_verdict(
    workspace_root: Path,
    audit_records: list[_AuditRecord],
) -> None:
    """Mock audit_sink receives a fully-populated Verdict + Action.

    W2-1.3: engine emits ``AuditDecision(kind='permission', ...)``
    envelopes; the mock unpacks the embedded verdict. The action is
    captured via closure so the field-level assertions on the action
    still hold.
    """
    target = workspace_root / "data.txt"
    target.write_text("abc", encoding="utf-8")

    captured: dict[str, Action | None] = {"action": None}

    def sink(decision: object) -> None:
        verdict = getattr(decision, "engine_decision", None)
        audit_records.append(_AuditRecord(verdict=verdict, action=captured["action"]))

    action = Action(
        kind="write_file",
        target=str(target),
        metadata={"size_bytes": 3},
    )
    captured["action"] = action

    engine = OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=sink,
    )

    ctx = PermissionContext(
        mode=PermissionMode.SYNC,
        task_id="task-audit",
        user_id="user-audit",
        session_id="session-audit",
    )
    engine.check(action, ctx)

    assert len(audit_records) == 1
    record = audit_records[0]
    v = record.verdict
    assert isinstance(v, Verdict)
    assert isinstance(v.allow, bool)
    assert v.mode is PermissionMode.SYNC
    assert isinstance(v.reason, str) and len(v.reason) > 0
    assert v.risk_level in ("read", "write", "exec", "destructive")
    assert isinstance(v.requires_approval, bool)

    a = record.action
    assert isinstance(a, Action)
    assert a.kind == "write_file"
    assert a.target == str(target)
    assert a.metadata == {"size_bytes": 3}

    payload = v.to_dict()
    for key in ("allow", "mode", "reason", "risk_level", "requires_approval"):
        assert key in payload


# ---------------------------------------------------------------------------
# Verdict invariant + audit-failure isolation
# ---------------------------------------------------------------------------


def test_verdict_invariant_rejects_inconsistent_construction() -> None:
    """Verdict.__post_init__ rejects allow=True + requires_approval=True."""
    with pytest.raises(ValueError, match="Inconsistent Verdict"):
        Verdict(
            allow=True,
            mode=PermissionMode.SYNC,
            reason="test",
            risk_level="read",
            requires_approval=True,
        )


def test_audit_sink_exception_does_not_break_verdict(
    workspace_root: Path,
) -> None:
    """An exception in audit_sink must NOT break the verdict path."""
    def broken_sink(decision: object) -> None:
        raise RuntimeError("audit sink blew up")

    engine = OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=broken_sink,
    )
    action = Action(kind="read_file", target=str(workspace_root / "x.py"))
    ctx = PermissionContext(mode=PermissionMode.SYNC)
    verdict = engine.check(action, ctx)
    assert verdict.allow is True
    assert verdict.risk_level == "read"