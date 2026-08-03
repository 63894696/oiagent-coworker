# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/audit.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package; replaced the upstream (*args, **kwargs) audit sink
#     duck type with a typed Protocol carrying an AuditDecision envelope.
#   - Introduced AuditDecision tagged-union to discriminate permission /
#     path_sandbox / shell_classifier / standing_rule payloads; facade
#     helpers wrap subsystem-specific calls into the same envelope.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Audit envelope + facade for OIagent Coworker permissions (W2-1.3 + W2-1.4).

This module tightens the audit-sink contract that was previously a loose
``Callable[..., None]`` duck type. The re-review Note 1 flagged that
catching ``(*args, **kwargs)`` made it impossible to type-check or doc-
generate what each subsystem was emitting. W2-1.3 replaces that with:

  * ``AuditDecision`` -- a frozen tagged-union dataclass carrying exactly
    one payload slot per subsystem (engine_decision / sandbox_decision /
    classification / standing_rule), plus an optional ``metadata`` dict
    for subsystem-specific auxiliary context (W2-1.4 addition).
  * ``AuditSink`` -- a runtime-checkable Protocol with a single argument
    that MUST be an ``AuditDecision``.
  * ``OIagentCoworkerAuditFacade`` -- a top-level facade that wraps the
    four subsystem calls into one ``AuditDecision`` each. W2-1.4 adds
    ``for_path_sandbox_with_original()`` and
    ``for_shell_classifier_with_target()`` adapter variants that accept
    the (decision, ctx_payload) 2-arg shape used by path_sandbox and
    shell_classifier internally, packaging the ctx_payload into the
    envelope's ``metadata`` field before delegating to the inner sink.

Anti-flattery boundary (see plan §3.1 / §8.1.1):
    - No ``import openworker`` anywhere in this file.
    - No OAuth broker / MCP server runtime / Tauri shell calls.
    - No ``openai`` / ``anthropic`` direct SDK calls.
    - Borrowed design only (envelope + facade), not runtime integration.

Design note on dependencies:
    audit.py deliberately does NOT import persistence.py. The standing-
    rule subsystem adapter is wired in persistence.py's own test surface
    (via direct ``AuditDecision`` construction) so audit.py has no upward
    dependency edge. This keeps the import DAG strictly
    ``engine <-- audit`` and ``engine <-- persistence <-- audit``.

W2-1.4 metadata field rationale:
    The pre-W2-1.4 facade had ``for_path_sandbox()`` / ``for_shell_classifier()``
    accepting only a single typed payload, which clashed with the way
    path_sandbox / shell_classifier emit (decision, ctx_payload) pairs.
    Rather than mutate those subsystem call sites (which would break
    W2-1.2 ship signatures), W2-1.4 introduces adapter variants that
    accept the 2-arg shape and pack ``ctx_payload`` into a free-form
    ``metadata`` dict on the envelope. This keeps both the upstream
    facade 1-arg shape and the subsystem-native 2-arg shape correct
    without coercion at the call site.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from oiagent_coworker.permissions.engine import Verdict
    from oiagent_coworker.permissions.path_sandbox import SandboxDecision
    from oiagent_coworker.permissions.shell_classifier import ShellClassification

__all__ = [
    "AuditDecision",
    "AuditSink",
    "OIagentCoworkerAuditFacade",
]

_LOGGER = logging.getLogger(__name__)


AuditKind = Literal[
    "permission",
    "path_sandbox",
    "shell_classifier",
    "standing_rule",
    "inbox",  # W2-2: OIagentCoworkerInboxService envelopes
    "selfwake",  # W2-3: OIagentCoworkerSelfWakeScheduler envelopes
    "skill",  # W2-5: OIagentCoworkerSkillsService envelopes
]
StandingRuleAction = Literal["add", "revoke"]


