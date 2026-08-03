# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/skills/models.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     broad skill descriptor with a focused set of four frozen dataclasses:
#     SkillStatus (enum), SkillSpec (immutable spec), Skill (runtime
#     instance), and an internal _utcnow helper.
#   - The upstream SKILL.md folder-as-truth loader is dropped; this
#     module only holds the data model used by persistence.py and
#     service.py.
#   - The upstream stage_confirm gate is replaced by the OIagent-only
#     gate in stage_confirm.py (W2-5.2); this module does not invoke it
#     directly.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Skill data models for OIagent Coworker (W2-5).

This module defines four frozen dataclasses that represent the
de-serialized contract of a skill within the OIagent Coworker daemon.

* :class:`SkillStatus` — the live state of a registered skill.
* :class:`SkillSpec` — the immutable user-facing definition (name,
  version, entrypoint, optional config / metadata).
* :class:`Skill` — the fully-resolved runtime instance produced by
  :class:`OIagentCoworkerSkillsService`.

Freezing semantics
------------------

All four dataclasses are ``@dataclass(frozen=True)`` so that the
instance reference itself is immutable. The ``config`` and
``metadata`` fields use ``dict[str, Any]`` (mutable dicts) because
the service contract requires that callers may update the skill
configuration at runtime via :meth:`~OIagentCoworkerSkillsService.update_skill_status`
and :meth:`~OIagentCoworkerSkillsService.load_skill_module` mutations
(see service.py).  The frozen outer wrapper prevents accidental
replacement of the entire field, not individual key mutations.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this module.
    - No SKILL.md frontmatter / scope resolution.
    - Borrowed design (dataclass shape), not runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

__all__ = ["Skill", "SkillSpec", "SkillStatus"]


class SkillStatus(str, Enum):
    """Live state of a registered :class:`Skill`."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    BROKEN = "broken"


@dataclass(frozen=True)
class SkillSpec:
    """Immutable skill definition submitted by the user.

    Attributes:
        name: Human-readable skill name (e.g. ``"web-search"``).
        version: Semantic version string (e.g. ``"1.0.0"``).
        description: One-line human-readable summary of what the skill
            does.
        entrypoint: Python dotted module path (e.g. ``"skills.web_search"``)
            that the service will ``importlib.import_module`` on first
            use.
        config: Optional skill-specific configuration dictionary.
        metadata: Optional free-form auxiliary metadata dictionary.
    """

    name: str
    version: str
    description: str
    entrypoint: str
    config: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Skill:
    """Fully-resolved runtime instance of a skill.

    A :class:`Skill` is produced by :meth:`OIagentCoworkerSkillsService.register_skill`
    and is the unit that the service tracks in its in-memory registry.

    Attributes:
        skill_id: UUID4 hex string assigned at registration time.
        spec: The :class:`SkillSpec` that produced this instance.
        status: Current :class:`SkillStatus` (defaults to ``ACTIVE``).
        loaded_at: UTC datetime when this skill instance was created.
        last_used_at: UTC datetime of the most recent successful use,
            or ``None`` if the skill has never been used.
        error: Optional human-readable error message (set by the
            service when a module load or execution fails).
        metadata: Free-form auxiliary metadata (copies from ``spec``
            at creation time, may be mutated by the service).
    """

    skill_id: str
    spec: SkillSpec
    status: SkillStatus = SkillStatus.ACTIVE
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _utcnow() -> datetime:
    """Return a timezone-aware UTC :class:`datetime`."""
    return datetime.now(UTC)
