# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Implements the §5 PolicyGate
#     compat layer: hot-read feature-flag routing between the legacy
#     OIagent P0-3 PolicyEngine (duck-typed, not imported) and the new
#     OIagentCoworkerPermissionEngine, with shadow-mode verdict diffing
#     and enforce-mode fallback-to-legacy on new-engine crash.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""OIagent Coworker -- P0-3 PolicyGate compatibility layer.

The gate sits in front of two permission engines and routes each check
according to a hot-read feature flag:

    shadow    - legacy decides; new engine runs as a sidecar; verdict
                diffs (and new-engine errors) are audited.
    enforce   - new engine decides; legacy idle. A crash in the new
                engine falls back to legacy for that call (a crash must
                never deny service).
    only_old  - legacy decides; new engine fully idle (explicit rollback).

The flag file is re-read on EVERY check() call (plan §5.4 rollback-
without-restart requirement). There is deliberately no mtime caching
and no file watching. All parse/config failures fall back to SHADOW --
not only_old -- because SHADOW preserves identical production behavior
(legacy still decides) while keeping the diff stream alive so the
breakage is noticed. ``only_old`` is reserved as an explicit operator
action.

Anti-flattery boundary (see plan §3.1 / §8.1.1):
    - No ``import openworker`` anywhere in this file.
    - No ``openai`` / ``anthropic`` direct SDK calls.
    - The gate NEVER imports ``oiagent.policy`` (the legacy engine is
      duck-typed via the ``LegacyPolicyEngine`` Protocol; P0-3 does not
      live in this repo).
    - Audit goes through the injected ``AuditSink`` using the standard
      ``AuditDecision`` envelope with ``kind="permission"``; ``AuditKind``
      is NOT extended (keeps the ``engine <-- audit`` DAG untouched).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from oiagent_coworker.permissions.audit import AuditDecision, AuditSink

if TYPE_CHECKING:
    from oiagent_coworker.permissions.engine import (
        Action,
        OIagentCoworkerPermissionEngine,
        PermissionContext,
        Verdict,
    )

__all__ = [
    "LegacyPolicyEngine",
    "PolicyGate",
    "PolicyGateMode",
    "VerdictDiff",
]

_LOGGER = logging.getLogger(__name__)

# Feature-flag key inside feature_flags.json. Expected real-world path:
#   ${OIAGENT_VAULT}/oiagent_coworker/feature_flags.json
# The gate does NOT resolve ${OIAGENT_VAULT} itself -- the caller does.
_FLAG_KEY = "permissions_v2_shadow"

# Decision-bearing fields compared by the shadow-mode diff. ``reason``
# is free text and intentionally NOT compared: legacy and the new
# engine phrase differently by design.
_DIFF_FIELDS: tuple[str, ...] = (
    "allow",
    "requires_approval",
    "risk_level",
    "mode",
)


@runtime_checkable
class LegacyPolicyEngine(Protocol):
    """Duck-typed contract for oiagent.policy.PolicyEngine (P0-3).

    P0-3 does not live in this repo; the gate only relies on a single
    ``classify(action, ctx) -> Verdict`` method with the same shapes as
    engine.py's ``Action`` / ``PermissionContext`` / ``Verdict``.
    ``@runtime_checkable`` lets the gate constructor reject objects
    without a ``classify`` method via ``isinstance``.
    """

    def classify(self, action: Action, ctx: PermissionContext) -> Verdict:
        ...


class PolicyGateMode(Enum):
    """Routing mode resolved from the hot-read feature flag."""

    SHADOW = "shadow"      # legacy decides; new engine sidecar; diffs audited
    ENFORCE = "enforce"    # new engine decides; legacy idle
    ONLY_OLD = "only_old"  # legacy decides; new engine fully idle (rollback)


@dataclass(frozen=True)
class VerdictDiff:
    """Shadow-mode comparison of the two engines' verdicts.

    Only the four decision-bearing fields (``allow``,
    ``requires_approval``, ``risk_level``, ``mode``) participate in the
    comparison; ``reason`` free text is excluded by design.

    Attributes:
        action_kind: ``Action.kind`` of the compared call.
        action_target: ``Action.target`` of the compared call.
        legacy_verdict: ``Verdict.to_dict()`` of the legacy engine.
        new_verdict: ``Verdict.to_dict()`` of the new engine; empty dict
            when the new engine raised (None-safe construction).
        mismatched_fields: Subset of _DIFF_FIELDS that disagreed.
        new_engine_error: ``str(exc)`` when the sidecar engine raised.
    """

    action_kind: str
    action_target: str
    legacy_verdict: dict[str, Any]
    new_verdict: dict[str, Any]
    mismatched_fields: tuple[str, ...]
    new_engine_error: str | None = None