@dataclass(frozen=True)
class AuditDecision:
    """Tagged-union envelope for a single audit record.

    Exactly one of the optional payload fields is populated based on
    ``kind``. The envelope is the only argument to ``AuditSink``; this
    replaces the W2-1.1 ``Callable[[Verdict, Action], None]`` style that
    had no schema and no type safety.

    Attributes:
        kind: Discriminator; selects which payload field is populated.
        timestamp: When the underlying decision was made (UTC).
        engine_decision: Populated when ``kind == "permission"``.
        sandbox_decision: Populated when ``kind == "path_sandbox"``.
        classification: Populated when ``kind == "shell_classifier"``.
        standing_rule_action: For ``kind == "standing_rule"``, the action
            that triggered the audit ("add" or "revoke").
        standing_rule: For ``kind == "standing_rule"``, the affected rule.
            (Forward-declared; populated by persistence.py callers.)
        selfwake_envelope: For ``kind == "selfwake"``, the
            ``TaskFireEnvelope`` describing a register / tick_fire / succeed /
            fail / cancel / disable / enable event from
            ``OIagentCoworkerSelfWakeScheduler``. (Forward-declared as
            ``object | None`` to avoid the audit -> selfwake import edge;
            runtime callers in selfwake/scheduler.py populate this slot
            with the dataclass instance.)
        metadata: Free-form subsystem-specific auxiliary context.
            W2-1.4 addition: ``for_path_sandbox_with_original()`` packs
            ``original_path`` here, and ``for_shell_classifier_with_target()``
            packs ``command``. Consumers MAY rely on string keys but
            MUST treat absent keys as best-effort (older facade adapters
            do not populate this field).
        error: Optional error message (e.g. audit-sink swallowed exception).
    """

    kind: AuditKind
    timestamp: datetime
    engine_decision: Verdict | None = None
    sandbox_decision: SandboxDecision | None = None
    classification: ShellClassification | None = None
    standing_rule_action: StandingRuleAction | None = None
    standing_rule: object | None = None  # StandingRule -- avoids import cycle
    selfwake_envelope: object | None = None  # TaskFireEnvelope -- avoids import cycle
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class AuditSink(Protocol):
    """Audit sink protocol -- W2-1.3 tightened contract.

    The audit sink receives a single ``AuditDecision`` envelope carrying
    the typed payload for the originating subsystem (engine, path_sandbox,
    shell_classifier, standing_rule, inbox, or selfwake). Subsystem-specific
    payload lives in the matching optional field; ``kind`` discriminates
    which is populated.

    Implementations MUST be idempotent and MUST NEVER raise. The caller
    (``OIagentCoworkerPermissionEngine.check`` and the facade adapters)
    catches sink exceptions internally to keep the verdict / decision
    invariants stable.

    Runtime checkability note: ``@runtime_checkable`` allows ``isinstance``
    checks against this Protocol. The check accepts any callable with a
    compatible single-argument signature; it does NOT inspect that
    argument's runtime type.
    """

    def __call__(self, decision: AuditDecision) -> None:
        ...


def _utcnow() -> datetime:
    """Return a timezone-aware UTC ``datetime`` (small helper for adapters)."""
    return datetime.now(UTC)


