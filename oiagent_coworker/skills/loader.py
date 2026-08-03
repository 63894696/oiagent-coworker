# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/skills/loader.py
#   Upstream commit:  not present (W2-5.1 loader.py is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W2-5.1; implements SKILL.md
#     folder-as-truth loader with scope resolution (global / project /
#     user priority).
#   - Borrowed design from upstream loader.py (discovery + scope
#     priority) but replaces upstream filesystem heuristics with a
#     focused, pytest-safe implementation.
#   - No upstream commit hash available because the loader module was
#     dropped during W1-1.1 rename; design is documented in the W2
#     extraction plan §205 / §515-518.
#   - Frontmatter parsing mirrors oiagent_coworker.persona.persistence
#     so SKILL.md format is consistent with persona.md.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""SKILL.md folder-as-truth loader for OIagent Coworker skills (W2-5.1).

This module provides :class:`OIagentCoworkerSkillLoader`, which discovers
skills by walking directory trees for ``SKILL.md`` files (the "folder-as-
truth" convention) and resolves scope conflicts when the same skill name
appears at multiple scope levels.

Scope priority
--------------

  * ``global`` — lowest; applies to all workspaces
  * ``project`` — mid; overrides global for a specific workspace
  * ``user`` — highest; overrides both for the current user

When a skill name is registered at multiple scopes, the highest-priority
scope wins.

SKILL.md frontmatter contract
-----------------------------

Each ``SKILL.md`` must contain a YAML frontmatter block (delimited by
``---``) with at least:

  * ``name`` — unique skill identifier
  * ``version`` — semantic version
  * ``description`` — one-line summary
  * ``entrypoint`` — dotted Python module path

Optional frontmatter fields: ``config`` (dict) and ``metadata`` (dict).
The body (markdown after the closing ``---``) is intentionally ignored
by this loader — it may be parsed later by :mod:`manifest` or consumed
by a SKILL.md-aware renderer.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this module.
    - No stage_confirm gate; no aisuite stubs.
    - No asyncio / background thread runtime.
    - Borrowed design (folder-as-truth + scope priority), not runtime.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from oiagent_coworker.skills.models import SkillSpec

__all__ = [
    "SkillEntry",
    "SkillSource",
    "OIagentCoworkerSkillLoader",
]

_LOGGER = logging.getLogger(__name__)

# Regex to match the YAML frontmatter block at the start of a file,
# identical to the one used in oiagent_coworker.persona.persistence so
# SKILL.md and persona.md share the same format contract.
_FRONT_MATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL | re.MULTILINE
)

# Required frontmatter keys for a valid SKILL.md.
_SKILL_REQUIRED_KEYS = {"name", "version", "description", "entrypoint"}


class SkillSource(str, Enum):
    """The scope at which a skill was discovered.

    Scope controls override ordering: USER > PROJECT > GLOBAL.
    """

    GLOBAL = "global"
    PROJECT = "project"
    USER = "user"

    @property
    def priority(self) -> int:
        """Integer priority: higher = more authoritative."""
        return _scope_priority_index(self)


def _scope_priority_index(source: SkillSource) -> int:
    """Return the integer priority for *source*; higher is more authoritative."""
    return {"global": 0, "project": 1, "user": 2}[source.value]


@dataclass(frozen=True)
class SkillEntry:
    """A skill discovered from a SKILL.md on disk.

    Attributes:
        name: The skill name from frontmatter.
        path: Absolute path to the SKILL.md file.
        scope: The :class:`SkillSource` this entry belongs to.
        spec: The deserialized :class:`SkillSpec`.
    """

    name: str
    path: Path
    scope: SkillSource
    spec: SkillSpec

    @property
    def priority(self) -> int:
        """Convenience accessor for scope priority."""
        return self.scope.priority


