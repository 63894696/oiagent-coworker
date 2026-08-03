# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/skills/service.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; merged upstream
#     loader and registry logic into a single service class.
#   - The upstream SKILL.md folder-as-truth loader and scope resolver
#     are dropped; this service only manages in-memory skill registry
#     backed by a JSONL persistence layer.
#   - The upstream stage_confirm gate is replaced by the OIagent-only
#     gate in stage_confirm.py (W2-5.2); this module does not invoke it
#     directly.
#   - Added audit-sink integration: every register / update / load /
#     unload / delete emits an :class:`AuditDecision` envelope.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Skill service for OIagent Coworker (W2-5).

This module implements :class:`OIagentCoworkerSkillsService`, which
manages the lifecycle of :class:`Skill` objects: registering, querying,
updating status, and lazy module loading.

The service is intended to be a singleton-like object owned by the
OIagent Coworker daemon. It is thread-safe for concurrent reads and
writes via a single :class:`threading.RLock`.

Audit integration
-----------------

On every state change the service emits an
:class:`~oiagent_coworker.permissions.audit.AuditDecision` envelope
with ``kind='skill'`` via the injected ``audit_sink``. Consumers
should be prepared to handle the new ``"skill"`` kind in their
``AuditKind`` Literal (W2-5 extends it in :mod:`permissions.audit`).

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this module.
    - No SKILL.md parsing; no stage_confirm gate.
    - Borrowed design (registry + lazy import), not runtime.