class OIagentCoworkerAuditFacade:
    """Top-level audit facade wiring all subsystems to one sink.

    The facade is the single integration point between the permission
    subsystem and OIagent's external audit pipeline
    (``oiagent.audit.P2_10_audit_sink``). It exposes one ``for_*`` helper
    per subsystem; each helper returns a sink that wraps the subsystem's
    native decision into an ``AuditDecision`` envelope before delegating
    to the inner sink.
    """

    def __init__(self, sink: AuditSink) -> None:
        if not callable(sink):
            raise TypeError(
                f"AuditFacade sink must be callable, got {type(sink).__name__}"
            )
        self._sink: AuditSink = sink

    # ------------------------------------------------------------------
    # Subsystem-specific adapters
    # ------------------------------------------------------------------

    def for_engine(self) -> AuditSink:
        """Return a sink adapter that tags engine decisions with kind='permission'.

        The returned callable accepts a single argument. Two shapes are
        accepted (W2-1.4.1 forward-with-detection):

          * A bare ``Verdict`` -- wrapped into an
            ``AuditDecision(kind='permission', engine_decision=v)`` before
            delegating to the inner sink. This is the W2-1.3 drop-in
            replacement for the W2-1.1 ``Callable[[Verdict, Action], None]``
            contract.
          * An already-built ``AuditDecision(kind='permission', ...)`` --
            forwarded verbatim to the inner sink, avoiding double-wrap.
            ``OIagentCoworkerPermissionEngine.check`` (W2-1.3) already
            pre-wraps its ``Verdict`` in an ``AuditDecision``; if the
            engine is wired through this adapter the inner sink would
            otherwise receive ``decision.engine_decision.engine_decision``
            = ``AuditDecision``. The forward branch preserves a single
            envelope layer.

        ``Action`` is no longer needed at the audit boundary because the
        engine emits a single decision per call.
        """
        sink = self._sink

        def adapter(verdict) -> None:
            if isinstance(verdict, AuditDecision) and verdict.kind == "permission":
                sink(verdict)
            else:
                sink(
                    AuditDecision(
                        kind="permission",
                        timestamp=_utcnow(),
                        engine_decision=verdict,
                    )
                )

        return adapter

    def for_path_sandbox(self) -> AuditSink:
        """Return a sink adapter for path_sandbox decisions (kind='path_sandbox').

        The returned callable accepts a ``SandboxDecision`` and wraps it
        with ``kind='path_sandbox'``. Existing ``path_sandbox.py`` calls
        ``self.audit_sink(decision, decision.original_path)`` (2-arg legacy
        signature); the W2-1.3 facade adapter takes only the
        ``SandboxDecision`` (1-arg), so existing W2-1.2 wiring is
        unaffected as long as the sink is the path_sandbox-native one,
        not the new typed sink.
        """

        def _adapter(decision_obj: SandboxDecision) -> None:
            decision = AuditDecision(
                kind="path_sandbox",
                timestamp=_utcnow(),
                sandbox_decision=decision_obj,
            )
            self._sink(decision)

        return _adapter

    def for_shell_classifier(self) -> AuditSink:
        """Return a sink adapter for shell_classifier decisions (kind='shell_classifier')."""

        def _adapter(classification: ShellClassification) -> None:
            decision = AuditDecision(
                kind="shell_classifier",
                timestamp=_utcnow(),
                classification=classification,
            )
            self._sink(decision)

        return _adapter

    # ------------------------------------------------------------------
    # W2-1.4: 2-arg adapter variants for subsystems whose internal call
    # sites emit (decision, ctx_payload). The ctx_payload is preserved
    # inside ``AuditDecision.metadata`` so the inner sink still receives
    # a single envelope and the W2-1.3 typed-sink contract is honored.
    # ------------------------------------------------------------------

    def for_path_sandbox_with_original(
        self,
    ) -> Callable[[SandboxDecision, Path], None]:
        """Return a 2-arg sink adapter for path_sandbox (W2-1.4).

        ``OIagentCoworkerPathSandbox._finish`` invokes its ``audit_sink``
        with two positional arguments -- ``(SandboxDecision, original_path)``
        -- because path_sandbox internally already had the original path
        in scope and threading it through the audit envelope as an
        additional field would have meant a non-uniform call signature.

        The returned callable accepts that 2-arg shape and produces a
        single ``AuditDecision`` envelope whose ``metadata['original_path']``
        carries the original request path. The inner sink still receives
        exactly one ``AuditDecision`` argument, preserving the W2-1.3
        1-arg ``AuditSink`` Protocol contract.
        """

        def _adapter(
            decision_obj: SandboxDecision,
            original_path: Path,
        ) -> None:
            envelope = AuditDecision(
                kind="path_sandbox",
                timestamp=_utcnow(),
                sandbox_decision=decision_obj,
                metadata={"original_path": original_path},
            )
            self._sink(envelope)

        return _adapter

    def for_shell_classifier_with_target(
        self,
    ) -> Callable[[ShellClassification, str], None]:
        """Return a 2-arg sink adapter for shell_classifier (W2-1.4).

        ``OIagentCoworkerShellClassifier._audit`` invokes its
        ``audit_sink`` with two positional arguments --
        ``(ShellClassification, command)`` -- because the classifier
        internally already had the raw command string in scope.

        The returned callable accepts that 2-arg shape and produces a
        single ``AuditDecision`` envelope whose ``metadata['command']``
        carries the original command string. The inner sink still
        receives exactly one ``AuditDecision`` argument, preserving the
        W2-1.3 1-arg ``AuditSink`` Protocol contract.
        """

        def _adapter(
            classification: ShellClassification,
            command: str,
        ) -> None:
            envelope = AuditDecision(
                kind="shell_classifier",
                timestamp=_utcnow(),
                classification=classification,
                metadata={"command": command},
            )
            self._sink(envelope)

        return _adapter

    def for_standing_rule_store(
        self,
        store: object,
    ) -> object:
        """Wire a standing-rule store through the facade and return it (W2-1.4).

        Persistence's ``_audit`` method already builds the full
        ``AuditDecision(kind='standing_rule', standing_rule_action=...,
        standing_rule=...)`` envelope before calling its
        ``audit_sink``. The facade adapter here is therefore a
        pass-through: it accepts the pre-built envelope and forwards
        it to the inner sink.

        Args:
            store: An ``OIagentCoworkerStandingRuleStore`` (typed as
                ``object`` to avoid an import cycle). The store's
                audit sink is replaced with the pass-through adapter
                via the public ``set_audit_sink`` setter.

        Returns:
            The same store, so callers can chain ``store.add(rule)``
            / ``store.revoke(rule_id)`` directly.

        Note:
            Uses the public ``set_audit_sink()`` on the store (W2-1.4.1
            addition); does not poke private attrs. We do not import
            persistence here to keep audit.py at the bottom of the
            import DAG.
        """
        from oiagent_coworker.permissions.persistence import (
            OIagentCoworkerStandingRuleStore,
        )

        if not isinstance(store, OIagentCoworkerStandingRuleStore):
            raise TypeError(
                f"for_standing_rule_store expected OIagentCoworkerStandingRuleStore; "
                f"got {type(store).__name__}. Wrap a real store."
            )
        sink = self._sink

        def _passthrough(decision: AuditDecision) -> None:
            sink(decision)

        store.set_audit_sink(_passthrough)
        return store

    def emit_standing_rule(
        self,
        action: StandingRuleAction,
        standing_rule: object | None = None,
    ) -> None:
        """Emit a standing_rule AuditDecision directly.

        Persistence.py calls this in its add / revoke methods (instead of
        returning a wrapped subclass store) to avoid the inheritance /
        init-state coupling a subclass would introduce.
        """
        decision = AuditDecision(
            kind="standing_rule",
            timestamp=_utcnow(),
            standing_rule_action=action,
            standing_rule=standing_rule,
        )
        self._sink(decision)
