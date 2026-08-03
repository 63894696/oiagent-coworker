# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/tools/bash.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package and expanded classification to three explicit risk tiers.
#   - Added PowerShell, cmd.exe, package-management, and source-control rules.
#   - Reused the W2-1.1 destructive guard as the first-line rule set.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original copyright and permission notices are retained.

"""Cross-shell command risk classification for OIagent Coworker.

Anti-flattery boundary (see plan §3.1):
    - No upstream-package import; attribution is retained in the SPDX header.
    - No OAuth broker, MCP server runtime, or Tauri shell calls.
    - No vendor LLM SDK imports or API calls.
    - Borrowed design (MIT, see SPDX header), not runtime integration.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from oiagent_coworker.permissions.engine import _DESTRUCTIVE_PATTERNS, AuditSink

__all__ = [
    "OIagentCoworkerShellClassifier",
    "ShellClassification",
    "ShellRiskLevel",
]

_LOGGER = logging.getLogger(__name__)

ShellKind = Literal["bash", "pwsh", "cmd", "sh", "zsh", "fish", "unknown"]


class ShellRiskLevel(Enum):
    """Approval-oriented command risk tier."""

    SAFE = "safe"
    RISKY = "risky"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ShellClassification:
    """Immutable classification suitable for permission audit logging."""

    risk_level: ShellRiskLevel
    matched_patterns: tuple[str, ...]
    target_normalized: str
    shell_kind: ShellKind
    requires_approval: bool
    rationale: str


# The first eight entries intentionally reference the compiled W2-1.1 patterns.
_DESTRUCTIVE_NAMES = (
    "rm_root", "rm_home_root", "rm_current_dir", "del_recursive",
    "del_force_quiet", "rmdir_force_quiet", "format_drive", "dd_overwrite",
)
_DESTRUCTIVE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    zip(_DESTRUCTIVE_NAMES, _DESTRUCTIVE_PATTERNS, strict=True)
) + (
    ("mkfs", re.compile(r"\bmkfs(\.[a-z0-9]+)?\b", re.IGNORECASE)),
    ("shutdown", re.compile(
        r"\b(shutdown|poweroff|halt|reboot|init\s+[0-6])\b", re.IGNORECASE
    )),
    ("forkbomb", re.compile(r":\(\)\s*\{.*\|.*:.*&.*\};:")),
)

_RISKY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("curl", re.compile(r"\bcurl\b", re.IGNORECASE)),
    ("wget", re.compile(r"\bwget\b", re.IGNORECASE)),
    ("chmod", re.compile(r"\bchmod\s+(?:-[rRwxXstugo]+\s+)?[0-7]{3,4}\b")),
    ("chown", re.compile(r"\bchown\s+")),
    ("rm_file", re.compile(r"\brm\s+(?!-rf?\s+(?:/|~|\.))")),
    ("rmdir", re.compile(r"\brmdir\b", re.IGNORECASE)),
    ("kill", re.compile(r"\b(kill|killall|pkill)\s+-[0-9]+\b")),
    ("sudo", re.compile(r"\bsudo\b")),
    ("git_push_force", re.compile(
        r"\bgit\s+push\b[^|;&]*(?:-f(?:\s|$)|--force(?!-with-lease)\b)"
    )),
    ("git_reset_hard", re.compile(r"\bgit\s+reset\s+--hard\b")),
    ("git_diff_no_index", re.compile(r"\bgit\s+diff\b[^|;&]*--no-index\b")),
    ("npm_global_install", re.compile(r"\bnpm\s+(install|i)\s+-g\b")),
    ("pip_install", re.compile(r"\bpip(\d+)?\s+install\b")),
    ("apt_install", re.compile(
        r"\b(apt|apt-get|yum|dnf|pacman)\s+install\b"
    )),
    ("mount", re.compile(r"\bmount\s+")),
    ("umount", re.compile(r"\bumount\s+")),
    ("iptables", re.compile(r"\biptables\b")),
    ("systemctl", re.compile(
        r"\bsystemctl\s+(start|stop|restart|enable|disable)\b"
    )),
    ("set_execution_policy", re.compile(r"\bSet-ExecutionPolicy\b", re.IGNORECASE)),
)

_SAFE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("echo", re.compile(r"\becho\s+")),
    ("cat", re.compile(r"\bcat\s+")),
    ("ls", re.compile(r"\bls(?:\s+|$|-)")),
    ("grep", re.compile(r"\bgrep\s+")),
    ("head", re.compile(r"\bhead\s+")),
    ("tail", re.compile(r"\btail\s+")),
    ("wc", re.compile(r"\bwc\s+")),
    ("pwd", re.compile(r"\bpwd\s*$")),
    ("which", re.compile(r"\b(which|type)\s+")),
    ("date", re.compile(r"\bdate(?:\s|$)")),
    ("env_print", re.compile(r"\benv(?:\s|$|\|)")),
    ("git_status", re.compile(r"\bgit\s+(status|log|diff|show|branch)\b")),
    ("git_diff_safe", re.compile(r"\bgit\s+diff\b(?![^|;&]*--no-index)")),
    ("pytest", re.compile(r"\b(pytest|ruff|mypy|black|flake8)\b")),
)


class OIagentCoworkerShellClassifier:
    """Classify shell command strings, with destructive precedence."""

    def __init__(self, audit_sink: AuditSink | None = None) -> None:
        self.audit_sink = audit_sink

    def classify(
        self,
        command: str,
        shell_hint: str = "unknown",
    ) -> ShellClassification:
        """Return a classification; malformed inputs conservatively become risky."""
        if not isinstance(command, str):
            command = str(command)
        normalized = command.strip().lower()
        shell_kind = self._detect_shell_kind(command, shell_hint)

        destructive = self._matches(command, _DESTRUCTIVE_RULES)
        if destructive:
            result = self._result(
                ShellRiskLevel.DESTRUCTIVE, destructive, normalized, shell_kind
            )
        else:
            risky = self._matches(command, _RISKY_RULES)
            if risky:
                result = self._result(
                    ShellRiskLevel.RISKY, risky, normalized, shell_kind
                )
            else:
                safe = self._matches(command, _SAFE_RULES)
                result = self._result(
                    ShellRiskLevel.SAFE, safe, normalized, shell_kind
                )
        self._audit(result, command)
        return result

    def is_safe(self, command: str) -> bool:
        """Return True only for commands classified SAFE."""
        return self.classify(command).risk_level is ShellRiskLevel.SAFE

    def is_destructive(self, command: str) -> bool:
        """Return True only for commands classified DESTRUCTIVE."""
        return self.classify(command).risk_level is ShellRiskLevel.DESTRUCTIVE

    @staticmethod
    def _matches(
        command: str,
        rules: tuple[tuple[str, re.Pattern[str]], ...],
    ) -> tuple[str, ...]:
        return tuple(name for name, pattern in rules if pattern.search(command))

    @staticmethod
    def _result(
        level: ShellRiskLevel,
        matches: tuple[str, ...],
        normalized: str,
        shell_kind: ShellKind,
    ) -> ShellClassification:
        if matches:
            rationale = f"Matched pattern '{matches[0]}' in {level.value} set"
        else:
            rationale = "No risky or destructive pattern matched; defaulted to safe"
        return ShellClassification(
            risk_level=level,
            matched_patterns=matches,
            target_normalized=normalized,
            shell_kind=shell_kind,
            requires_approval=level is not ShellRiskLevel.SAFE,
            rationale=rationale,
        )

    @staticmethod
    def _detect_shell_kind(command: str, hint: str) -> ShellKind:
        normalized_hint = hint.strip().lower()
        aliases: dict[str, ShellKind] = {
            "bash": "bash", "pwsh": "pwsh", "powershell": "pwsh",
            "cmd": "cmd", "cmd.exe": "cmd", "sh": "sh", "zsh": "zsh",
            "fish": "fish", "unknown": "unknown",
        }
        if normalized_hint != "unknown" and normalized_hint in aliases:
            return aliases[normalized_hint]
        stripped = command.lstrip().lower()
        if stripped.startswith("#!"):
            first_line = stripped.splitlines()[0]
            for shell in ("pwsh", "zsh", "fish", "bash", "sh"):
                if shell in first_line:
                    return shell  # type: ignore[return-value]
        if stripped.startswith(("pwsh ", "powershell ")):
            return "pwsh"
        if stripped.startswith(("cmd /c", "cmd.exe /c")):
            return "cmd"
        return "bash"

    def _audit(self, result: ShellClassification, command: str) -> None:
        if self.audit_sink is not None:
            try:
                self.audit_sink(result, command)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 -- audit must not break policy
                _LOGGER.warning("audit_sink failed for shell classification: %s", exc)
