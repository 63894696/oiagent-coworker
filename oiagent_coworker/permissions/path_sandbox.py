# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/path_sandbox.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package; replaced the upstream audit sink with injected AuditSink.
#   - Hardened workspace validation with absolute resolution and safe tilde expansion.
#   - Added explicit symlink, absolute-path, and case-insensitive decisions.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original copyright and permission notices are retained.

"""Workspace path isolation for OIagent Coworker.

Anti-flattery boundary (see plan §3.1):
    - No upstream-package imports; attribution is retained only in SPDX metadata.
    - No OAuth broker, MCP server runtime, or Tauri shell integration.
    - No vendor LLM SDK imports.
    - Borrowed design (MIT, see SPDX header), not an external runtime integration.
"""

from __future__ import annotations

import logging
import ntpath
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from oiagent_coworker.permissions.engine import AuditSink

__all__ = [
    "OIagentCoworkerPathSandbox",
    "PathSandboxConfig",
    "SandboxDecision",
    "SandboxReason",
]

_LOGGER = logging.getLogger(__name__)


class SandboxReason(Enum):
    """Reason attached to a path-sandbox decision."""

    ALLOWED = "allowed"
    OUTSIDE_WORKSPACE = "outside"
    TILDE_EXPANSION_ESCAPE = "tilde_esc"
    SYMLINK_ESCAPE = "symlink_esc"
    ABSOLUTE_BYPASS = "absolute_bypass"


@dataclass(frozen=True)
class SandboxDecision:
    """Immutable result of resolving and checking one requested path."""

    allow: bool
    reason: SandboxReason
    resolved_path: Path | None
    original_path: Path
    error: str | None = None


@dataclass(frozen=True)
class PathSandboxConfig:
    """Configuration for workspace path containment."""

    workspace_root: Path
    allow_tilde_expansion: bool = True
    allow_symlinks: bool = False
    case_insensitive: bool = os.name == "nt"


class OIagentCoworkerPathSandbox:
    """Resolve requested paths and confine them to a workspace root."""

    def __init__(
        self,
        config: PathSandboxConfig,
        audit_sink: AuditSink | None = None,
    ) -> None:
        root = Path(config.workspace_root)
        if not root.is_absolute():
            raise ValueError("workspace_root must be an absolute path")
        try:
            resolved_root = root.resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"workspace_root cannot be resolved: {exc}") from exc
        self.config = PathSandboxConfig(
            workspace_root=resolved_root,
            allow_tilde_expansion=config.allow_tilde_expansion,
            allow_symlinks=config.allow_symlinks,
            case_insensitive=config.case_insensitive,
        )
        self.audit_sink = audit_sink

    def sandbox_path(self, requested: str | Path) -> SandboxDecision:
        """Resolve *requested* and return a containment decision without raising."""
        original = Path(requested)
        text = os.fspath(requested)
        has_tilde = "~" in text
        expanded = text
        if has_tilde and self.config.allow_tilde_expansion:
            expanded = os.path.expanduser(text)

        candidate = Path(expanded)
        was_absolute = candidate.is_absolute() or self._is_windows_absolute(expanded)
        if not was_absolute:
            candidate = self.config.workspace_root / candidate

        try:
            had_symlink = self._contains_symlink(candidate)
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            return self._finish(
                SandboxDecision(
                    False,
                    SandboxReason.OUTSIDE_WORKSPACE,
                    None,
                    original,
                    str(exc),
                )
            )

        if self.is_within_workspace(resolved):
            return self._finish(
                SandboxDecision(True, SandboxReason.ALLOWED, resolved, original)
            )

        if has_tilde and self.config.allow_tilde_expansion:
            reason = SandboxReason.TILDE_EXPANSION_ESCAPE
        elif had_symlink and not self.config.allow_symlinks:
            reason = SandboxReason.SYMLINK_ESCAPE
        elif was_absolute and self._is_direct_absolute(text):
            reason = SandboxReason.ABSOLUTE_BYPASS
        else:
            reason = SandboxReason.OUTSIDE_WORKSPACE
        return self._finish(SandboxDecision(False, reason, resolved, original))

    def check_relative(self, rel: str | Path) -> SandboxDecision:
        """Check a path that callers require to be relative to the workspace."""
        original = Path(rel)
        text = os.fspath(rel)
        if original.is_absolute() or self._is_windows_absolute(text):
            return self._finish(
                SandboxDecision(
                    False,
                    SandboxReason.ABSOLUTE_BYPASS,
                    original.resolve(strict=False),
                    original,
                )
            )
        return self.sandbox_path(rel)

    def is_within_workspace(self, abs_path: Path) -> bool:
        """Return whether *abs_path* is contained by the configured root."""
        path = Path(abs_path)
        root = self.config.workspace_root
        if self.config.case_insensitive:
            path_text = self._normalized_case_text(path)
            root_text = self._normalized_case_text(root).rstrip("/")
            return path_text == root_text or path_text.startswith(f"{root_text}/")
        try:
            return path.is_relative_to(root)
        except (OSError, ValueError):
            return False

    def _finish(self, decision: SandboxDecision) -> SandboxDecision:
        if self.audit_sink is not None:
            try:
                self.audit_sink(decision, decision.original_path)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 -- audit must not break policy
                _LOGGER.warning("audit_sink failed for path decision: %s", exc)
        return decision

    @staticmethod
    def _is_windows_absolute(text: str) -> bool:
        drive, tail = ntpath.splitdrive(text)
        return bool(drive and tail.startswith(("\\", "/"))) or text.startswith("\\\\")

    @staticmethod
    def _is_direct_absolute(text: str) -> bool:
        normalized = text.replace("\\", "/")
        return "/../" not in normalized and not normalized.endswith("/..")

    @staticmethod
    def _contains_symlink(path: Path) -> bool:
        current = Path(path.anchor) if path.is_absolute() else Path()
        for part in path.parts[1:] if path.is_absolute() else path.parts:
            current /= part
            try:
                if current.is_symlink():
                    return True
            except OSError:
                return False
        return False

    @staticmethod
    def _normalized_case_text(path: Path) -> str:
        return os.path.normpath(os.fspath(path)).replace("\\", "/").casefold().rstrip("/")
