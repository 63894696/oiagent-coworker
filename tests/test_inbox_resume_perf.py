# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W2 plan §7.4 boundary ① is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Boundary ① (W2 plan §7.4):
#     inbox resume performance. A true 100w-item resume is not CI-feasible
#     (per-line fsync on append), so this suite pre-writes N=10k / N=20k
#     append envelopes by bulk json.dumps (bypassing the service's
#     fsync-per-line path) and asserts near-linear wall-clock scaling of
#     ``OIagentCoworkerInboxService.__init__`` / ``_rebuild_from_disk``:
#     two sizes x 3 runs each, medians compared with ratio < 4.0, plus a
#     generous single-point hang guard (< 30 s). No absolute-millisecond
#     assertions (CI noise absorbed by the relative threshold).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Boundary ① (W2 plan §7.4) -- inbox resume performance.

Test intent (per handoff contract
``2026-08-03-w2-74-boundary-tests-contract.md``, boundary ①):

  1. Correctness first: after constructing a service over a pre-written
     N-append JSONL log, ``service.count()`` equals the number of live
     items (no envelope dropped or double-applied by replay).
  2. Complexity assertion (primary): measure wall-clock of service
     construction (= ``_rebuild_from_disk`` single-pass replay) at
     N1=10_000 and N2=20_000, 3 runs each, take medians, assert
     ``median(t(N2)) / median(t(N1)) < 4.0`` -- an O(N^2) regression
     would push the ratio toward 4-8 and trip this bound, while CI
     noise on a legal O(N) implementation stays well below it.
  3. Loose single-point hang guard: ``median(t(20_000)) < 30 s``.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No absolute-millisecond assertions (deterministic under CI jitter).
    - tmp_path only; no writes outside pytest temp dirs.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.inbox.service import OIagentCoworkerInboxService

# ---------------------------------------------------------------------------
# Scale + threshold constants (contract boundary ①, D1 / D5 decisions)
# ---------------------------------------------------------------------------

_SMALL_N = 10_000
_LARGE_N = 20_000
_RUNS_PER_SIZE = 3
_RATIO_UPPER_BOUND = 4.0
_SINGLE_POINT_HANG_GUARD_S = 30.0

_EPOCH = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prewrite_append_envelopes(path: Path, n: int) -> None:
    """Bulk-write ``n`` well-formed ``append`` envelopes to ``path``.

    Shape matches ``inbox.persistence._serialize_envelope`` output
    (envelope_id monotonic from 1, action="append", full InboxItem dict).
    We bypass ``service.append`` deliberately: its per-line fsync would
    make N=20k writes CI-prohibitive, and the boundary under test is
    *replay* (resume), not the write path. Lines are generated lazily
    and flushed once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        for i in range(1, n + 1):
            created = _EPOCH + timedelta(seconds=i)
            payload = {
                "envelope_id": i,
                "timestamp": created.isoformat(),
                "action": "append",
                "item_id": uuid.uuid4().hex,
                "actor": "system",
                "item": {
                    "item_id": None,  # patched below to match envelope item_id
                    "kind": "notification",
                    "priority": "normal",
                    "title": f"boundary-01 item {i}",
                    "body": f"resume-perf body {i}",
                    "source": "boundary-test",
                    "created_at": created.isoformat(),
                    "expires_at": None,
                    "metadata": {"seq": i},
                },
            }
            # item.item_id must equal the envelope-level item_id.
            payload["item"]["item_id"] = payload["item_id"]
            fp.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            fp.write("\n")
        fp.flush()


def _median_construction_seconds(path: Path, runs: int) -> float:
    """Construct a fresh service over ``path`` ``runs`` times; return
    the median wall-clock seconds of the ``__init__`` call (which is
    where ``_rebuild_from_disk`` runs)."""
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        OIagentCoworkerInboxService(storage_path=path, max_items=100_000)
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


# ===========================================================================
# Boundary ① tests
# ===========================================================================


def test_01_resume_correctness_at_scale(tmp_path: Path) -> None:
    """Correctness first: a service resumed over N pre-written append
    envelopes reports exactly N live items -- replay neither drops nor
    double-applies envelopes."""
    path = tmp_path / "inbox.jsonl"
    _prewrite_append_envelopes(path, _SMALL_N)

    service = OIagentCoworkerInboxService(storage_path=path, max_items=100_000)

    assert service.count() == _SMALL_N


def test_02_resume_scales_near_linearly(tmp_path: Path) -> None:
    """Complexity assertion (primary): median construction time at N2=2*N1
    must be less than 4x the median at N1. O(N^2) replay would push the
    ratio toward 4-8; a legal O(N) single-pass replay stays well under
    the bound despite CI jitter."""
    small_path = tmp_path / "small" / "inbox.jsonl"
    large_path = tmp_path / "large" / "inbox.jsonl"
    _prewrite_append_envelopes(small_path, _SMALL_N)
    _prewrite_append_envelopes(large_path, _LARGE_N)

    # Warm the OS page cache on both files so the first timed run is not
    # an outlier dominated by cold disk reads.
    OIagentCoworkerInboxService(storage_path=small_path, max_items=100_000)
    OIagentCoworkerInboxService(storage_path=large_path, max_items=100_000)

    median_small = _median_construction_seconds(small_path, _RUNS_PER_SIZE)
    median_large = _median_construction_seconds(large_path, _RUNS_PER_SIZE)

    ratio = median_large / median_small if median_small > 0 else 0.0
    print(
        f"\n[boundary-01] resume median: "
        f"N={_SMALL_N} -> {median_small:.3f}s, "
        f"N={_LARGE_N} -> {median_large:.3f}s, ratio={ratio:.2f}"
    )
    assert ratio < _RATIO_UPPER_BOUND, (
        f"resume scaling regressed: median(t({_LARGE_N}))="
        f"{median_large:.3f}s / median(t({_SMALL_N}))="
        f"{median_small:.3f}s = {ratio:.2f} >= {_RATIO_UPPER_BOUND}"
    )


def test_03_resume_large_size_hang_guard(tmp_path: Path) -> None:
    """Loose single-point upper bound: N=20_000 resume median must stay
    under 30 s. This is a hang guard only, not a performance regression
    gate -- CI hardware variance is absorbed by the generous bound."""
    path = tmp_path / "inbox.jsonl"
    _prewrite_append_envelopes(path, _LARGE_N)

    # Warm page cache (not timed).
    OIagentCoworkerInboxService(storage_path=path, max_items=100_000)

    median_large = _median_construction_seconds(path, _RUNS_PER_SIZE)
    print(f"\n[boundary-01] N={_LARGE_N} resume median: {median_large:.3f}s")
    assert median_large < _SINGLE_POINT_HANG_GUARD_S, (
        f"resume hung: median(t({_LARGE_N}))={median_large:.3f}s "
        f">= {_SINGLE_POINT_HANG_GUARD_S}s"
    )
