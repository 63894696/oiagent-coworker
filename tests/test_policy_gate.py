"""Tests for oiagent_coworker.permissions.policy_gate -- P0-3 acceptance.

Covers the PolicyGate contract: three-mode routing table, hot-read
feature-flag reload, failure-mode fallback-to-shadow matrix, shadow-mode
verdict diffing, enforce-mode fallback-to-legacy on new-engine crash,
and audit-sink failure isolation.

Fixture patterns mirror tests/test_permissions.py: tmp_path for the
flags file, list-buffer mock audit sink, stub engines.

No external deps beyond pytest (already in pyproject dev-dependencies;
no new deps introduced).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.audit import AuditDecision
from oiagent_coworker.permissions.engine import (
    Action,
    OIagentCoworkerPermissionEngine,
    PermissionContext,
    PermissionMode,
    Verdict,
)
from oiagent_coworker.permissions.policy_gate import (
    LegacyPolicyEngine,
    PolicyGate,
    PolicyGateMode,
)

# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------


class _StubLegacy:
    """Legacy PolicyEngine stub; records classify() call count."""

    def __init__(self, verdict: Verdict) -> None:
        self._verdict = verdict
        self.calls = 0

    def classify(self, action: Action, ctx: PermissionContext) -> Verdict:
        self.calls += 1
        return self._verdict


class _RaisingEngine:
    """New-engine stub that always raises from check()."""

    def __init__(self, message: str = "boom") -> None:
        self._message = message
        self.calls = 0

    def check(self, action: Action, ctx: PermissionContext) -> Verdict:
        self.calls += 1
        raise RuntimeError(self._message)


def _allow_verdict() -> Verdict:
    return Verdict(
        allow=True,
        mode=PermissionMode.SYNC,
        reason="stub legacy allows",
        risk_level="read",
        requires_approval=False,
    )


def _deny_verdict() -> Verdict:
    return Verdict(
        allow=False,
        mode=PermissionMode.SYNC,
        reason="stub legacy denies destructive action",
        risk_level="destructive",
        requires_approval=True,
    )


@pytest.fixture
def flags_path(tmp_path: Path) -> Path:
    return tmp_path / "feature_flags.json"


def _write_flags(flags_path: Path, value) -> None:
    flags_path.write_text(
        json.dumps({"permissions_v2_shadow": value}), encoding="utf-8",
    )


@pytest.fixture
def gate_audit_records() -> list[AuditDecision]:
    """Per-test gate audit record buffer (gate's OWN sink)."""
    return []


@pytest.fixture
def gate_audit_sink(gate_audit_records: list[AuditDecision]):
    def sink(decision: AuditDecision) -> None:
        gate_audit_records.append(decision)
    return sink


@pytest.fixture
def engine_audit_records() -> list[AuditDecision]:
    """Per-test record buffer for the new engine's internal audit."""
    return []


@pytest.fixture
def engine_audit_sink(engine_audit_records: list[AuditDecision]):
    def sink(decision: AuditDecision) -> None:
        engine_audit_records.append(decision)
    return sink


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    p = tmp_path / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


@pytest.fixture
def real_engine(
    workspace_root: Path,
    engine_audit_sink,
) -> OIagentCoworkerPermissionEngine:
    return OIagentCoworkerPermissionEngine(
        workspace_root=workspace_root,
        audit_sink=engine_audit_sink,
    )


def _ctx() -> PermissionContext:
    return PermissionContext(mode=PermissionMode.SYNC, task_id="task-gate")


# ---------------------------------------------------------------------------
# 1. shadow, engines agree -> legacy decides, no gate audit
# ---------------------------------------------------------------------------


def test_shadow_mode_legacy_decides_and_engines_agree_no_diff(
    flags_path: Path,
    gate_audit_records: list[AuditDecision],
    engine_audit_records: list[AuditDecision],
    real_engine: OIagentCoworkerPermissionEngine,
) -> None:
    _write_flags(flags_path, "shadow")
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=gate_audit_records.append,
        flags_path=flags_path,
    )

    action = Action(kind="read_file", target="x.py")
    verdict = gate.check(action, _ctx())

    assert verdict is legacy_verdict
    assert legacy.calls == 1
    assert len(gate_audit_records) == 0
    # New engine ran as sidecar: its own internal audit fired.
    assert len(engine_audit_records) == 1


# ---------------------------------------------------------------------------
# 2. shadow mismatch -> exactly one gate diff audit
# ---------------------------------------------------------------------------


