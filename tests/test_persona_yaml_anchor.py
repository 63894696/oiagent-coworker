# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W2 plan §7.4 boundary ④ is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Boundary ④ (W2 plan §7.4):
#     persona frontmatter YAML anchor/alias contract. Adjudicated
#     semantics (D3 = option A, user-ratified): ``yaml.safe_load``
#     natively supports legal anchors (``&a`` / ``*a`` / ``<<:``) and the
#     loader does not explicitly forbid them, so the contract is "support
#     and pin the current behaviour": legal anchors load transparently;
#     an undefined alias is a corrupt file and is isolated (skipped) by
#     ``load_all``; an anchor that expands to an empty required field is
#     rejected by validation. No implementation change.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Boundary ④ (W2 plan §7.4) -- persona frontmatter YAML anchor contract.

Adjudicated semantics (contract D3, option A -- support / pin current
behaviour):

  1. Legal anchors/aliases (``&nm`` / ``*nm``) and merge keys
     (``<<: *d``) expand transparently under ``yaml.safe_load``; a
     persona whose required fields are carried by alias expansion loads
     successfully with the expanded values.
  2. An undefined alias (``*undefined`` with no anchor) is a corrupt
     file: ``load_all`` must skip it (no raise, no crash) while still
     loading a sibling well-formed persona from the same directory.
  3. An anchor expanding to an empty required field is rejected by
     ``_validate_persona_data`` with ``ValueError`` on ``load_persona``.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - Deterministic assertions only; no thresholds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.persona.persistence import OIagentCoworkerPersonaPersistence

# ---------------------------------------------------------------------------
# Frontmatter fixtures
# ---------------------------------------------------------------------------

# Legal anchor: name/description/version all arrive via alias expansion.
_ANCHORED_PERSONA_MD = """\
---
name: &nm hera
description: *nm
version: &ver 1.2.3
author: boundary-test
---
Body is ignored by the loader.
"""

# Legal merge key: required fields land through ``<<: *defaults``.
_MERGE_KEY_PERSONA_MD = """\
---
defaults: &defaults
  description: merged description
  version: 2.0.0
name: zeus
<<: *defaults
---
"""

# Undefined alias: no ``&undefined`` anchor exists anywhere.
_UNDEFINED_ALIAS_MD = """\
---
name: broken
description: *undefined
version: 0.0.1
---
"""

# Anchor expands to an empty string in a required field.
_EMPTY_ANCHOR_MD = """\
---
name: &empty ""
description: valid description
version: 0.0.1
---
"""

# Plain well-formed persona (sibling for the isolation test).
_GOOD_PERSONA_MD = """\
---
name: athena
description: goddess of wisdom
version: 3.1.4
---
"""


def _write(tmp_path: Path, filename: str, content: str) -> Path:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


# ===========================================================================
# Boundary ④ tests
# ===========================================================================


def test_01_legal_anchor_loads_with_expanded_values(tmp_path: Path) -> None:
    """Legal anchor/alias: required fields carried by ``&``/``*`` expand
    to the anchor's value; ``load_persona`` succeeds and ``load_all``
    contains the persona."""
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    _write(personas_dir, "hera.md", _ANCHORED_PERSONA_MD)

    persistence = OIagentCoworkerPersonaPersistence(personas_dir)
    persona = persistence.load_persona(personas_dir / "hera.md")

    assert persona.name == "hera"
    assert persona.description == "hera"  # alias expanded to anchor value
    assert persona.version == "1.2.3"

    all_personas = persistence.load_all()
    assert "hera" in all_personas
    assert all_personas["hera"].description == "hera"


def test_02_merge_key_loads_with_expanded_values(tmp_path: Path) -> None:
    """Legal merge key (``<<: *defaults``): required fields arrive via
    the merged mapping and load transparently."""
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    _write(personas_dir, "zeus.md", _MERGE_KEY_PERSONA_MD)

    persistence = OIagentCoworkerPersonaPersistence(personas_dir)
    persona = persistence.load_persona(personas_dir / "zeus.md")

    assert persona.name == "zeus"
    assert persona.description == "merged description"
    assert persona.version == "2.0.0"


def test_03_undefined_alias_isolated_by_load_all(tmp_path: Path) -> None:
    """Undefined alias: ``load_all`` skips the corrupt file (no raise)
    and still loads the well-formed sibling from the same directory."""
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    _write(personas_dir, "broken.md", _UNDEFINED_ALIAS_MD)
    _write(personas_dir, "athena.md", _GOOD_PERSONA_MD)

    persistence = OIagentCoworkerPersonaPersistence(personas_dir)
    all_personas = persistence.load_all()  # must NOT raise

    assert set(all_personas.keys()) == {"athena"}
    assert all_personas["athena"].version == "3.1.4"


def test_04_empty_required_field_via_anchor_rejected(tmp_path: Path) -> None:
    """An anchor expanding to an empty required field is rejected:
    ``load_persona`` raises ``ValueError``."""
    personas_dir = tmp_path / "personas"
    personas_dir.mkdir()
    bad = _write(personas_dir, "empty.md", _EMPTY_ANCHOR_MD)

    persistence = OIagentCoworkerPersonaPersistence(personas_dir)
    with pytest.raises(ValueError):
        persistence.load_persona(bad)
