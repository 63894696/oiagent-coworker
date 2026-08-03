# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/permissions.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced openworker audit
#     sink with oiagent.audit.P2_10; shell op regex list extended with
#     PowerShell + cmd.exe on Windows; standing rule expiry default shortened
#     from 1h to 15min per OIagent P2-10 risk profile.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""OIagent Coworker -- five-mode permission decision engine (W2-1.1).

This module is the core of OIagent Coworker's permission system. It replaces
OIagent P0-3 PolicyEngine for the five-mode decision path
(async / sync / plan / interrupt / compaction). Heavy lifting for the path
sandbox, shell op classification, and standing-rule persistence lives in
sibling modules (W2-1.2 / W2-1.3 / W2-1.4); this engine owns the mode-based
verdict synthesis and the audit-sink wiring.

Anti-flattery boundary (see plan §3.1 / §8.1.1):
    - No `import openworker` anywhere in this file.
    - No OAuth broker / MCP server runtime / Tauri shell calls.
    - No `openai` / `anthropic` direct SDK calls.
    - Audit goes through an injected sink; callers wire this to
      `oiagent.audit.P2_10_audit_sink`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

# AuditSink protocol now lives in oiagent_coworker.permissions.audit
# (W2-1.3 tightening per re-review Note 1).
# Re-imported here for backward compatibility with engine.py callers.
from oiagent_coworker.permissions.audit import (
    AuditDecision,
    AuditSink,
)

# Borrowed design (MIT, see SPDX header for upstream attribution):
#   - PermissionMode five-state machine
#   - Path sandbox + shell op detection + task-scoped standing rule design
# NOT borrowed:
#   - OAuth broker / MCP server runtime / Tauri shell
#   - openai / anthropic direct SDK calls (routed via OIagent 15721 proxy)
# For upstream diff, see git log --follow <this file> and SPDX header.

__all__ = [
    "Action",
    "AuditSink",
    "OIagentCoworkerPermissionEngine",
    "PermissionContext",
    "PermissionMode",
    "RiskLevel",
    "Verdict",
]

_LOGGER = logging.getLogger(__name__)


# Action-kind buckets -- drive _classify_risk().
_READ_KINDS: frozenset[str] = frozenset({
    "read_file", "read_dir", "list_files", "glob", "grep", "search",
    "stat", "exists", "cat", "head", "tail", "inspect",
})
_WRITE_KINDS: frozenset[str] = frozenset({
    "write_file", "create_file", "edit_file", "append_file",
    "mkdir", "rename", "copy_file", "symlink", "touch",
})
_EXEC_KINDS: frozenset[str] = frozenset({
    "shell", "exec", "run_command", "subprocess", "python_exec",
    "node_exec", "bash", "sh", "cmd", "powershell", "pwsh", "command",
})
_DESTRUCTIVE_KINDS: frozenset[str] = frozenset({
    "delete_file", "delete_dir", "remove_file", "rm", "truncate",
    "drop_table", "wipe_disk", "format", "destroy", "unlink",
})

# Default standing-rule TTL: 15 minutes (OIagent P2-10 risk profile).
# Upstream source project used 1h; OIagent tightened to 15min per plan §3.1.
DEFAULT_STANDING_RULE_TTL_S: int = 15 * 60

# Cross-platform destructive shell patterns (plan §3.1 modification note:
#   "shell op regex list extended with PowerShell + cmd.exe on Windows").
# Full classification lives in W2-1.2 shell_classifier.py; this is the
# first-line guard used by _classify_risk().
_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # rm -rf / (explicit slash root).
    re.compile(r"\brm\s+-rf?\s+/"),
    # rm -rf ~ and rm -rf ~/foo (home-root; anchor widened per W2-1.1 fix).
    re.compile(r"\brm\s+-rf?\s+~"),
    # rm -rf . and rm -rf ./build (current-dir; anchor widened per W2-1.1 fix).
    re.compile(r"\brm\s+-rf?\s+\."),
    re.compile(r"\bRemove-Item\b[^|;&]*-Recurse\b", re.IGNORECASE),
    re.compile(r"\bdel\s+/[sS]\s+/[qQ]\b"),
    re.compile(r"\brmdir\s+/[sS]\s+/[qQ]\b"),
    re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),
    re.compile(r"\bdd\s+if=.*of=/dev/(sd|nvme|hd)"),
)


class PermissionMode(Enum):
    """Five-mode permission state machine.

    ASYNC       - background, does not block the main conversation
    SYNC        - same-thread synchronous execution
    PLAN        - draft plan only; user must approve before execution
    INTERRUPT   - execution may be halted by the user mid-flight
    COMPACTION  - long-conversation compression window (read-only)
    """

    ASYNC = "async"
    SYNC = "sync"
    PLAN = "plan"
    INTERRUPT = "interrupt"
    COMPACTION = "compaction"