class PolicyGate:
    """Feature-flag router in front of legacy + new permission engines.

    Public API:
        check(action, ctx) -> Verdict
        current_mode() -> PolicyGateMode

    Side effects:
        In SHADOW mode, exactly one gate audit record is emitted per
        check() call ONLY when the two engines disagree or the new
        engine raised (agreement emits nothing extra -- the new engine's
        own internal audit is already a heartbeat). In ENFORCE fallback
        (new engine crash), one audit record with the fallback marker
        is emitted. The gate's audit goes to the gate's OWN audit_sink,
        a distinct envelope from the new engine's internal
        kind="permission" records, which are never suppressed.

    Thread safety:
        The gate holds only immutable references after __init__; the
        flag file is read fresh on every call, so concurrent check()
        calls each resolve the mode independently.
    """

    def __init__(
        self,
        legacy: LegacyPolicyEngine,
        new_engine: OIagentCoworkerPermissionEngine,
        audit_sink: AuditSink,
        flags_path: Path,
    ) -> None:
        """Initialize the gate.

        Args:
            legacy: Duck-typed legacy PolicyEngine (must expose
                ``classify(action, ctx) -> Verdict``).
            new_engine: The new OIagentCoworkerPermissionEngine (must
                expose a callable ``check(action, ctx)``).
            audit_sink: Callable accepting one ``AuditDecision``
                envelope; receives the gate's diff / fallback records.
            flags_path: Path to feature_flags.json. NOT read here --
                hot-read only inside check() / current_mode().

        Raises:
            TypeError: If legacy lacks ``classify``, new_engine lacks a
                callable ``check``, or audit_sink is not callable.
            ValueError: If flags_path is None.
        """
        if not isinstance(legacy, LegacyPolicyEngine):
            raise TypeError(
                "legacy must satisfy the LegacyPolicyEngine protocol "
                f"(classify(action, ctx) -> Verdict); got "
                f"{type(legacy).__name__}"
            )
        if not callable(getattr(new_engine, "check", None)):
            raise TypeError(
                "new_engine must expose a callable check(action, ctx); "
                f"got {type(new_engine).__name__}"
            )
        if not callable(audit_sink):
            raise TypeError(
                f"audit_sink must be callable, got {type(audit_sink).__name__}"
            )
        if flags_path is None:
            raise ValueError(
                "PolicyGate requires a non-None flags_path for hot-read "
                "feature-flag resolution. Received None."
            )
        self._legacy: LegacyPolicyEngine = legacy
        self._new_engine: OIagentCoworkerPermissionEngine = new_engine
        self._audit_sink: AuditSink = audit_sink
        self._flags_path: Path = Path(flags_path)
        _LOGGER.debug(
            "PolicyGate initialized: flags_path=%s", self._flags_path,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, action: Action, ctx: PermissionContext) -> Verdict:
        """Route to legacy/new/shadow per hot-read flag; return winner.

        Routing table (plan §5):
            shadow    -> legacy verdict; new engine sidecar; diff audit
                         on mismatch or new-engine error.
            enforce   -> new engine verdict; legacy idle; on new-engine
                         crash, fall back to legacy for this call and
                         audit the fallback.
            only_old  -> legacy verdict; new engine fully idle.

        Raises:
            Whatever legacy.classify raises in shadow / only_old modes
            (propagated unchanged).
        """
        mode = self._resolve_mode()
        if mode is PolicyGateMode.ENFORCE:
            return self._check_enforce(action, ctx, mode)
        if mode is PolicyGateMode.ONLY_OLD:
            return self._legacy.classify(action, ctx)
        return self._check_shadow(action, ctx, mode)

    def current_mode(self) -> PolicyGateMode:
        """Public read of the resolved mode right now (diagnostics/tests).

        Uses the same hot-read path as check().
        """
        return self._resolve_mode()

    # ------------------------------------------------------------------
    # Mode handlers
    # ------------------------------------------------------------------

    def _check_shadow(
        self,
        action: Action,
        ctx: PermissionContext,
        mode: PolicyGateMode,
    ) -> Verdict:
        """Legacy decides; new engine runs as a sidecar; diffs audited."""
        # Legacy runs first and its exceptions propagate -- shadow mode
        # must never change production behavior.
        legacy_verdict = self._legacy.classify(action, ctx)

        new_verdict_dict: dict[str, Any] = {}
        new_engine_error: str | None = None
        try:
            new_verdict = self._new_engine.check(action, ctx)
            new_verdict_dict = new_verdict.to_dict()
        except Exception as exc:  # noqa: BLE001 -- sidecar must not break verdict
            new_engine_error = str(exc)
            _LOGGER.warning(
                "PolicyGate shadow sidecar new engine raised %s for "
                "action=%s; returning legacy verdict",
                exc, action,
            )

        diff = self._build_diff(
            action,
            legacy_verdict.to_dict(),
            new_verdict_dict,
            new_engine_error,
        )
        if diff.mismatched_fields or diff.new_engine_error is not None:
            error = (
                f"policy_gate:new_engine_error: {new_engine_error}"
                if new_engine_error is not None
                else None
            )
            self._emit_gate_audit(
                winning=legacy_verdict,
                mode=mode,
                diff=diff,
                error=error,
            )
        # Engines agree -> emit NOTHING extra; the new engine's own
        # internal audit record is already a heartbeat.
        return legacy_verdict

    def _check_enforce(
        self,
        action: Action,
        ctx: PermissionContext,
        mode: PolicyGateMode,
    ) -> Verdict:
        """New engine decides; on crash fall back to legacy for this call."""
        try:
            return self._new_engine.check(action, ctx)
        except Exception as exc:  # noqa: BLE001 -- crash must never deny service
            _LOGGER.warning(
                "PolicyGate enforce-mode new engine raised %s for "
                "action=%s; falling back to legacy for this call",
                exc, action,
            )
            legacy_verdict = self._legacy.classify(action, ctx)
            self._emit_gate_audit(
                winning=legacy_verdict,
                mode=mode,
                diff=None,
                error=f"policy_gate:new_engine_error: {exc}",
                extra={"fallback": "legacy_on_new_engine_error"},
            )
            return legacy_verdict

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_mode(self) -> PolicyGateMode:
        """Hot-read the feature flag on every call.

        Fallback on ALL parse/config problems is SHADOW (never
        only_old): SHADOW keeps legacy deciding (identical production
        behavior) while preserving the diff stream to notice breakage.
        """
        try:
            raw = self._flags_path.read_text(encoding="utf-8")
        except OSError as exc:
            # Covers FileNotFoundError, PermissionError, and any other
            # unreadable-file condition. No state caching: warn on
            # every check() call.
            _LOGGER.warning(
                "PolicyGate cannot read flags file %s (%s); "
                "defaulting to shadow",
                self._flags_path, exc,
            )
            return PolicyGateMode.SHADOW

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOGGER.warning(
                "PolicyGate flags file %s is malformed JSON (%s); "
                "defaulting to shadow",
                self._flags_path, exc,
            )
            return PolicyGateMode.SHADOW

        if not isinstance(data, dict):
            _LOGGER.warning(
                "PolicyGate flags file %s top level is %s, not a dict; "
                "defaulting to shadow",
                self._flags_path, type(data).__name__,
            )
            return PolicyGateMode.SHADOW

        if _FLAG_KEY not in data:
            # Default path: key missing -> SHADOW silently.
            return PolicyGateMode.SHADOW

        value = data[_FLAG_KEY]
        if not isinstance(value, str):
            # bool / int / list / null all land here (bool is not str).
            _LOGGER.warning(
                "PolicyGate flag %r has non-string value %r (%s); "
                "defaulting to shadow",
                _FLAG_KEY, value, type(value).__name__,
            )
            return PolicyGateMode.SHADOW

        try:
            return PolicyGateMode(value)
        except ValueError:
            _LOGGER.warning(
                "PolicyGate flag %r has unknown value %r; expected one "
                "of %s; defaulting to shadow",
                _FLAG_KEY, value,
                [m.value for m in PolicyGateMode],
            )
            return PolicyGateMode.SHADOW

    @staticmethod
    def _build_diff(
        action: Action,
        legacy_verdict: dict[str, Any],
        new_verdict: dict[str, Any],
        new_engine_error: str | None,
    ) -> VerdictDiff:
        """Compare the four decision-bearing fields only."""
        if new_engine_error is not None:
            mismatched: tuple[str, ...] = ()
        else:
            mismatched = tuple(
                field
                for field in _DIFF_FIELDS
                if legacy_verdict.get(field) != new_verdict.get(field)
            )
        return VerdictDiff(
            action_kind=action.kind,
            action_target=action.target,
            legacy_verdict=legacy_verdict,
            new_verdict=new_verdict,
            mismatched_fields=mismatched,
            new_engine_error=new_engine_error,
        )

    def _emit_gate_audit(
        self,
        *,
        winning: Verdict,
        mode: PolicyGateMode,
        diff: VerdictDiff | None,
        error: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Emit the gate's own AuditDecision envelope (best-effort).

        The envelope reuses kind="permission" with the WINNING legacy
        verdict in ``engine_decision`` (uniform downstream stream) and
        the gate payload under ``metadata["policy_gate"]``. Audit
        failures are logged but never break the verdict path (mirrors
        the engine.py contract).
        """
        gate_meta: dict[str, Any] = {"mode": mode.value}
        if diff is not None:
            gate_meta["diff"] = asdict(diff)
        if extra:
            gate_meta.update(extra)
        envelope = AuditDecision(
            kind="permission",
            timestamp=datetime.now(UTC),
            engine_decision=winning,
            metadata={"policy_gate": gate_meta},
            error=error,
        )
        try:
            self._audit_sink(envelope)
        except Exception as exc:  # noqa: BLE001 -- audit must not break verdict
            _LOGGER.warning(
                "PolicyGate audit_sink raised %s; verdict path unaffected",
                exc,
            )