class OIagentCoworkerSkillLoader:
    """Discover skills from SKILL.md files on disk.

    :class:`OIagentCoworkerSkillLoader` walks a root directory looking
    for ``SKILL.md`` files (case-sensitive) and deserializes their YAML
    frontmatter into :class:`SkillEntry` objects.

    After discovery, :meth:`resolve` applies scope priority so that
    duplicate skill names across scopes are collapsed to the highest-
    priority winner.

    Anti-flattery boundary (see plan §3.2):
        - No ``import openworker`` anywhere in this class.
        - No SKILL.md markdown-body parsing; only frontmatter.
        - No stage_confirm gate.
    """

    def __init__(self) -> None:
        self._discoveries: list[SkillEntry] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, root: Path, scope: SkillSource) -> list[SkillEntry]:
        """Walk *root* and collect all valid SKILL.md entries at *scope*.

        Args:
            root: Directory to walk. The loader looks one level deep —
                any subdirectory containing a ``SKILL.md`` file is
                treated as a skill folder.
            scope: The :class:`SkillSource` to tag discovered entries.

        Returns:
            A list of successfully parsed :class:`SkillEntry` objects.
            Entries that fail frontmatter validation are silently
            skipped with a WARNING-level log.

        Raises:
            ValueError: If *root* does not exist or is not a directory.
        """
        root = Path(root)
        if not root.exists() or not root.is_dir():
            raise ValueError(f"root must be an existing directory: {root}")

        entries: list[SkillEntry] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                entry = self._parse_skill_md(skill_md, scope)
                if entry is not None:
                    entries.append(entry)
            except Exception:
                _LOGGER.warning(
                    "Skipping malformed skill at %s", skill_md, exc_info=True
                )
        self._discoveries.extend(entries)
        return entries

    def resolve(self, entries: list[SkillEntry] | None = None) -> list[SkillEntry]:
        """Collapse duplicate names to the highest-priority scope.

        Args:
            entries: Entries to resolve. Defaults to the most recent
                :meth:`discover` result if no argument is given.

        Returns:
            A new list with one entry per unique skill name — the entry
            whose :attr:`~SkillEntry.scope` has the highest
            :attr:`~SkillSource.priority`.
        """
        source = entries if entries is not None else self._discoveries
        best: dict[str, SkillEntry] = {}
        for entry in source:
            existing = best.get(entry.name)
            if existing is None or entry.priority > existing.priority:
                best[entry.name] = entry
        return list(best.values())

    def clear(self) -> None:
        """Reset internal discovery cache."""
        self._discoveries.clear()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_front_matter(content: str) -> dict[str, Any]:
        """Extract and parse YAML frontmatter from markdown content.

        Returns:
            A dictionary of the parsed frontmatter. Empty dict if no
            frontmatter block is found.

        Raises:
            yaml.YAMLError: If the frontmatter is not valid YAML.
        """
        match = _FRONT_MATTER_PATTERN.match(content)
        if not match:
            return {}
        yaml_text = match.group(1)
        return yaml.safe_load(yaml_text) or {}

    @staticmethod
    def _parse_skill_md(path: Path, scope: SkillSource) -> SkillEntry | None:
        """Parse a single SKILL.md into a :class:`SkillEntry`.

        Returns:
            A :class:`SkillEntry` on success, or ``None`` if the file
            is malformed or missing required fields.

        Raises:
            yaml.YAMLError: If the frontmatter is not valid YAML
                (caller is expected to catch this).
        """
        content = path.read_text(encoding="utf-8")
        data = OIagentCoworkerSkillLoader._parse_front_matter(content)
        if not data:
            _LOGGER.warning("SKILL.md at %s has no frontmatter", path)
            return None

        missing = _SKILL_REQUIRED_KEYS - set(data.keys())
        if missing:
            _LOGGER.warning(
                "SKILL.md at %s missing required keys: %s",
                path,
                sorted(missing),
            )
            return None

        # Coerce types to match SkillSpec field constraints.
        name = str(data["name"]).strip()
        version = str(data["version"]).strip()
        description = str(data["description"]).strip()
        entrypoint = str(data["entrypoint"]).strip()

        if not all([name, version, description, entrypoint]):
            _LOGGER.warning(
                "SKILL.md at %s has empty required field", path
            )
            return None

        config: dict[str, Any] = data.get("config", {})
        if not isinstance(config, dict):
            config = {}

        metadata: dict[str, Any] = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        spec = SkillSpec(
            name=name,
            version=version,
            description=description,
            entrypoint=entrypoint,
            config=config,
            metadata=metadata,
        )
        return SkillEntry(name=name, path=path, scope=scope, spec=spec)