RiskLevel = Literal["read", "write", "exec", "destructive"]


@dataclass(frozen=True)
class Verdict:
    """Result of a single permission check.

    Invariant: `requires_approval=True` implies `allow=False`. Enforced
    by __post_init__ so mode-specific decisions cannot produce
    inconsistent verdicts.
    """

    allow: bool
    mode: PermissionMode
    reason: str
    risk_level: RiskLevel
    requires_approval: bool

    def __post_init__(self) -> None:
        if self.requires_approval and self.allow:
            raise ValueError(
                "Inconsistent Verdict: requires_approval=True but allow=True "
                f"(mode={self.mode.value}, risk_level={self.risk_level})"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (consumed by audit sink)."""
        return {
            "allow": self.allow,
            "mode": self.mode.value,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval,
        }


@dataclass(frozen=True)
class Action:
    """A proposed action awaiting a permission check.

    Attributes:
        kind: One of the recognized action kinds (e.g. "read_file",
            "write_file", "shell", "delete_file"). Drives the default
            risk tier; full shell-op semantic classification lives in
            W2-1.2 shell_classifier.py.
        target: The path or shell command the action targets.
        metadata: Free-form context the caller wants attached to the
            audit record (e.g. {"shell_cmd": "rm -rf /tmp/foo"}).
    """

    kind: str
    target: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionContext:
    """Context for a single permission check.

    Attributes:
        mode: Which PermissionMode the check should be made under.
        task_id: Task scope identifier for standing-rule lookup. None
            means one-off (no standing rule applies).
        user_id: User identity for audit. None means anonymous session.
        timestamp: When the check is happening (UTC).
        session_id: Optional session identifier for audit correlation.
        force_strict: If True, escalate write/exec to PLAN-style approval
            even in SYNC/INTERRUPT modes.
    """

    mode: PermissionMode
    task_id: str | None = None
    user_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    session_id: str | None = None
    force_strict: bool = False


class OIagentCoworkerPermissionEngine:
    """Five-mode permission decision engine -- covers OIagent P0-3 PolicyEngine.

    Public API:
        check(action, ctx) -> Verdict

    Side effects:
        Every call to check() invokes self.audit_sink with an
        ``AuditDecision(kind='permission', engine_decision=verdict)``
        envelope (W2-1.3 typed contract). Audit failures are logged but
        never break the verdict path.

    Thread safety:
        The engine holds only immutable references after __init__ and is
        safe for concurrent check() calls. The audit_sink contract is
        responsible for its own thread safety.
    """

    def __init__(
        self,
        workspace_root: Path | None,
        audit_sink: AuditSink,
    ) -> None:
        """Initialize the engine.

        Args:
            workspace_root: Filesystem root for the path sandbox. Required.
                The full sandbox (symlink/hardlink escape, TOCTOU) lives in
                W2-1.2 path_sandbox.py; this constructor only resolves the
                root so downstream checks can do relative_path containment.
            audit_sink: Callable invoked with one ``AuditDecision`` argument
                (W2-1.3 typed contract). The default caller wires this to
                ``oiagent.audit.P2_10_audit_sink`` via the audit facade.

        Raises:
            ValueError: If workspace_root is None.
            TypeError: If audit_sink is not callable.
        """
        if workspace_root is None:
            raise ValueError(
                "OIagentCoworkerPermissionEngine requires a non-None "
                "workspace_root for path sandbox. Received None."
            )
        if not callable(audit_sink):
            raise TypeError(
                f"audit_sink must be callable, "
                f"got {type(audit_sink).__name__}"
            )
        self.workspace_root: Path = Path(workspace_root).resolve()
        self.audit_sink: AuditSink = audit_sink
        _LOGGER.debug(
            "OIagentCoworkerPermissionEngine initialized: workspace_root=%s",
            self.workspace_root,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, action: Action, ctx: PermissionContext) -> Verdict:
        """Main entry: produce a Verdict and drop an audit record.

        Args:
            action: The proposed action.
            ctx: The decision context (mode, task, user, ...).

        Returns:
            A Verdict whose `allow` field decides whether the action may
            proceed. The audit_sink is invoked exactly once per call,
            even when the verdict denies the action.

        Raises:
            TypeError: If action or ctx are of the wrong type.
        """
        if not isinstance(action, Action):
            raise TypeError(
                f"check() requires Action, got {type(action).__name__}"
            )
        if not isinstance(ctx, PermissionContext):
            raise TypeError(
                f"check() requires PermissionContext, "
                f"got {type(ctx).__name__}"
            )

        risk_level = self._classify_risk(action)

        mode = ctx.mode
        if mode is PermissionMode.ASYNC:
            verdict = self._decide_async(action, ctx, risk_level)
        elif mode is PermissionMode.SYNC:
            verdict = self._decide_sync(action, ctx, risk_level)
        elif mode is PermissionMode.PLAN:
            verdict = self._decide_plan(action, ctx, risk_level)
        elif mode is PermissionMode.INTERRUPT:
            verdict = self._decide_interrupt(action, ctx, risk_level)
        elif mode is PermissionMode.COMPACTION:
            verdict = self._decide_compaction(action, ctx, risk_level)
        else:
            # Defensive fallback -- should be unreachable given the Enum.
            verdict = Verdict(
                allow=False,
                mode=mode,
                reason=(
                    f"unknown PermissionMode: {mode!r}; refusing as a "
                    f"precaution and routing to user approval"
                ),
                risk_level=risk_level,
                requires_approval=True,
            )

        # Audit (best-effort; never let audit failure break the verdict).
        # W2-1.3: wrap verdict in AuditDecision so the new typed
        # AuditSink Protocol is satisfied. Action context is preserved
        # inside the Verdict's reason metadata; the envelope's
        # ``engine_decision`` field is the source of truth.
        try:
            self.audit_sink(
                AuditDecision(
                    kind="permission",
                    timestamp=datetime.now(UTC),
                    engine_decision=verdict,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- audit must not break verdict
            _LOGGER.warning(
                "audit_sink raised %s for action=%s; verdict=%s",
                exc, action, verdict,
            )
        return verdict

    # ------------------------------------------------------------------
    # Mode-specific decisions
    # ------------------------------------------------------------------

    def _decide_async(
        self,
        action: Action,
        ctx: PermissionContext,
        risk_level: RiskLevel,
    ) -> Verdict:
        """ASYNC: background, non-blocking.

        read/write/exec: allow (no approval needed). Side effects audited.
        destructive: always requires_approval; allow=False.
        """
        if risk_level == "destructive":
            return Verdict(
                allow=False,
                mode=PermissionMode.ASYNC,
                reason=(
                    f"destructive action '{action.kind}' on '{action.target}' "
                    f"cannot run async without explicit user approval"
                ),
                risk_level=risk_level,
                requires_approval=True,
            )
        return Verdict(
            allow=True,
            mode=PermissionMode.ASYNC,
            reason=(
                f"async execution allowed for {risk_level} action "
                f"'{action.kind}' on '{action.target}'"
            ),
            risk_level=risk_level,
            requires_approval=False,
        )

    def _decide_sync(
        self,
        action: Action,
        ctx: PermissionContext,
        risk_level: RiskLevel,
    ) -> Verdict:
        """SYNC: same-thread synchronous execution.

        read: allow unconditionally.
        write/exec: allow only if target within workspace_root AND
            ctx.force_strict is not set.
        destructive: requires_approval; allow=False.
        """
        if risk_level == "destructive":
            return Verdict(
                allow=False,
                mode=PermissionMode.SYNC,
                reason=(
                    f"destructive action '{action.kind}' on '{action.target}' "
                    f"requires explicit user approval even in sync mode"
                ),
                risk_level=risk_level,
                requires_approval=True,
            )
        if ctx.force_strict and risk_level in ("write", "exec"):
            return Verdict(
                allow=False,
                mode=PermissionMode.SYNC,
                reason=(
                    f"force_strict escalation: {risk_level} action "
                    f"'{action.kind}' on '{action.target}' requires approval"
                ),
                risk_level=risk_level,
                requires_approval=True,
            )
        if risk_level in ("write", "exec") and not self._target_within_workspace(action.target):
            return Verdict(
                allow=False,
                mode=PermissionMode.SYNC,
                reason=(
                    f"target '{action.target}' is outside workspace_root "
                    f"'{self.workspace_root}'; sync {risk_level} refused"
                ),
                risk_level=risk_level,
                requires_approval=True,
            )
        return Verdict(
            allow=True,
            mode=PermissionMode.SYNC,
            reason=(
                f"sync execution allowed for {risk_level} action "
                f"'{action.kind}' on '{action.target}'"
            ),
            risk_level=risk_level,
            requires_approval=False,
        )

    def _decide_plan(
        self,
        action: Action,
        ctx: PermissionContext,
        risk_level: RiskLevel,
    ) -> Verdict:
        """PLAN: never execute without explicit user approval.

        Always allow=False, requires_approval=True. The reason must
        contain the phrase "user approval" (test contract §8.1.1).
        """
        return Verdict(
            allow=False,
            mode=PermissionMode.PLAN,
            reason=(
                f"plan mode requires user approval before executing "
                f"{risk_level} action '{action.kind}' on '{action.target}'"
            ),
            risk_level=risk_level,
            requires_approval=True,
        )

    def _decide_interrupt(
        self,
        action: Action,
        ctx: PermissionContext,
        risk_level: RiskLevel,
    ) -> Verdict:
        """INTERRUPT: allow execution but user can halt mid-flight.

        read/write: allow.
        exec: allow only if target within workspace_root.
        destructive: requires_approval; allow=False.
        """
        if risk_level == "destructive":
            return Verdict(
                allow=False,
                mode=PermissionMode.INTERRUPT,
                reason=(
                    f"destructive action '{action.kind}' on '{action.target}' "
                    f"requires user approval even with interrupt capability"
                ),
                risk_level=risk_level,
                requires_approval=True,
            )
        if risk_level == "exec" and not self._target_within_workspace(action.target):
            return Verdict(
                allow=False,
                mode=PermissionMode.INTERRUPT,
                reason=(
                    f"exec target '{action.target}' is outside workspace_root "
                    f"'{self.workspace_root}'; interrupt mode refuses"
                ),
                risk_level=risk_level,
                requires_approval=True,
            )
        return Verdict(
            allow=True,
            mode=PermissionMode.INTERRUPT,
            reason=(
                f"interrupt-mode execution allowed for {risk_level} action "
                f"'{action.kind}' on '{action.target}' (user may halt)"
            ),
            risk_level=risk_level,
            requires_approval=False,
        )

    def _decide_compaction(
        self,
        action: Action,
        ctx: PermissionContext,
        risk_level: RiskLevel,
    ) -> Verdict:
        """COMPACTION: long-conversation compression window (read-only).

        read: allow (compaction must read its own context).
        write/exec/destructive: strict refusal (not approval, just refuse).
        """
        if risk_level != "read":
            return Verdict(
                allow=False,
                mode=PermissionMode.COMPACTION,
                reason=(
                    f"compaction mode forbids {risk_level} action "
                    f"'{action.kind}' on '{action.target}' "
                    f"(compaction is read-only)"
                ),
                risk_level=risk_level,
                requires_approval=False,
            )
        return Verdict(
            allow=True,
            mode=PermissionMode.COMPACTION,
            reason=(
                f"compaction-mode read allowed for '{action.kind}' "
                f"on '{action.target}'"
            ),
            risk_level=risk_level,
            requires_approval=False,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _classify_risk(self, action: Action) -> RiskLevel:
        """Classify an Action into read/write/exec/destructive.

        Precedence: explicit destructive kind > destructive shell pattern
        in target > exec > write > read. Unknown kinds default to "exec"
        (safest refusal under SYNC).

        Cross-platform coverage:
            - POSIX:       rm -rf /, rm -rf ~
            - PowerShell:  Remove-Item -Recurse, del /s /q, rmdir /s /q
            - Generic:     format C:, dd of=/dev/sda
        """
        kind = action.kind.lower().strip()
        target = action.target or ""

        if kind in _DESTRUCTIVE_KINDS:
            return "destructive"
        if self._looks_destructive(target):
            return "destructive"
        if kind in _EXEC_KINDS:
            return "exec"
        if kind in _WRITE_KINDS:
            return "write"
        if kind in _READ_KINDS or not kind:
            return "read"
        return "exec"

    def _looks_destructive(self, target: str) -> bool:
        """Return True if `target` matches any cross-platform destructive
        shell pattern."""
        if not target:
            return False
        return any(pat.search(target) for pat in _DESTRUCTIVE_PATTERNS)

    def _target_within_workspace(self, target: str) -> bool:
        """First-line workspace containment check.

        Full path sandbox (symlink/hardlink escape, TOCTOU, .git/
        traversal) lives in W2-1.2 path_sandbox.py. Here we do a
        resolved-path containment check so the decision table refuses
        obviously out-of-workspace targets without needing the heavier
        sandbox.

        Bare commands (e.g. "echo hi") are deferred to shell_classifier
        (W2-1.2); for those, this check returns True (no opinion).
        """
        if not target:
            return True
        # Bare commands have no leading slash, drive letter, or backslash.
        if not (target.startswith(("/", "\\")) or re.match(r"^[a-zA-Z]:[\\/]", target)):
            return True
        try:
            target_path = Path(target).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        try:
            target_path.relative_to(self.workspace_root)
            return True
        except ValueError:
            return False