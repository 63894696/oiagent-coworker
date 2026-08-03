# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/skills/__init__.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     broad re-export surface with a curated public API for the skills
#     subsystem.
#   - W2-5.1: added re-export of loader module (SkillSource, SkillEntry,
#     OIagentCoworkerSkillLoader) for SKILL.md folder-as-truth discovery.
#   - W2-5.2: stage_confirm gate added, wired to PolicyGate (P0-3 §5) instead of Tauri dialog; upstream had no stage_confirm (OIagent-only).
#   - W2-5.3: manifest.py added (SKILL.md body digest + e2e_overlay declaration check); capability-04 mounted as a SKILL.md fixture (OIagent-only).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Skills package for OIagent Coworker (W2-5).

Public API:

    * :class:`SkillStatus` -- the live state enum for a skill.
    * :class:`SkillSpec` -- immutable user-facing skill definition.
    * :class:`Skill` -- fully-resolved runtime skill instance.
    * :class:`OIagentCoworkerSkillsPersistence` -- JSONL persistence
      layer for skill definitions and replay.
    * :class:`OIagentCoworkerSkillsService` -- business core; owns the
      skill registry, lazy module loading, and audit emission.
    * :class:`OIagentCoworkerSkillLoader` -- SKILL.md folder-as-truth
      discovery with global/project/user scope resolution (W2-5.1).

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this package.
    - Markdown-body parsing lives only in manifest.py (W2-5.3); loader stays frontmatter-only. No OIagent approval/policy-layer imports — the stage_confirm gate is duck-typed and injected (W2-5.2).
    - Borrowed design (registry + lazy import + folder-as-truth), not runtime.
"""

from oiagent_coworker.skills.loader import (
    OIagentCoworkerSkillLoader,
    SkillEntry,
    SkillSource,
)
from oiagent_coworker.skills.manifest import (
    E2E_OVERLAY_KEY,
    OIagentCoworkerSkillManifest,
    OverlayDeclarationError,
    SkillBody,
    SkillManifest,
)
from oiagent_coworker.skills.models import Skill, SkillSpec, SkillStatus
from oiagent_coworker.skills.persistence import OIagentCoworkerSkillsPersistence
from oiagent_coworker.skills.service import OIagentCoworkerSkillsService
from oiagent_coworker.skills.stage_confirm import (
    OIagentCoworkerStageConfirm,
    StageConfirmDenied,
    StageConfirmResult,
    build_upload_action,
    invoke_skill_with_confirm,
)

__all__ = [
    "E2E_OVERLAY_KEY",
    "OIagentCoworkerSkillLoader",
    "OIagentCoworkerSkillManifest",
    "OIagentCoworkerSkillsPersistence",
    "OIagentCoworkerSkillsService",
    "OIagentCoworkerStageConfirm",
    "OverlayDeclarationError",
    "Skill",
    "SkillBody",
    "SkillEntry",
    "SkillManifest",
    "SkillSpec",
    "SkillStatus",
    "SkillSource",
    "StageConfirmDenied",
    "StageConfirmResult",
    "build_upload_action",
    "invoke_skill_with_confirm",
]