def test_shadow_mode_verdict_mismatch_emits_diff(
    flags_path: Path,
    gate_audit_records: list[AuditDecision],
    real_engine: OIagentCoworkerPermissionEngine,
) -> None:
    _write_flags(flags_path, "shadow")
    # Stub legacy allows a destructive action; real engine denies it.
    legacy = _StubLegacy(_allow_verdict())
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=gate_audit_records.append,
        flags_path=flags_path,
    )

    action = Action(kind="delete_file", target="/tmp/important")
    verdict = gate.check(action, _ctx())

    assert verdict.allow is True  # legacy wins in shadow mode
    assert len(gate_audit_records) == 1
    record = gate_audit_records[0]
    assert record.kind == "permission"
    assert record.engine_decision is verdict
    diff = record.metadata["policy_gate"]["diff"]
    assert "allow" in diff["mismatched_fields"]
    assert diff["action_kind"] == "delete_file"
    assert diff["new_engine_error"] is None
    assert record.metadata["policy_gate"]["mode"] == "shadow"


# ---------------------------------------------------------------------------
# 3. enforce -> new engine decides, legacy untouched
# ---------------------------------------------------------------------------


def test_enforce_mode_new_engine_decides_legacy_untouched(
    flags_path: Path,
    gate_audit_records: list[AuditDecision],
    engine_audit_records: list[AuditDecision],
    real_engine: OIagentCoworkerPermissionEngine,
) -> None:
    _write_flags(flags_path, "enforce")
    legacy = _StubLegacy(_allow_verdict())
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=gate_audit_records.append,
        flags_path=flags_path,
    )

    action = Action(kind="delete_file", target="/tmp/important")
    verdict = gate.check(action, _ctx())

    assert legacy.calls == 0
    assert verdict.allow is False  # real engine denies destructive
    assert verdict.risk_level == "destructive"
    assert len(engine_audit_records) == 1
    # Gate emits no audit of its own in clean enforce mode.
    assert len(gate_audit_records) == 0


# ---------------------------------------------------------------------------
# 4. only_old -> legacy decides, new engine fully idle
# ---------------------------------------------------------------------------


def test_only_old_mode_new_engine_idle(
    flags_path: Path,
    engine_audit_records: list[AuditDecision],
    real_engine: OIagentCoworkerPermissionEngine,
) -> None:
    _write_flags(flags_path, "only_old")
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=lambda d: None,
        flags_path=flags_path,
    )

    verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert verdict is legacy_verdict
    assert legacy.calls == 1
    # New engine never ran -> no internal audit records.
    assert len(engine_audit_records) == 0


# ---------------------------------------------------------------------------
# 5. hot reload: flip the flag mid-process, same gate instance
# ---------------------------------------------------------------------------


def test_hot_reload_flag_flip_mid_process(
    flags_path: Path,
    engine_audit_records: list[AuditDecision],
    real_engine: OIagentCoworkerPermissionEngine,
) -> None:
    _write_flags(flags_path, "shadow")
    legacy = _StubLegacy(_allow_verdict())
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=lambda d: None,
        flags_path=flags_path,
    )
    action = Action(kind="read_file", target="x.py")

    assert gate.current_mode() is PolicyGateMode.SHADOW
    gate.check(action, _ctx())
    assert legacy.calls == 1

    _write_flags(flags_path, "enforce")
    assert gate.current_mode() is PolicyGateMode.ENFORCE
    verdict = gate.check(action, _ctx())

    # Second call routed to the new engine; legacy call count stays 1.
    assert legacy.calls == 1
    assert verdict.allow is True
    assert len(engine_audit_records) == 2


# ---------------------------------------------------------------------------
# 6. missing flags file -> SHADOW + warning
# ---------------------------------------------------------------------------


def test_missing_flags_file_defaults_shadow_with_warning(
    tmp_path: Path,
    real_engine: OIagentCoworkerPermissionEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = tmp_path / "does_not_exist.json"
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=lambda d: None,
        flags_path=missing,
    )

    with caplog.at_level(logging.WARNING):
        verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert gate.current_mode() is PolicyGateMode.SHADOW
    assert verdict is legacy_verdict
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# 7. malformed JSON -> SHADOW + warning
# ---------------------------------------------------------------------------


def test_malformed_json_defaults_shadow(
    flags_path: Path,
    real_engine: OIagentCoworkerPermissionEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    flags_path.write_text("{not json", encoding="utf-8")
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=lambda d: None,
        flags_path=flags_path,
    )

    with caplog.at_level(logging.WARNING):
        verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert gate.current_mode() is PolicyGateMode.SHADOW
    assert verdict is legacy_verdict
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# 8. wrong-type flag value -> SHADOW + warning
# ---------------------------------------------------------------------------


def test_wrong_type_flag_value_defaults_shadow(
    flags_path: Path,
    real_engine: OIagentCoworkerPermissionEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_flags(flags_path, True)
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=lambda d: None,
        flags_path=flags_path,
    )

    with caplog.at_level(logging.WARNING):
        verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert gate.current_mode() is PolicyGateMode.SHADOW
    assert verdict is legacy_verdict
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# 9. unknown flag string -> SHADOW + warning
# ---------------------------------------------------------------------------


