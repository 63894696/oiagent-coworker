# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/personas/loader.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     broad loader with a focused persistence layer for persona definitions.
#   - The upstream personadiscovery and registry integration is moved to
#     the service layer; this class only loads raw persona definitions from
#     markdown files.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Persistence layer for OIagent Coworker persona definitions (W2-4).

This module implements :class:`OIagentCoworkerPersonaPersistence`, which
loads :class:`Persona` objects from markdown files in a directory.

Each persona file must contain a YAML frontmatter block (delimited by
``---`` lines) with at least the fields ``name``, ``description``, and
``version``. Optional fields include ``author``, ``tags``, and ``module``.

Anti-flattery boundary (see plan \xc2\xa73.2):
    - No ``import openworker`` anywhere in this module.
    - No OpenAI / Anthropic provider classes.
    - Borrowed design (markdown frontmatter parsing), not runtime.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from oiagent_coworker.persona.models import Persona

__all__ = ["OIagentCoworkerPersonaPersistence"]

_LOGGER = logging.getLogger(__name__)

# Regex to match the YAML frontmatter block at the start of a file.
_FRONT_MATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL | re.MULTILINE
)


def _parse_front_matter(content: str) -> dict:
    """Extract and parse YAML frontmatter from markdown content.

    Args:
        content: The full content of a markdown file.

    Returns:
        A dictionary of the parsed frontmatter. If no frontmatter is found,
        returns an empty dictionary.

    Raises:
        yaml.YAMLError: If the frontmatter is not valid YAML.
    """
    match = _FRONT_MATTER_PATTERN.match(content)
    if not match:
        return {}
    yaml_text = match.group(1)
    return yaml.safe_load(yaml_text) or {}


def _validate_persona_data(data: dict, file_path: Path) -> None:
    """Validate that the persona data contains required fields.

    Args:
        data: The parsed frontmatter dictionary.
        file_path: The path to the file being validated (for error messages).

    Raises:
        ValueError: If a required field is missing or empty.
    """
    required = {"name", "description", "version"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(
            f"Persona file {file_path} is missing required fields: {sorted(missing)}"
        )
    for field in required:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Persona file {file_path} has invalid or empty required field: {field}"
            )


class OIagentCoworkerPersonaPersistence:
    """Load persona definitions from markdown files in a directory.

    Thread safety:
        The class is stateless and thread-safe for concurrent reads.
        The caller is responsible for synchronizing writes to the directory.

    Disk layout:
        ``personas_dir`` is a directory containing ``*.md`` files. Each file
        must contain a YAML frontmatter block with the persona definition.
    """

    def __init__(self, personas_dir: Path) -> None:
        self.personas_dir: Path = Path(personas_dir)
        if not self.personas_dir.is_dir():
            raise NotADirectoryError(
                f"Persona directory does not exist: {self.personas_dir}"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_persona_files(self) -> list[Path]:
        """Return a list of all markdown files in the persona directory.

        Returns:
            A list of paths to ``*.md`` files, sorted alphabetically.
        """
        return sorted(self.personas_dir.glob("*.md"))

    def load_persona(self, file_path: Path) -> Persona:
        """Load a single persona from a markdown file.

        Args:
            file_path: Path to the markdown file.

        Returns:
            A :class:`Persona` object populated from the file's frontmatter.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValueError: If the file is missing required fields or has invalid
                frontmatter.
            yaml.YAMLError: If the frontmatter is not valid YAML.
        """
        if not file_path.is_file():
            raise FileNotFoundError(f"Persona file not found: {file_path}")
        content = file_path.read_text(encoding="utf-8")
        data = _parse_front_matter(content)
        _validate_persona_data(data, file_path)
        return Persona(
            name=str(data["name"]),
            description=str(data["description"]),
            version=str(data["version"]),
            author=data.get("author"),
            tags=list(data.get("tag", []) or []),  # support both 'tags' and 'tag'
            module=data.get("module"),
        )

    def load_all(self) -> dict[str, Persona]:
        """Load all persona definitions from the directory.

        Returns:
            A dictionary mapping persona names to :class:`Persona` objects.

        Notes:
            Files that fail to parse are logged and skipped. If multiple
            files define the same persona name, the last one wins.
        """
        personas: dict[str, Persona] = {}
        for file_path in self.list_persona_files():
            try:
                persona = self.load_persona(file_path)
                personas[persona.name] = persona
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to load persona from %s: %s", file_path, exc
                )
        return personas

    def reload(self) -> dict[str, Persona]:
        """Reload all persona definitions from the directory.

        This is equivalent to calling :meth:`load_all` again.

        Returns:
            A dictionary mapping persona names to :class:`Persona` objects.
        """
        return self.load_all()