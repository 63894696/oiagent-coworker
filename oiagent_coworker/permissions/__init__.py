# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/__init__.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     broad re-export surface with a curated public API for the
#     permissions subsystem.
#   - Added PolicyGate (P0-3 compat layer: LegacyPolicyEngine protocol,
#     PolicyGate router, PolicyGateMode, VerdictDiff) to the public
#     surface; path_sandbox / shell_classifier / persistence stay
#     internal to keep the curated surface minimal.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Permissions package for OIagent Coworker (W2-1 + P0-3 gate).

Public API:

    * :class:`Action`, :class:`PermissionContext`, :class:`Verdict` --
      the engine's core dataclasses.
    * :class:`PermissionMode` -- five-mode permission state machine.
    * :class:`OIagentCoworkerPermissionEngine` -- five-mode permission
      decision engine (W2-1.1).
    * :class:`AuditDecision`, :class:`AuditSink`,
      :class:`OIagentCoworkerAuditFacade` -- typed audit envelope +
      facade (W2-1.3 / W2-1.4).
    * :class:`LegacyPolicyEngine`, :class:`PolicyGate`,
      :class:`PolicyGateMode`, :class:`VerdictDiff` -- P0-3 PolicyGate
      compat layer (hot-read feature-flag routing between the legacy
      PolicyEngine and the new engine, with shadow-mode verdict diffing).

Anti-flattery boundary (see plan §3.1 / §3.2):
    - No ``import openworker`` anywhere in this package.
    - No ``openai`` / ``anthropic`` direct SDK calls.
    - The gate NEVER imports ``oiagent.policy``; the legacy engine is
      duck-typed via the ``LegacyPolicyEngine`` Protocol.
"""

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
from oiagent_coworker.permissions.policy_gate import (
    LegacyPolicyEngine,
    PolicyGate,
    PolicyGateMode,
    VerdictDiff,
)

__all__ = [
    "Action",
    "AuditDecision",
    "AuditSink",
    "LegacyPolicyEngine",
    "OIagentCoworkerAuditFacade",
    "OIagentCoworkerPermissionEngine",
    "PermissionContext",
    "PermissionMode",
    "PolicyGate",
    "PolicyGateMode",
    "Verdict",
    "VerdictDiff",
]