def test_unknown_flag_string_defaults_shadow(
    flags_path: Path,
    real_engine: OIagentCoworkerPermissionEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_flags(flags_path, "yolo")
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=lambda d: None,
        flags_path=flags_path,
    )

    with caplog.at_level(logging.WARNING):
        verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert gate.current_mode() is PolicyGateMode.SHADOW
    assert verdict is legacy_verdict
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# 10. flags_path under a nonexistent vault root -> SHADOW + warning
# ---------------------------------------------------------------------------


def test_missing_env_var_scenario_vault_unset(
    tmp_path: Path,
    real_engine: OIagentCoworkerPermissionEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Boundary doc: caller failed to resolve ${OIAGENT_VAULT}; the
    flags path points inside a vault root dir that does not exist."""
    vault_path = (
        tmp_path / "nonexistent_vault" / "oiagent_coworker"
        / "feature_flags.json"
    )
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=lambda d: None,
        flags_path=vault_path,
    )

    with caplog.at_level(logging.WARNING):
        verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert gate.current_mode() is PolicyGateMode.SHADOW
    assert verdict is legacy_verdict
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# 11. shadow + new engine raises -> legacy verdict, diff records error
# ---------------------------------------------------------------------------


def test_shadow_new_engine_raises_diff_records_error(
    flags_path: Path,
    gate_audit_records: list[AuditDecision],
) -> None:
    _write_flags(flags_path, "shadow")
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    raising = _RaisingEngine("sidecar exploded")
    gate = PolicyGate(
        legacy=legacy,
        new_engine=raising,
        audit_sink=gate_audit_records.append,
        flags_path=flags_path,
    )

    verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert verdict is legacy_verdict
    assert raising.calls == 1
    assert len(gate_audit_records) == 1
    record = gate_audit_records[0]
    assert record.error is not None
    assert "new_engine_error" in record.error
    diff = record.metadata["policy_gate"]["diff"]
    assert "sidecar exploded" in diff["new_engine_error"]
    assert diff["new_verdict"] == {}


# ---------------------------------------------------------------------------
# 12. enforce + new engine raises -> fall back to legacy, audit fallback
# ---------------------------------------------------------------------------


def test_enforce_new_engine_raises_falls_back_to_legacy(
    flags_path: Path,
    gate_audit_records: list[AuditDecision],
) -> None:
    _write_flags(flags_path, "enforce")
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)
    raising = _RaisingEngine("enforce crash")
    gate = PolicyGate(
        legacy=legacy,
        new_engine=raising,
        audit_sink=gate_audit_records.append,
        flags_path=flags_path,
    )

    verdict = gate.check(Action(kind="read_file", target="x.py"), _ctx())

    assert verdict is legacy_verdict
    assert legacy.calls == 1
    assert len(gate_audit_records) == 1
    record = gate_audit_records[0]
    gate_meta = record.metadata["policy_gate"]
    assert gate_meta["fallback"] == "legacy_on_new_engine_error"
    assert gate_meta["mode"] == "enforce"
    assert record.error is not None
    assert "new_engine_error" in record.error


# ---------------------------------------------------------------------------
# 13. gate audit_sink raises -> verdict path unbroken, warning logged
# ---------------------------------------------------------------------------


def test_audit_sink_raises_does_not_break_verdict(
    flags_path: Path,
    real_engine: OIagentCoworkerPermissionEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_flags(flags_path, "shadow")
    # Force a mismatch so the gate tries to emit a diff audit.
    legacy_verdict = _allow_verdict()
    legacy = _StubLegacy(legacy_verdict)

    def bad_sink(decision: AuditDecision) -> None:
        raise RuntimeError("audit backend down")

    gate = PolicyGate(
        legacy=legacy,
        new_engine=real_engine,
        audit_sink=bad_sink,
        flags_path=flags_path,
    )

    with caplog.at_level(logging.WARNING):
        verdict = gate.check(
            Action(kind="delete_file", target="/tmp/important"), _ctx(),
        )

    assert verdict is legacy_verdict
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# 14. LegacyPolicyEngine runtime protocol check
# ---------------------------------------------------------------------------


def test_legacy_protocol_runtime_check(
    flags_path: Path,
    real_engine: OIagentCoworkerPermissionEngine,
) -> None:
    stub = _StubLegacy(_allow_verdict())
    assert isinstance(stub, LegacyPolicyEngine)

    class _NoClassify:
        pass

    assert not isinstance(_NoClassify(), LegacyPolicyEngine)
    with pytest.raises(TypeError):
        PolicyGate(
            legacy=_NoClassify(),
            new_engine=real_engine,
            audit_sink=lambda d: None,
            flags_path=flags_path,
        )

    # Also verify flags_path=None -> ValueError.
    with pytest.raises(ValueError):
        PolicyGate(
            legacy=stub,
            new_engine=real_engine,
            audit_sink=lambda d: None,
            flags_path=None,
        )
