# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W2-5.1/5.2/5.3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart (upstream skills/ fixture is
#     empty). Implements the W2-5.2 stage-confirm gate: translates the
#     skills "upload"/"invoke" action into a PolicyGate.check() call,
#     reusing the P0-3 §5 compat layer instead of a Tauri dialog.
#   - Audit flows through the injected PolicyGate (kind="permission");
#     this module adds no audit records (no double-emit).
#   - This file supersedes the "stage_confirm omitted (W2-6 scope)"
#     marker that the skills package __init__.py carried pre-W2-5.2.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""OIagent Coworker -- W2-5.2 stage-confirm gate for skills.

This module is the single sanctioned choke point between the skills
subsystem and actual skill execution. Every "upload" (register a skill)
and "invoke" (run a skill) action is translated into an
:class:`~oiagent_coworker.permissions.engine.Action` and routed through
an injected permission gate (duck-typed: any object exposing a callable
``check(action, ctx) -> Verdict``). In production the injected gate is
the P0-3 §5 :class:`~oiagent_coworker.permissions.policy_gate.PolicyGate`
compat layer; the upstream OpenWorker Tauri confirmation dialog is NOT
reimplemented here.

Audit boundary
--------------

This module emits ZERO audit records of its own and takes NO
``audit_sink`` parameter. Audit flows exclusively through the injected
gate, which emits ``kind="permission"`` records. Injecting a second
sink here would double-emit every decision.

Fail-closed semantics
---------------------

A raising gate propagates: the exception is NOT swallowed into a
silent allow. An approval-required verdict (``requires_approval=True``)
is mapped to ``allowed=False`` and surfaces as
:class:`StageConfirmDenied` from the invoke path.

Anti-flattery boundary (see plan §3.2):
    - No upstream OpenWorker imports anywhere in this file.
    - No OIagent approval/policy-layer imports; the gate is duck-typed,
      same convention as policy_gate.py's LegacyPolicyEngine.
    - No Tauri dialog; the confirm UX lives outside this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from oiagent_coworker.permissions.engine import (
    Action,
    PermissionContext,
    PermissionMode,
    Verdict,
)

__all__ = [
    "OIagentCoworkerStageConfirm",
    "StageConfirmDenied",
    "StageConfirmResult",
    "build_upload_action",
    "invoke_skill_with_confirm",
]

_LOGGER = logging.getLogger(__name__)

# Metadata keys stamped on every Action this module builds. Caller-
# supplied metadata is merged OVER these (caller keys win on collision).
_STAGE_METADATA: dict[str, str] = {"stage": "stage_confirm"}


@dataclass(frozen=True)
class StageConfirmResult:
    """Outcome of a single stage-confirm gate check.

    Attributes:
        allowed: True iff the gate verdict permits the action outright
            (``verdict.allow and not verdict.requires_approval``).
        verdict: The raw :class:`Verdict` returned by the gate, passed
            through verbatim for audit/diagnostics.
        action: The :class:`Action` that was submitted to the gate.
    """

    allowed: bool
    verdict: Verdict
    action: Action


class StageConfirmDenied(PermissionError):
    """Raised when the stage-confirm gate refuses an upload/invoke.

    Carries the full :class:`StageConfirmResult` so callers can inspect
    ``.result.verdict.requires_approval`` to distinguish a hard deny
    from an approval-required deny.
    """

    def __init__(self, result: StageConfirmResult) -> None:
        self.result: StageConfirmResult = result
        super().__init__(
            f"stage_confirm denied {result.action.kind!r} on "
            f"{result.action.target!r}: {result.verdict.reason} "
            f"(requires_approval={result.verdict.requires_approval})"
        )


def build_upload_action(
    skill_name: str,
    *,
    target: str,
    kind: str = "upload",
    metadata: dict[str, Any] | None = None,
) -> Action:
    """Build the Action submitted to the gate for a skills upload/invoke.

    The returned Action's metadata ALWAYS carries
    ``{"skill_name": skill_name, "stage": "stage_confirm"}`` merged
    UNDER the caller-supplied ``metadata`` (caller keys win on
    collision).

    Args:
        skill_name: Name of the skill being uploaded/invoked.
        target: Path or identifier the action targets (e.g. skill dir).
        kind: Action kind; ``"upload"`` for registration,
            ``"skill_invoke"`` for invocation.
        metadata: Caller-supplied extra metadata (merged over the
            stage-confirm defaults).

    Returns:
        An :class:`Action` ready for ``gate.check(action, ctx)``.
    """
    merged: dict[str, Any] = {
        "skill_name": skill_name,
        **_STAGE_METADATA,
    }
    if metadata:
        merged.update(metadata)
    return Action(kind=kind, target=target, metadata=merged)