"""

from __future__ import annotations

import importlib
import logging
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oiagent_coworker.skills.models import Skill, SkillSpec, SkillStatus
from oiagent_coworker.skills.persistence import OIagentCoworkerSkillsPersistence

if TYPE_CHECKING:
    from oiagent_coworker.permissions.audit import AuditSink

__all__ = ["OIagentCoworkerSkillsService"]

_LOGGER = logging.getLogger(__name__)


class OIagentCoworkerSkillsService:
    """Service managing the skill registry and lazy module loading.

    Thread safety:
        All public methods are thread-safe. The class uses a single
        :class:`threading.RLock` to protect the internal ``_skills``
        dict and the ``_modules`` cache. Reads are fast and lock
        contention is minimal after initial load.

    Attributes:
        persistence: The persistence layer used to store and replay
            skill events.
        audit_sink: Optional audit sink for emitting
            :class:`~oiagent_coworker.permissions.audit.AuditDecision`
            envelopes. ``None`` disables auditing.
        _skills: A dictionary mapping skill ids to :class:`Skill`
            objects (replayed from the persistence layer at
            construction time).
        _modules: A dictionary mapping skill ids to loaded module
            objects (or ``None`` if the skill has no module or the
            module failed to load).
        _lock: A reentrant lock guarding access to ``_skills`` and
            ``_modules``.
    """

    def __init__(
        self,
        storage_path: Path,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self.persistence = OIagentCoworkerSkillsPersistence(storage_path)
        self._audit_sink = audit_sink
        self._skills: dict[str, Skill] = {}
        self._modules: dict[str, object | None] = {}
        self._lock = threading.RLock()
        self._load_from_persistence()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_skill(
        self,
        name: str,
        version: str,
        description: str,
        entrypoint: str,
        config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Skill:
        """Register a new skill and persist it.

        Args:
            name: Human-readable skill name.
            version: Semantic version string.
            description: One-line human-readable summary.
            entrypoint: Python dotted module path (e.g.
                ``"skills.web_search"``).
            config: Optional skill-specific configuration.
            metadata: Optional free-form auxiliary metadata.

        Returns:
            The newly created :class:`Skill`.
        """
        spec = SkillSpec(
            name=name,
            version=version,
            description=description,
            entrypoint=entrypoint,
            config=dict(config) if config else {},
            metadata=dict(metadata) if metadata else {},
        )
        skill = Skill(
            skill_id=uuid.uuid4().hex,
            spec=spec,
        )
        with self._lock:
            self._skills[skill.skill_id] = skill
            self._modules[skill.skill_id] = None
        self.persistence.append_skill(skill)
        self._emit_audit("register", skill)
        _LOGGER.info("Registered skill %s (id=%s, entrypoint=%s)", name, skill.skill_id, entrypoint)
        return skill

    def get_skill(self, skill_id: str) -> Skill | None:
        """Return the :class:`Skill` object for the given id.

        Args:
            skill_id: The UUID4 hex of the skill (case-sensitive).

        Returns:
            The :class:`Skill` if found, otherwise ``None``.
        """
        with self._lock:
            return self._skills.get(skill_id)

    def list_skills(self, status: SkillStatus | None = None) -> list[Skill]:
        """Return a list of all known skills.

        Args:
            status: Optional filter — only return skills matching
                this status. Pass ``None`` for no filter.

        Returns:
            A list of :class:`Skill` objects (not sorted).
        """
        with self._lock:
            if status is None:
                return list(self._skills.values())
            return [s for s in self._skills.values() if s.status == status]

    def update_skill_status(
        self,
        skill_id: str,
        status: SkillStatus,
    ) -> bool:
        """Update the status of an existing skill.

        Args:
            skill_id: The UUID4 hex of the skill.
            status: The new :class:`SkillStatus`.

        Returns:
            ``True`` if the skill was found and updated, ``False``
            otherwise.
        """
        with self._lock:
            skill = self._skills.get(skill_id)
            if skill is None:
                return False
            # Skill is frozen; create a new instance with the updated status.
            updated = Skill(
                skill_id=skill.skill_id,
                spec=skill.spec,
                status=status,
                loaded_at=skill.loaded_at,
                last_used_at=skill.last_used_at,
                error=skill.error,
                metadata=dict(skill.metadata),
            )
            self._skills[skill_id] = updated
        self.persistence.update_skill(updated)
        self._emit_audit("update_status", updated)
        _LOGGER.info("Updated skill %s status -> %s", skill_id, status.value)
        return True

    def load_skill_module(self, skill_id: str) -> object | None:
        """Load and cache the Python module for the given skill.

        If the skill's ``spec.entrypoint`` is set, this method imports
        the module and caches it under ``skill_id``. Subsequent calls
        return the cached module.

        Args:
            skill_id: The UUID4 hex of the skill.

        Returns:
            The module object if the skill has an entrypoint and it
            was successfully loaded; otherwise ``None``.
        """
        with self._lock:
            skill = self._skills.get(skill_id)
            if not skill:
                return None
            if skill.spec.entrypoint is None or skill.spec.entrypoint == "":
                return None
            if skill_id not in self._modules:
                try:
                    module = importlib.import_module(skill.spec.entrypoint)
                    self._modules[skill_id] = module
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to load module %s for skill %s: %s",
                        skill.spec.entrypoint,
                        skill_id,
                        exc,
                    )
                    self._modules[skill_id] = None
            return self._modules[skill_id]

    def unload_skill_module(self, skill_id: str) -> bool:
        """Remove the cached module for the given skill.

        Args:
            skill_id: The UUID4 hex of the skill.

        Returns:
            ``True`` if a module was found and removed, ``False``
            otherwise.
        """
        with self._lock:
            if skill_id not in self._modules:
                return False
            self._modules.pop(skill_id)
        self._emit_audit("unload_module", self._skills.get(skill_id))
        _LOGGER.info("Unloaded module for skill %s", skill_id)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_from_persistence(self) -> None:
        """Rebuild the in-memory registry from the JSONL log."""
        try:
            skills = list(self.persistence.replay())
            self._skills = {s.skill_id: s for s in skills}
            # Initialize module cache to None for each skill.
            self._modules = {skill_id: None for skill_id in self._skills}
            _LOGGER.info(
                "Loaded %d skills from persistence at %s",
                len(skills),
                self.persistence._path,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Failed to load skills from persistence: %s", exc)
            self._skills = {}
            self._modules = {}

    def _emit_audit(self, action: str, skill: Skill | None = None) -> None:
        """Emit an AuditDecision envelope if a sink is wired."""
        if self._audit_sink is None:
            return
        try:
            from oiagent_coworker.permissions.audit import AuditDecision, _utcnow

            payload: dict[str, Any] = {"action": action}
            if skill is not None:
                payload["skill_id"] = skill.skill_id
                payload["skill_name"] = skill.spec.name
                payload["skill_status"] = skill.status.value
            decision = AuditDecision(
                kind="skill",
                timestamp=_utcnow(),
                metadata=payload,
            )
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Audit emit failed: %s", exc)
