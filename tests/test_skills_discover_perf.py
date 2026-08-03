# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W2 plan §7.4 boundary ③ is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Boundary ③ (W2 plan §7.4):
#     skills discover performance at 1000+ SKILL.md scale. A fixture
#     factory materializes real skill directories under tmp_path (cheap:
#     ~1000 small files, plain write_text, no fsync), then asserts
#     near-linear wall-clock scaling of
#     ``OIagentCoworkerSkillLoader.discover``: two sizes (500 / 1000) x
#     3 runs each, medians compared with ratio < 4.0, plus a generous
#     single-point hang guard (< 30 s). ``resolve`` over the 1000-entry
#     discovery is exercised as a dedup sanity pass. No absolute-
#     millisecond assertions (CI noise absorbed by relative thresholds).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Boundary ③ (W2 plan §7.4) -- skills discover performance at 1000+ scale.

Test intent (per handoff contract, boundary ③):

  1. Correctness first: discovering a root with N generated skill
     folders returns exactly N entries, unique names, none dropped.
  2. Complexity assertion (primary): median discover time at 1000 must
     be < 4x the median at 500 -- discover is a per-directory linear
     scan; a regression (e.g. re-resolving the root per child, or a
     quadratic dedup) would push the ratio up and trip the bound.
  3. Loose single-point hang guard: median discover at 1000 < 30 s.
  4. ``resolve()`` over the 1000-entry discovery completes and returns
     the full unique-name set (dedup does not blow up at scale).

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No absolute-millisecond assertions.
    - tmp_path only; each size tier gets its own isolated root so
      repeated runs cannot interfere.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.skills.loader import (
    OIagentCoworkerSkillLoader,
    SkillSource,
)

_SMALL_N = 500
_LARGE_N = 1_000
_RUNS_PER_SIZE = 3
_RATIO_UPPER_BOUND = 4.0
_SINGLE_POINT_HANG_GUARD_S = 30.0

_SKILL_MD_TEMPLATE = """\
---
name: skill-{i:04d}
version: 0.1.0
description: Boundary 03 generated skill {i}
entrypoint: skills.generated.skill_{i:04d}
---
"""


def _make_skills_root(base: Path, n: int) -> Path:
    """Materialize ``n`` skill folders (``skill-NNNN/SKILL.md``) under
    ``base`` and return the root. Minimal legal frontmatter (the four
    required keys), unique names, empty body."""
    root = base / f"skills_{n}"
    for i in range(n):
        skill_dir = root / f"skill-{i:04d}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            _SKILL_MD_TEMPLATE.format(i=i), encoding="utf-8"
        )
    return root


def _median_discover_seconds(root: Path, runs: int) -> float:
    """Run ``discover`` over ``root`` ``runs`` times with a fresh loader
    each time (``discover`` accumulates into ``self._discoveries``; a
    fresh instance per run keeps each measurement independent) and
    return the median wall-clock seconds."""
    samples: list[float] = []
    for _ in range(runs):
        loader = OIagentCoworkerSkillLoader()
        start = time.perf_counter()
        loader.discover(root, SkillSource.GLOBAL)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


# ===========================================================================
# Boundary ③ tests
# ===========================================================================


def test_01_discover_correctness_at_1000(tmp_path: Path) -> None:
    """Correctness first: 1000 generated skills -> exactly 1000 entries,
    unique names, no drops."""
    root = _make_skills_root(tmp_path, _LARGE_N)

    entries = OIagentCoworkerSkillLoader().discover(root, SkillSource.GLOBAL)

    assert len(entries) == _LARGE_N
    names = {e.name for e in entries}
    assert len(names) == _LARGE_N
    assert names == {f"skill-{i:04d}" for i in range(_LARGE_N)}


def test_02_discover_scales_near_linearly(tmp_path: Path) -> None:
    """Complexity assertion (primary): median discover time at 1000 must
    be < 4x the median at 500."""
    small_root = _make_skills_root(tmp_path / "small", _SMALL_N)
    large_root = _make_skills_root(tmp_path / "large", _LARGE_N)

    # Warm the OS page cache on both trees.
    OIagentCoworkerSkillLoader().discover(small_root, SkillSource.GLOBAL)
    OIagentCoworkerSkillLoader().discover(large_root, SkillSource.GLOBAL)

    median_small = _median_discover_seconds(small_root, _RUNS_PER_SIZE)
    median_large = _median_discover_seconds(large_root, _RUNS_PER_SIZE)

    ratio = median_large / median_small if median_small > 0 else 0.0
    print(
        f"\n[boundary-03] discover median: "
        f"N={_SMALL_N} -> {median_small:.3f}s, "
        f"N={_LARGE_N} -> {median_large:.3f}s, ratio={ratio:.2f}"
    )
    assert ratio < _RATIO_UPPER_BOUND, (
        f"discover scaling regressed: median(t({_LARGE_N}))="
        f"{median_large:.3f}s / median(t({_SMALL_N}))="
        f"{median_small:.3f}s = {ratio:.2f} >= {_RATIO_UPPER_BOUND}"
    )


def test_03_discover_large_size_hang_guard(tmp_path: Path) -> None:
    """Loose single-point upper bound: median discover at 1000 < 30 s.
    Hang guard only, not a regression gate."""
    root = _make_skills_root(tmp_path, _LARGE_N)

    # Warm page cache (not timed).
    OIagentCoworkerSkillLoader().discover(root, SkillSource.GLOBAL)

    median_large = _median_discover_seconds(root, _RUNS_PER_SIZE)
    print(f"\n[boundary-03] N={_LARGE_N} discover median: {median_large:.3f}s")
    assert median_large < _SINGLE_POINT_HANG_GUARD_S, (
        f"discover hung: median(t({_LARGE_N}))={median_large:.3f}s "
        f">= {_SINGLE_POINT_HANG_GUARD_S}s"
    )


def test_04_resolve_dedup_at_1000(tmp_path: Path) -> None:
    """``resolve()`` over the 1000-entry discovery completes and keeps
    the full unique-name set (all names distinct at GLOBAL scope, so
    dedup must return all 1000 entries)."""
    root = _make_skills_root(tmp_path, _LARGE_N)

    loader = OIagentCoworkerSkillLoader()
    entries = loader.discover(root, SkillSource.GLOBAL)
    resolved = loader.resolve(entries)

    assert len(resolved) == _LARGE_N
    assert {e.name for e in resolved} == {
        f"skill-{i:04d}" for i in range(_LARGE_N)
    }