class OIagentCoworkerStageConfirm:
    """Stage-confirm gate: skills upload/invoke -> PolicyGate.check().

    Injection, NOT ownership: the gate is supplied by the caller and is
    duck-typed (any object with a callable ``check(action, ctx) ->
    Verdict``). This module does NOT import PolicyGate or any OIagent
    approval/policy-layer module.

    Side effects:
        None of its own. Every confirm call delegates to the injected
        gate, which owns audit emission (kind="permission"). This class
        adds no audit records (no double-emit).

    Thread safety:
        Holds only immutable references after __init__; safe for
        concurrent confirm calls as long as the injected gate and
        ctx_factory are themselves safe.
    """

    def __init__(
        self,
        gate: Any,
        ctx_factory: Callable[[], PermissionContext] | None = None,
    ) -> None:
        """Initialize the stage-confirm gate.

        Args:
            gate: Any object exposing a callable
                ``check(action, ctx) -> Verdict``. Typically the P0-3 §5
                PolicyGate, but duck-typed for testability.
            ctx_factory: Zero-arg callable returning a fresh
                :class:`PermissionContext` per check. Default builds
                ``PermissionContext(mode=PermissionMode.SYNC)`` with
                one-off scope (no task/user/session id).

        Raises:
            TypeError: If gate lacks a callable ``check``, or
                ctx_factory is not None and not callable.
        """
        if not callable(getattr(gate, "check", None)):
            raise TypeError(
                "gate must expose a callable check(action, ctx) -> Verdict; "
                f"got {type(gate).__name__}"
            )
        if ctx_factory is not None and not callable(ctx_factory):
            raise TypeError(
                f"ctx_factory must be callable, got {type(ctx_factory).__name__}"
            )
        self._gate: Any = gate
        self._ctx_factory: Callable[[], PermissionContext] = (
            ctx_factory if ctx_factory is not None else self._default_ctx
        )
        _LOGGER.debug(
            "OIagentCoworkerStageConfirm initialized: gate=%s",
            type(gate).__name__,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def confirm_upload(
        self,
        skill_name: str,
        *,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> StageConfirmResult:
        """Run the gate for a skill upload (registration) action.

        A raising gate propagates (fail-closed; never a silent allow).
        """
        action = build_upload_action(
            skill_name, target=target, kind="upload", metadata=metadata,
        )
        return self._confirm(action)

    def confirm_invoke(
        self,
        skill_name: str,
        *,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> StageConfirmResult:
        """Run the gate for a skill invocation action (plan §6.2 step 6).

        Thin wrapper over :meth:`confirm_upload` with
        ``kind="skill_invoke"``. A raising gate propagates.
        """
        action = build_upload_action(
            skill_name, target=target, kind="skill_invoke", metadata=metadata,
        )
        return self._confirm(action)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _confirm(self, action: Action) -> StageConfirmResult:
        """Submit the action to the gate and map Verdict -> result.

        Gate semantics:
            allowed = verdict.allow AND (not verdict.requires_approval).

        A require-approval verdict surfaces as allowed=False; the
        ``requires_approval`` flag is preserved on the result's verdict
        so the caller can distinguish it from a hard deny.
        """
        ctx = self._ctx_factory()
        verdict = self._gate.check(action, ctx)
        allowed = bool(verdict.allow) and not verdict.requires_approval
        return StageConfirmResult(allowed=allowed, verdict=verdict, action=action)

    @staticmethod
    def _default_ctx() -> PermissionContext:
        """Default context: SYNC mode, one-off scope (no standing rule)."""
        return PermissionContext(mode=PermissionMode.SYNC)


def invoke_skill_with_confirm(
    confirm: OIagentCoworkerStageConfirm,
    skill_name: str,
    *,
    target: str,
    invoke: Callable[[], Any],
    metadata: dict[str, Any] | None = None,
) -> Any:
    """The sanctioned skill-invoke path for the W2-5.2 scope.

    Scope note: this function gates the *invoke* path only. Gating the
    pre-existing
    :meth:`OIagentCoworkerSkillsService.load_skill_module`
    module-loading path is W2-6 integration scope, not W2-5.2; no
    service wiring is done here.

    Order:
        1. ``result = confirm.confirm_invoke(...)`` -- ALWAYS hits the
           gate, even when the verdict will deny.
        2. If ``not result.allowed`` -> raise :class:`StageConfirmDenied`
           carrying the result (``invoke`` is NOT run).
        3. ``return invoke()`` -- the zero-arg callable runs ONLY when
           the gate allowed the action.

    Args:
        confirm: The stage-confirm gate instance.
        skill_name: Name of the skill being invoked.
        target: Path or identifier the invocation targets.
        invoke: Zero-arg callable performing the actual invocation.
        metadata: Caller-supplied extra metadata for the gate Action.

    Returns:
        Whatever ``invoke()`` returns.

    Raises:
        StageConfirmDenied: When the gate denies (hard deny or
            approval-required).
        Exception: Whatever the gate raises propagates unchanged
            (fail-closed; never a silent allow).
    """
    result = confirm.confirm_invoke(skill_name, target=target, metadata=metadata)
    if not result.allowed:
        raise StageConfirmDenied(result)
    return invoke()
