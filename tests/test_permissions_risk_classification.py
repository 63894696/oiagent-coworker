"""Risk-classification parametrized tests for the permission engine.

This file is split out from tests/test_permissions.py per W2-1.1 fix to
keep each test file focused (<= 150 LOC). Coverage preserved verbatim:

  - test_risk_level_classification:  11 parametrized cases
  - test_plan_mode_requires_user_approval:  4 parametrized cases
  - test_rm_rf_home_root_is_destructive:    1 new case (W2-1.1 fix)

Total: 16 parametrized cases.

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
# Fixtures (local -- keep the split file self-contained)
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
def audit_records() -> list[_AuditRecord]:
    """Per-test audit record buffer."""
    return []


@pytest.fixture
def audit_sink(audit_records: list[_AuditRecord]):
    """Mock audit sink compatible with OIagentCoworkerPermissionEngine.

    W2-1.3: engine now emits ``AuditDecision(kind='permission', ...)``
    envelopes; the mock accepts one such decision and unpacks the
    embedded ``engine_decision`` (a ``Verdict``). ``action`` is
    ``None`` because the envelope does not forward it; the
    risk-classification tests do not inspect the action field.
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
# test_risk_level_classification (11 cases -- preserved from
# tests/test_permissions.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,target,expected_risk",
    [
        ("read_file", "/workspace/x.py", "read"),
        ("grep", "/workspace/x.py", "read"),
        ("write_file", "/workspace/x.py", "write"),
        ("mkdir", "/workspace/dir", "write"),
        ("shell", "echo hello", "exec"),
        ("bash", "ls -la /workspace", "exec"),
        ("delete_file", "/workspace/x.py", "destructive"),
        ("rm", "/workspace/x.py", "destructive"),
        # Cross-platform destructive shell patterns (plan §3.1 mod note).
        ("shell", "rm -rf /workspace", "destructive"),
        ("shell", "Remove-Item -Recurse C:\\Windows", "destructive"),
        ("shell", "del /s /q C:\\*", "destructive"),
    ],
)
def test_risk_level_classification(
    engine: OIagentCoworkerPermissionEngine,
    kind: str,
    target: str,
    expected_risk: str,
) -> None:
    """Risk classification buckets action.kind into the right tier.

    Cross-platform destructive shell patterns (PowerShell + cmd.exe) are
    detected even when kind="shell" -- the plan §3.1 modification note.
    """
    action = Action(kind=kind, target=target)
    ctx = PermissionContext(mode=PermissionMode.SYNC)
    verdict = engine.check(action, ctx)
    assert verdict.risk_level == expected_risk, (
        f"kind={kind!r} target={target!r}: "
        f"risk_level={verdict.risk_level}, expected {expected_risk}"
    )


# ---------------------------------------------------------------------------
# test_plan_mode_requires_user_approval (4 cases -- preserved from
# tests/test_permissions.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        Action(kind="read_file", target="/workspace/x.py"),
        Action(kind="write_file", target="/workspace/x.py"),
        Action(kind="shell", target="echo hello"),
        Action(kind="delete_file", target="/workspace/x.py"),
    ],
)
def test_plan_mode_requires_user_approval(
    engine: OIagentCoworkerPermissionEngine,
    action: Action,
) -> None:
    """Plan mode: requires_approval=True + reason contains 'user approval'."""
    ctx = PermissionContext(mode=PermissionMode.PLAN)
    verdict = engine.check(action, ctx)
    assert verdict.requires_approval is True
    assert verdict.allow is False
    assert verdict.mode is PermissionMode.PLAN
    assert "user approval" in verdict.reason, (
        f"plan-mode reason must mention 'user approval'; got: {verdict.reason!r}"
    )


# ---------------------------------------------------------------------------
# NEW (W2-1.1 fix): rm -rf ~ must be classified as destructive.
# Anchor widened in engine._DESTRUCTIVE_PATTERNS to cover home-root
# explicitly; this test guards against future regression.
# ---------------------------------------------------------------------------


def test_rm_rf_home_root_is_destructive(
    engine: OIagentCoworkerPermissionEngine,
) -> None:
    """`rm -rf ~` must classify as destructive (home-root deletion)."""
    action = Action(kind="shell", target="rm -rf ~")
    ctx = PermissionContext(mode=PermissionMode.SYNC)
    verdict = engine.check(action, ctx)
    assert verdict.risk_level == "destructive", (
        f"rm -rf ~ must be destructive; got risk_level={verdict.risk_level}"
    )
    assert verdict.requires_approval is True
    assert verdict.allow is False


@pytest.mark.parametrize(
    "cmd",
    [
        "rm .bashrc",
        "rm .zshrc",
        "rm .profile",
    ],
)
def test_rm_dotfile_is_not_destructive(
    engine: OIagentCoworkerPermissionEngine, cmd: str
) -> None:
    """Single dotfile delete must classify as exec, NOT destructive.

    Regression for W2-1.1 pattern[0] (since removed): a wildcard flag pattern
    anchored on `[/~.]` with a trailing word-boundary matched `rm .` and
    misclassified `rm .bashrc` as destructive. Daily upgrades / dotfile edits
    were falsely triggering CRITICAL approval paths.
    """
    action = Action(kind="shell", target=cmd)
    ctx = PermissionContext(mode=PermissionMode.SYNC)
    verdict = engine.check(action, ctx)
    assert verdict.risk_level == "exec", (
        f"{cmd!r} must be exec (single dotfile delete); got "
        f"risk_level={verdict.risk_level}"
    )
    assert verdict.allow is True