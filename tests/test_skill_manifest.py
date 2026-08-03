# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_skill_manifest.py (new file)
#   Upstream commit:  not present (W2-5.1/5.2/5.3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W2-5.3; tests the manifest surface
#     (SKILL.md markdown-body digest + e2e_overlay declaration check)
#     and mounts capability-04 as a SKILL.md fixture.
#   - 12 tests, no external deps beyond pytest. tmp_path fixtures are
#     used for synthetic skill roots; the capability-04 fixture under
#     tests/fixtures/skills/ is the acceptance target.
#   - Mirrors the fixture patterns of test_stage_confirm.py:
#     _REPO_ROOT sys.path idiom, fail-closed error-path coverage.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Tests for oiagent_coworker.skills.manifest -- W2-5.3 acceptance.

Covers the manifest contract: capability-04 fixture acceptance,
frontmatter/metadata parsing, markdown-body digest (title / sections /
200-char digest), loader consistency, overlay declaration checks
(require/assert), fail-closed load errors, and constructor validation.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No vault-path resolution; all roots are fixtures or tmp_path.
    - No audit assertions; manifest emits zero audit records.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.skills.loader import (
    OIagentCoworkerSkillLoader,
    SkillSource,
)
from oiagent_coworker.skills.manifest import (
    E2E_OVERLAY_KEY,
    OIagentCoworkerSkillManifest,
    OverlayDeclarationError,
    SkillBody,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "skills"
CAPABILITY_04_DIR = FIXTURE_ROOT / "capability-04-e2e"


def _write_skill(root: Path, folder: str, frontmatter: str, body: str = "") -> Path:
    skill_dir = root / folder
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(frontmatter + body, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# 1. Acceptance: capability-04 fixture present and declares the overlay
# ----------------------------------------------------------------------


def test_capability_04_skill_present() -> None:
    skill_md = CAPABILITY_04_DIR / "SKILL.md"
    assert skill_md.is_file(), f"fixture missing: {skill_md}"
    assert "e2e_overlay: true" in skill_md.read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# 2. Overlay metadata is parsed under metadata: and require_overlay works
# ----------------------------------------------------------------------


def test_capability_04_overlay_metadata_parsed() -> None:
    manifest = OIagentCoworkerSkillManifest(FIXTURE_ROOT).load("capability-04-e2e")
    assert manifest.entry.spec.metadata[E2E_OVERLAY_KEY] is True
    loader = OIagentCoworkerSkillManifest(FIXTURE_ROOT)
    assert loader.require_overlay(manifest.entry) is True
    assert loader.require_overlay(manifest.entry, key=E2E_OVERLAY_KEY) is True


# ----------------------------------------------------------------------
# 3. Body digest: title, sections, digest shape
# ----------------------------------------------------------------------


def test_load_body_title_and_sections() -> None:
    manifest = OIagentCoworkerSkillManifest(FIXTURE_ROOT).load("capability-04-e2e")
    body = manifest.body
    assert body.title == "Capability-04 E2E Encryption Overlay"
    assert "Threat model — solved" in body.sections
    assert "Interface" in body.sections
    assert body.digest, "digest must be non-empty"
    assert len(body.digest) <= 200
    assert "---" not in body.digest


# ----------------------------------------------------------------------
# 4. Manifest frontmatter parse is consistent with loader.discover
# ----------------------------------------------------------------------


def test_loader_manifest_frontmatter_consistency() -> None:
    manifest = OIagentCoworkerSkillManifest(FIXTURE_ROOT).load("capability-04-e2e")
    discovered = OIagentCoworkerSkillLoader().discover(
        FIXTURE_ROOT, SkillSource.GLOBAL
    )
    by_name = {entry.name: entry for entry in discovered}
    assert "capability-04-e2e" in by_name
    manifest_spec = manifest.entry.spec
    discover_spec = by_name["capability-04-e2e"].spec
    assert manifest_spec.name == discover_spec.name
    assert manifest_spec.version == discover_spec.version
    assert manifest_spec.description == discover_spec.description
    assert manifest_spec.entrypoint == discover_spec.entrypoint
    assert manifest_spec.config == discover_spec.config
    assert manifest_spec.metadata == discover_spec.metadata


# ----------------------------------------------------------------------
# 5. assert_overlay raises on a plain (non-overlay) skill
# ----------------------------------------------------------------------


def test_assert_overlay_raises_on_plain_skill() -> None:
    loader = OIagentCoworkerSkillManifest(FIXTURE_ROOT / "global")
    manifest = loader.load("skill-a")
    with pytest.raises(OverlayDeclarationError) as excinfo:
        loader.assert_overlay(manifest.entry)
    message = str(excinfo.value)
    assert "skill-a" in message
    assert E2E_OVERLAY_KEY in message


# ----------------------------------------------------------------------
# 6. assert_overlay raises when the key is present but false
# ----------------------------------------------------------------------


def test_assert_overlay_false_value_raises(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "fake-overlay",
        (
            "---\n"
            "name: fake-overlay\n"
            "version: 0.1.0\n"
            "description: declares the key but disables it\n"
            "entrypoint: skills.fake_overlay\n"
            "metadata:\n"
            "  e2e_overlay: false\n"
            "---\n"
        ),
        "# Fake\n",
    )
    loader = OIagentCoworkerSkillManifest(tmp_path)
    manifest = loader.load("fake-overlay")
    assert loader.require_overlay(manifest.entry) is False
    with pytest.raises(OverlayDeclarationError):
        loader.assert_overlay(manifest.entry)


# ----------------------------------------------------------------------
# 7. Top-level e2e_overlay key is silently dropped (metadata-nesting trap)
# ----------------------------------------------------------------------


def test_overlay_top_level_key_is_dropped(tmp_path: Path) -> None:
    """Pins the negative arm of the metadata-nesting trap (code-reviewer W2-5.3 F1)."""
    _write_skill(
        tmp_path,
        "top-level-overlay",
        (
            "---\n"
            "name: top-level-overlay\n"
            "version: 0.1.0\n"
            "description: declares the key at top level, not under metadata\n"
            "entrypoint: skills.top_level_overlay\n"
            "e2e_overlay: true\n"
            "---\n"
        ),
        "# Top Level\n",
    )
    loader = OIagentCoworkerSkillManifest(tmp_path)
    manifest = loader.load("top-level-overlay")
    assert E2E_OVERLAY_KEY not in manifest.entry.spec.metadata
    assert loader.require_overlay(manifest.entry, key=E2E_OVERLAY_KEY) is False
    with pytest.raises(OverlayDeclarationError):
        loader.assert_overlay(manifest.entry, key=E2E_OVERLAY_KEY)


# ----------------------------------------------------------------------
# 8. Loading a missing skill raises FileNotFoundError
# ----------------------------------------------------------------------


def test_load_missing_file_raises() -> None:
    loader = OIagentCoworkerSkillManifest(FIXTURE_ROOT)
    with pytest.raises(FileNotFoundError):
        loader.load("no-such-skill")


# ----------------------------------------------------------------------
# 9. Frontmatter name must match the requested skill name
# ----------------------------------------------------------------------


def test_load_name_mismatch_raises(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "dir-x",
        (
            "---\n"
            "name: other\n"
            "version: 0.1.0\n"
            "description: folder name and frontmatter name disagree\n"
            "entrypoint: skills.other\n"
            "---\n"
        ),
        "# Other\n",
    )
    loader = OIagentCoworkerSkillManifest(tmp_path)
    with pytest.raises(ValueError, match="other"):
        loader.load("dir-x")


# ----------------------------------------------------------------------
# 10. Invalid frontmatter is fail-closed (loader would return None/skip)
# ----------------------------------------------------------------------


def test_load_bad_frontmatter_raises(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "broken",
        (
            "---\n"
            "name: broken\n"
            "version: 0.1.0\n"
            "---\n"
        ),
        "# Broken\n",
    )
    loader = OIagentCoworkerSkillManifest(tmp_path)
    with pytest.raises(ValueError):
        loader.load("broken")


# ----------------------------------------------------------------------
# 11. Constructor rejects non-path types
# ----------------------------------------------------------------------


def test_constructor_type_error() -> None:
    with pytest.raises(TypeError):
        OIagentCoworkerSkillManifest(123)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# 12. Constructor rejects a nonexistent root
# ----------------------------------------------------------------------


def test_constructor_missing_root_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        OIagentCoworkerSkillManifest(tmp_path / "does-not-exist")


# ----------------------------------------------------------------------
# 13. Empty body after frontmatter yields an empty SkillBody
# ----------------------------------------------------------------------


def test_parse_body_empty_body(tmp_path: Path) -> None:
    path = _write_skill(
        tmp_path,
        "hollow",
        (
            "---\n"
            "name: hollow\n"
            "version: 0.1.0\n"
            "description: no body at all\n"
            "entrypoint: skills.hollow\n"
            "---\n"
        ),
    )
    loader = OIagentCoworkerSkillManifest(tmp_path)
    body = loader.parse_body(path)
    assert body == SkillBody(title=None, sections=(), digest="")
