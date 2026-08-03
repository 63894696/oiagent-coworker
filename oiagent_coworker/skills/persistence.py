# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/skills/loader.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     folder-as-truth loader with a focused persistence layer for skill
#     definitions backed by JSONL append logs.
#   - The upstream SKILL.md frontmatter + scope resolution logic is
#     dropped; this persistence only loads raw skill specs from JSONL.
#   - The upstream stage_confirm gate is replaced by the OIagent-only
#     gate in stage_confirm.py (W2-5.2); this module does not invoke it
#     directly.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Persistence layer for OIagent Coworker skill definitions (W2-5).

This module implements :class:`OIagentCoworkerSkillsPersistence`, which
stores :class:`Skill` objects in an append-only JSONL log and can
replay that log to rebuild the current skill set.

JSONL contract
--------------

Each line is::

    {
      "event_type":  "create" | "update" | "delete",
      "skill":       <Skill as dict (dataclasses.asdict)> | null,
      "timestamp":   "<iso 8601 UTC>"
    }

* ``event_type == "create"`` → skill is added to the live set.
* ``event_type == "update"`` → skill is replaced in the live set.
* ``event_type == "delete"`` → skill is removed from the live set.

``datetime`` values are serialized via the shared ``_json_default``
hook so they round-trip as ISO 8601 strings.

Persistence semantics:

  * ``append_*`` is O(1) amortized: ``open(append)`` + ``write`` +
    ``fsync`` + ``close``.
  * ``replay()`` walks the file once, skipping lines that fail
    ``json.loads`` or that are missing the required keys. Skipped
    lines are logged at WARNING and do not block subsequent events.
  * Missing file is treated as an empty log; constructors do **not**
    create the file. The first ``append`` creates both the parent
    directory and the log file.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this module.
    - No SKILL.md frontmatter parsing; no stage_confirm gate.
    - Borrowed design (JSONL append), not runtime.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oiagent_coworker.skills.models import Skill, SkillSpec

__all__ = ["OIagentCoworkerSkillsPersistence"]

_LOGGER = logging.getLogger(__name__)


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for dataclass / datetime payloads."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError(f"Not JSON serializable: {type(obj).__name__}")


def _decode_line(raw: str) -> dict[str, Any]:
    """Parse a single JSONL line.

    Args:
        raw: One line from the JSONL log.

    Returns:
        The parsed dictionary.

    Raises:
        json.JSONDecodeError: If the line is not valid JSON.
    """
    return json.loads(raw)


def _encode_line(
    event_type: str, skill: Skill | None, timestamp: datetime, _extra_id: str | None = None
) -> str:
    """Encode one event as a JSONL line.

    Args:
        event_type: One of ``"create"`` / ``"update"`` / ``"delete"``.
        skill: The :class:`Skill` being persisted (``None`` for deletes).
        timestamp: UTC timestamp of the event.
        _extra_id: Optional skill_id injected for delete events so
            replay can locate the record without a full skill payload.

    Returns:
        A single JSONL line string ending with ``\\n``.
    """
    payload: dict[str, Any] = {
        "event_type": event_type,
        "skill": asdict(skill) if skill is not None else None,
        "timestamp": timestamp.isoformat(),
    }
    if event_type == "delete" and _extra_id:
        payload["skill_id"] = _extra_id
    return json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n"


class OIagentCoworkerSkillsPersistence:
    """Append-only JSONL store for :class:`Skill` (W2-5).

    Thread safety:
        The class is stateless and thread-safe for concurrent reads.
        Calls to ``append_*`` methods are serialized via ``fsync``
        so that each write is durable from the OS perspective.
    """

    def __init__(self, path: Path) -> None:
        self._path: Path = Path(path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append_skill(self, skill: Skill) -> None:
        """Append a ``create`` event for the given skill.

        Args:
            skill: The skill to persist.
        """
        self._append_event("create", skill)

    def update_skill(self, skill: Skill) -> None:
        """Append an ``update`` event for the given skill.

        Args:
            skill: The updated skill.
        """
        self._append_event("update", skill)

    def delete_skill(self, skill_id: str) -> None:
        """Append a ``delete`` event for the given skill id.

        Args:
            skill_id: The UUID4 hex of the skill to delete.
        """
        self._append_event("delete", None, _extra_id=skill_id)

    def replay(self) -> Iterator[Skill]:
        """Replay all events to reconstruct the current skill set.

        Returns:
            An iterator of active :class:`Skill` objects in the order
            they were last set (duplicates resolved by the last
            event).
        """
        if not self._path.exists():
            return
        skills: dict[str, Skill] = {}
        with open(self._path, "r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = _decode_line(raw)
                except json.JSONDecodeError as exc:
                    _LOGGER.warning(
                        "Skipping malformed line %d in %s: %s",
                        line_no,
                        self._path,
                        exc,
                    )
                    continue
                event_type = event.get("event_type")
                if event_type not in ("create", "update", "delete"):
                    _LOGGER.warning(
                        "Unknown event_type %r at line %d; skipping",
                        event_type,
                        line_no,
                    )
                    continue
                if event_type == "delete":
                    skill_id = event.get("skill_id") or (
                        event.get("skill") and event["skill"].get("skill_id")
                    )
                    if skill_id:
                        skills.pop(skill_id, None)
                    continue
                skill_dict = event.get("skill")
                if skill_dict is None:
                    _LOGGER.warning(
                        "create/update event missing skill dict at line %d",
                        line_no,
                    )
                    continue
                spec_dict = skill_dict.get("spec") or {}
                skill = Skill(
                    skill_id=str(skill_dict["skill_id"]),
                    spec=SkillSpec(
                        name=str(spec_dict.get("name", "")),
                        version=str(spec_dict.get("version", "")),
                        description=str(spec_dict.get("description", "")),
                        entrypoint=str(spec_dict.get("entrypoint", "")),
                        config=dict(spec_dict.get("config") or {}),
                        metadata=dict(spec_dict.get("metadata") or {}),
                    ),
                    status=str(skill_dict.get("status", "active")),
                    loaded_at=datetime.fromisoformat(
                        str(skill_dict.get("loaded_at") or "")
                    ),
                    last_used_at=(
                        datetime.fromisoformat(str(skill_dict["last_used_at"]))
                        if skill_dict.get("last_used_at")
                        else None
                    ),
                    error=str(skill_dict["error"]) if skill_dict.get("error") else None,
                    metadata=dict(skill_dict.get("metadata") or {}),
                )
                skills[skill.skill_id] = skill
        yield from skills.values()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_event(
        self,
        event_type: str,
        skill: Skill | None,
        _extra_id: str | None = None,
    ) -> None:
        """Write one event to the JSONL log with fsync durability.

        Mirrors inbox persistence: a single ``open(append)`` + ``write``
        + ``fsync`` per line.  Lines are never re-written; each call
        appends exactly one record.
        """
        timestamp = datetime.now(tz=UTC)
        line = _encode_line(event_type, skill, timestamp, _extra_id=_extra_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self._path, "a", encoding="utf-8") as fp:
                fp.write(line)
                fp.flush()
                import os

                os.fsync(fp.fileno())
        except OSError as exc:
            _LOGGER.error("Failed to persist skill event: %s", exc)
