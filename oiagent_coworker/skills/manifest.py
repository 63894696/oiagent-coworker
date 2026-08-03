# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W2-5.1/5.2/5.3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Implements the W2-5.3 manifest
#     surface: SKILL.md markdown-body digest (the parsing loader.py
#     explicitly defers) plus the e2e_overlay declaration check used to
#     mount capability-04 as a SKILL.md.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""OIagent Coworker -- W2-5.3 SKILL.md manifest surface.

The W2-5.1 loader (:mod:`oiagent_coworker.skills.loader`) deliberately
parses only the YAML frontmatter of a SKILL.md and ignores the markdown
body. This module fills that gap with a minimal, statically testable
body digest:

  * :class:`SkillBody` -- title (first ATX ``# `` heading), the ordered
    ``## ``/``### `` section headings, and a 200-character prose digest.
  * :class:`SkillManifest` -- pairs the loader's :class:`SkillEntry`
    with the parsed :class:`SkillBody`.
  * :class:`OIagentCoworkerSkillManifest` -- loads a single named skill
    from a caller-injected ``skills_root`` and checks the
    ``e2e_overlay`` frontmatter declaration used to mount capability-04
    as a SKILL.md.

Fail-closed semantics
---------------------

Unlike the loader's discover-and-skip convention, :meth:`load` raises
on any malformed input: ``FileNotFoundError`` for a missing SKILL.md
and ``ValueError`` for invalid frontmatter or a name/frontmatter
mismatch. A manifest is consumed by gate-adjacent code paths, so a
silently skipped skill is not acceptable here.

Audit boundary
--------------

This module emits ZERO audit records of its own and takes NO
``audit_sink`` parameter. Parsing and declaration checks are not gate
decisions (same convention as stage_confirm).

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this module.
    - No aisuite, no vendor SDK, no non-MIT dependency.
    - No OIagent daemon imports; ``skills_root`` is injected by the
      caller. This module NEVER resolves ${OIAGENT_VAULT} and never
      imports ``oiagent.vault.path``.
    - Emits ZERO audit records (same convention as stage_confirm:
      parsing and declaration checks are not gate decisions).
    - Borrowed concept (SKILL.md as manifest surface), not runtime.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from oiagent_coworker.skills.loader import (
    SkillEntry,
    SkillSource,
    # Intentional same-package private reuse: keeps the SKILL.md
    # frontmatter regex single-sourced in loader.py (adjudicated
    # Option A) without touching the sealed W2-5.1 loader.
    _FRONT_MATTER_PATTERN,
)
from oiagent_coworker.skills.models import SkillSpec

__all__ = [
    "SkillBody",
    "SkillManifest",
    "OverlayDeclarationError",
    "OIagentCoworkerSkillManifest",
    "E2E_OVERLAY_KEY",
]

#: Frontmatter ``metadata`` key declaring that a skill is an E2E
#: encryption overlay (used to mount capability-04 as a SKILL.md).
E2E_OVERLAY_KEY: str = "e2e_overlay"

_MAX_DIGEST_CHARS = 200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_ITALIC_RE = re.compile(r"(\*\*|__|\*|_)")
_CODE_SPAN_RE = re.compile(r"`([^`]*)`")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class SkillBody:
    """Digest of the markdown body of a SKILL.md.

    Attributes:
        title: Text of the first ATX level-1 heading (``# ``), or
            ``None`` if the body has none.
        sections: Texts of all level-2 (``## ``) and level-3 (``### ``)
            headings, in document order.
        digest: First non-heading prose paragraph with markdown markers
            stripped and frontmatter removed, truncated to 200 chars.
            Empty string when the body has no prose.
    """

    title: str | None
    sections: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class SkillManifest:
    """A loaded skill: the loader's entry plus the parsed body digest."""

    entry: SkillEntry
    body: SkillBody


class OverlayDeclarationError(ValueError):
    """Raised when a skill does not declare the required overlay metadata."""

    def __init__(self, skill_name: str, path: Path, key: str) -> None:
        self.skill_name = skill_name
        self.path = Path(path)
        self.key = key
        super().__init__(
            f"skill {skill_name!r} at {self.path} does not declare "
            f"metadata.{key}: true"
        )


def _strip_markdown_inline(text: str) -> str:
    """Strip inline markdown markers; keep it minimal, no library."""
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _CODE_SPAN_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    return _BOLD_ITALIC_RE.sub("", text)


class OIagentCoworkerSkillManifest:
    """Load a single named skill's manifest from a caller-injected root.

    ``skills_root`` is always supplied by the caller; this class never
    resolves environment variables or vault paths itself.

    Anti-flattery boundary (see plan §3.2):
        - No ``import openworker`` anywhere in this class.
        - No OIAGENT_VAULT resolution; ``skills_root`` is injected.
        - Emits ZERO audit records; parsing is not a gate decision.
    """

    def __init__(self, skills_root: Path) -> None:
        if not isinstance(skills_root, (str, os.PathLike)):
            raise TypeError(
                f"skills_root must be a str or os.PathLike, "
                f"got {type(skills_root).__name__}"
            )
        root = Path(skills_root)
        if not root.exists() or not root.is_dir():
            raise ValueError(f"root must be an existing directory: {root}")
        self._skills_root = root

    @property
    def skills_root(self) -> Path:
        """The caller-injected root directory containing skill folders."""
        return self._skills_root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(
        self, skill_name: str, *, scope: SkillSource = SkillSource.GLOBAL
    ) -> SkillManifest:
        """Load the manifest for *skill_name* under ``skills_root``.

        Reads ``skills_root/<skill_name>/SKILL.md``, parses frontmatter
        via the loader's conventions, and parses the markdown body via
        :meth:`parse_body`.

        Raises:
            FileNotFoundError: If the SKILL.md does not exist.
            ValueError: If the frontmatter is invalid (fail-closed,
                unlike loader.discover's skip-and-warn) or if the
                frontmatter ``name`` does not equal *skill_name*.
        """
        path = self._skills_root / skill_name / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(f"SKILL.md not found: {path}")
        content = path.read_text(encoding="utf-8")
        data = self._parse_frontmatter_fail_closed(content, path)

        name = str(data["name"]).strip()
        if name != skill_name:
            raise ValueError(
                f"frontmatter name {name!r} does not match requested "
                f"skill {skill_name!r} at {path}"
            )

        config = data.get("config", {})
        if not isinstance(config, dict):
            config = {}
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        spec = SkillSpec(
            name=name,
            version=str(data["version"]).strip(),
            description=str(data["description"]).strip(),
            entrypoint=str(data["entrypoint"]).strip(),
            config=config,
            metadata=metadata,
        )
        entry = SkillEntry(name=name, path=path, scope=scope, spec=spec)
        return SkillManifest(entry=entry, body=self.parse_body(path))

    def parse_body(self, path: Path) -> SkillBody:
        """Parse the markdown body of a SKILL.md into a :class:`SkillBody`.

        Statically testable: no frontmatter validation is performed;
        the body is simply the part after the closing ``---``.
        """
        content = Path(path).read_text(encoding="utf-8")
        match = _FRONT_MATTER_PATTERN.match(content)
        body_text = match.group(2) if match else content

        title: str | None = None
        sections: list[str] = []
        in_fence = False
        for raw_line in body_text.splitlines():
            line = raw_line.rstrip()
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            heading = _HEADING_RE.match(line)
            if heading is None:
                continue
            level = len(heading.group(1))
            text = heading.group(2)
            if level == 1 and title is None:
                title = text
            elif level in (2, 3):
                sections.append(text)

        return SkillBody(
            title=title,
            sections=tuple(sections),
            digest=self._extract_digest(body_text),
        )

    def require_overlay(
        self, entry: SkillEntry, *, key: str = E2E_OVERLAY_KEY
    ) -> bool:
        """Return True iff ``entry.spec.metadata[key]`` is True."""
        return entry.spec.metadata.get(key) is True

    def assert_overlay(
        self, entry: SkillEntry, *, key: str = E2E_OVERLAY_KEY
    ) -> None:
        """Raise :class:`OverlayDeclarationError` if the overlay is absent."""
        if not self.require_overlay(entry, key=key):
            raise OverlayDeclarationError(entry.name, entry.path, key)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_frontmatter_fail_closed(
        content: str, path: Path
    ) -> dict[str, Any]:
        """Parse frontmatter, raising ValueError on any malformation.

        Fail-closed counterpart to the loader's skip-and-warn discovery:
        a manifest load must never silently accept a malformed SKILL.md.
        """
        match = _FRONT_MATTER_PATTERN.match(content)
        if not match:
            raise ValueError(f"SKILL.md at {path} has no frontmatter")
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise ValueError(
                f"SKILL.md at {path} has invalid YAML frontmatter: {exc}"
            ) from exc
        if not isinstance(data, dict) or not data:
            raise ValueError(f"SKILL.md at {path} has empty frontmatter")

        required = ("name", "version", "description", "entrypoint")
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(
                f"SKILL.md at {path} missing required keys: {missing}"
            )
        if not all(str(data[k]).strip() for k in required):
            raise ValueError(
                f"SKILL.md at {path} has an empty required field"
            )
        return data

    @staticmethod
    def _extract_digest(body_text: str) -> str:
        """First non-heading prose paragraph, stripped, max 200 chars."""
        in_fence = False
        paragraph: list[str] = []
        for raw_line in body_text.splitlines():
            line = raw_line.strip()
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not line:
                if paragraph:
                    break
                continue
            if line.startswith("#"):
                continue
            paragraph.append(line)
        digest = _strip_markdown_inline(" ".join(paragraph))
        digest = re.sub(r"\s+", " ", digest).strip()
        return digest[:_MAX_DIGEST_CHARS]
