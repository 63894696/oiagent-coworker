# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_consistency.py (new file)
#   Upstream commit:  not present (W3-1 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W3-1; tests the shadow-mode verdict-diff
#     consistency analyzer (rolling agreement metric, per-field mismatch
#     classification, and the W3 Phase C shadow->enforce flip criterion).
#   - 16 tests, no external deps beyond pytest. All streams are synthetic
#     lists / generators of envelope dicts; the analyzer does no I/O and
#     emits no audit.
#   - Mirrors the fixture patterns of test_skill_manifest.py /
#     test_stage_confirm.py: _REPO_ROOT sys.path idiom, fail-closed
#     error-path coverage.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Tests for oiagent_coworker.permissions.consistency -- W3-1 acceptance.

Covers the analyzer contract: heartbeat-derived denominator, per-field
mismatch classification, new-engine-error as a distinct sole bucket,
rolling-window boundary inclusivity, flip-criterion threshold / sample
semantics, malformed-record handling, and the no-I/O / no-audit boundary.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No vault-path resolution; all inputs are synthetic envelope dicts.
    - No audit assertions; the analyzer emits zero audit records.
"""

from __future__ import annotations

import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.consistency import (
    DEFAULT_FLIP_THRESHOLD,
    DEFAULT_WINDOW,
    FlipCriterion,
    MismatchField,
    analyze_consistency,
    check_flip_criterion,
)

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _verdict(**overrides) -> dict:
    base = {
        "allow": True,
        "mode": "sync",
        "reason": "...",
        "risk_level": "read",
        "requires_approval": False,
    }
    base.update(overrides)
    return base


def heartbeat(ts: str) -> dict:
    """One engine sidecar heartbeat (one compared decision)."""
    return {
        "kind": "permission",
        "timestamp": ts,
        "engine_decision": _verdict(),
        "metadata": {},
        "error": None,
    }


def diff_envelope(ts: str, *, fields=("risk_level",), err=None) -> dict:
    """One SHADOW-mode gate diff envelope (mismatch or new-engine error)."""
    return {
        "kind": "permission",
        "timestamp": ts,
        "engine_decision": _verdict(),
        "metadata": {
            "policy_gate": {
                "mode": "shadow",
                "diff": {
                    "action_kind": "tool_call",
                    "action_target": "bash",
                    "legacy_verdict": _verdict(),
                    "new_verdict": _verdict(),
                    "mismatched_fields": list(fields),
                    "new_engine_error": err,
                },
            }
        },
        "error": None,
    }


def enforce_gate_envelope(ts: str) -> dict:
    """An ENFORCE-mode gate fallback envelope (must be ignored)."""
    return {
        "kind": "permission",
        "timestamp": _ts(NOW) if ts is None else ts,
        "engine_decision": _verdict(),
        "metadata": {
            "policy_gate": {
                "mode": "enforce",
                "fallback": "legacy_on_new_engine_error",
            }
        },
        "error": "policy_gate:new_engine_error: boom",
    }


def _agree_stream(n: int, *, start: datetime) -> list[dict]:
    """n heartbeats, one per minute going back from ``start``.

    Minute spacing keeps even large samples (e.g. 200) comfortably inside
    the default 7-day window so window-filtering never interferes with
    count assertions.
    """
    return [heartbeat(_ts(start - i * timedelta(minutes=1))) for i in range(n)]


# ----------------------------------------------------------------------
# 1. Agreement-only stream -> perfect consistency
# ----------------------------------------------------------------------


def test_agreement_only_stream_perfect_consistency() -> None:
    stream = _agree_stream(50, start=NOW)
    report = analyze_consistency(stream, now=NOW)
    assert report.total_compared == 50
    assert report.mismatches == 0
    assert report.new_engine_errors == 0
    assert report.agreements == 50
    assert report.consistency == 1.0
    assert report.malformed_records == 0
    assert report.mismatch_records == ()


# ----------------------------------------------------------------------
# 2. All-mismatch stream -> zero consistency
# ----------------------------------------------------------------------


def test_all_mismatch_stream_zero_consistency() -> None:
    # 10 heartbeats (denominator) + 10 shadow diff envelopes (all disagree).
    stream = _agree_stream(10, start=NOW) + [
        diff_envelope(_ts(NOW - timedelta(hours=i))) for i in range(10)
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.total_compared == 10
    assert report.mismatches == 10
    assert report.agreements == 0
    assert report.consistency == 0.0


# ----------------------------------------------------------------------
# 3. Mixed stream -> metric and counts
# ----------------------------------------------------------------------


def test_mixed_stream_metric_and_counts() -> None:
    stream = (
        _agree_stream(8, start=NOW)
        + [diff_envelope(_ts(NOW - timedelta(hours=1)), fields=("allow",))]
        + [diff_envelope(_ts(NOW - timedelta(hours=2)), err="boom")]
    )
    report = analyze_consistency(stream, now=NOW)
    assert report.total_compared == 8
    assert report.mismatches == 1
    assert report.new_engine_errors == 1
    assert report.agreements == 6
    assert report.consistency == pytest.approx(6 / 8)
    assert len(report.mismatch_records) == 2


# ----------------------------------------------------------------------
# 4. Each decision field classifies into its own bucket (parametrized)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name,category",
    [
        ("allow", MismatchField.ALLOW),
        ("requires_approval", MismatchField.REQUIRES_APPROVAL),
        ("risk_level", MismatchField.RISK_LEVEL),
        ("mode", MismatchField.MODE),
    ],
)
def test_mismatch_field_classified(field_name: str, category: MismatchField) -> None:
    stream = _agree_stream(1, start=NOW) + [
        diff_envelope(_ts(NOW), fields=(field_name,))
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.mismatches == 1
    assert report.mismatch_by_field.get(category) == 1
    # No other category should be populated.
    assert sum(report.mismatch_by_field.values()) == 1


# ----------------------------------------------------------------------
# 5. new_engine_error is a distinct, sole bucket (never double-counted)
# ----------------------------------------------------------------------


def test_new_engine_error_classified_as_distinct_category() -> None:
    stream = _agree_stream(1, start=NOW) + [
        diff_envelope(_ts(NOW), fields=(), err="sidecar raised"),
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.new_engine_errors == 1
    assert report.mismatches == 0
    assert report.mismatch_by_field.get(MismatchField.NEW_ENGINE_ERROR) == 1
    # NEW_ENGINE_ERROR must be the ONLY bucket for this record.
    assert sum(report.mismatch_by_field.values()) == 1
    record = report.mismatch_records[0]
    assert record.categories == (MismatchField.NEW_ENGINE_ERROR,)
    assert record.new_engine_error == "sidecar raised"


# ----------------------------------------------------------------------
# 6. Legacy-crash blind window is absent from the denominator
# ----------------------------------------------------------------------


def test_legacy_crash_blind_window_absent_from_denominator() -> None:
    # A legacy-crash call produces NEITHER heartbeat NOR diff envelope --
    # it simply never appears in the stream. So a stream representing a
    # window with legacy crashes contains only the observable records.
    # The denominator counts only what is present.
    stream = _agree_stream(5, start=NOW)
    report = analyze_consistency(stream, now=NOW)
    assert report.total_compared == 5
    assert report.consistency == 1.0
    # The blind window is not observable; total_compared reflects only
    # the 5 observable heartbeats, nothing more.
    assert report.agreements == 5


# ----------------------------------------------------------------------
# 7. Window boundary is inclusive on both ends
# ----------------------------------------------------------------------


def test_window_boundary_inclusive() -> None:
    window = timedelta(days=7)
    oldest = NOW - window  # exactly on the lower bound -> included
    newest = NOW  # exactly at now -> included
    just_outside = NOW - window - timedelta(seconds=1)  # excluded
    stream = [
        heartbeat(_ts(oldest)),
        heartbeat(_ts(newest)),
        heartbeat(_ts(just_outside)),
    ]
    report = analyze_consistency(stream, now=NOW, window=window)
    assert report.total_compared == 2
    assert report.agreements == 2


# ----------------------------------------------------------------------
# 8. Threshold boundary: exactly 99.5% (199/200) -> ready
# ----------------------------------------------------------------------


def test_threshold_boundary_exactly_99_5_percent_ready() -> None:
    # 200 heartbeats, 1 mismatch -> 199/200 = 0.995 exactly.
    stream = _agree_stream(200, start=NOW) + [
        diff_envelope(_ts(NOW), fields=("allow",)),
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.total_compared == 200
    assert report.agreements == 199
    criterion = check_flip_criterion(report, min_sample=1)
    assert report.consistency == pytest.approx(0.995)
    assert criterion.ready is True


# ----------------------------------------------------------------------
# 9. Just below threshold (198/200) -> not ready
# ----------------------------------------------------------------------


def test_just_below_threshold_not_ready() -> None:
    # 200 heartbeats, 2 mismatches -> 198/200 = 0.99 < 0.995.
    stream = _agree_stream(200, start=NOW) + [
        diff_envelope(_ts(NOW), fields=("allow",)),
        diff_envelope(_ts(NOW), fields=("mode",)),
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.agreements == 198
    criterion = check_flip_criterion(report, min_sample=1)
    assert report.consistency == pytest.approx(0.99)
    assert criterion.ready is False
    assert "threshold" in criterion.reason


# ----------------------------------------------------------------------
# 10. Empty stream -> consistency None, not ready
# ----------------------------------------------------------------------


def test_empty_stream_not_ready() -> None:
    report = analyze_consistency([], now=NOW)
    assert report.total_compared == 0
    assert report.consistency is None
    criterion = check_flip_criterion(report, min_sample=1)
    assert criterion.ready is False
    assert criterion.consistency is None


# ----------------------------------------------------------------------
# 11. Flip ready vs not-ready end-to-end
# ----------------------------------------------------------------------


def test_flip_ready_vs_not_ready_end_to_end() -> None:
    # Ready: 100% consistency over a large-enough sample.
    ready_report = analyze_consistency(_agree_stream(150, start=NOW), now=NOW)
    ready = check_flip_criterion(ready_report, min_sample=100)
    assert ready.ready is True
    assert "dual-verdict decisions" in ready.reason
    assert "legacy-crash calls are not observable" in ready.reason

    # Not ready: consistency too low.
    low_stream = _agree_stream(150, start=NOW) + [
        diff_envelope(_ts(NOW), fields=("allow",)) for _ in range(10)
    ]
    low_report = analyze_consistency(low_stream, now=NOW)
    not_ready = check_flip_criterion(low_report, min_sample=100)
    assert not_ready.ready is False
    assert "threshold" in not_ready.reason


# ----------------------------------------------------------------------
# 12. Non-permission kinds and enforce gate records are ignored
# ----------------------------------------------------------------------


def test_non_permission_kinds_and_enforce_gate_records_ignored() -> None:
    stream = _agree_stream(3, start=NOW) + [
        # Other audit kinds.
        {"kind": "path_sandbox", "timestamp": _ts(NOW), "metadata": {}},
        {"kind": "shell_classifier", "timestamp": _ts(NOW), "metadata": {}},
        {"kind": "inbox", "timestamp": _ts(NOW), "metadata": {}},
        # Enforce-mode gate fallback envelope (mode filter excludes it).
        enforce_gate_envelope(_ts(NOW)),
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.total_compared == 3
    assert report.mismatches == 0
    assert report.new_engine_errors == 0
    assert report.malformed_records == 0
    assert report.consistency == 1.0


# ----------------------------------------------------------------------
# 13. Malformed records are excluded from window math but counted
# ----------------------------------------------------------------------


def test_malformed_records_excluded_and_counted() -> None:
    stream = _agree_stream(2, start=NOW) + [
        # Unparseable timestamp.
        {"kind": "permission", "timestamp": "not-a-timestamp", "metadata": {}},
        # Missing timestamp.
        {"kind": "permission", "metadata": {}},
        # Diff with empty fields AND null error -> malformed diff.
        diff_envelope(_ts(NOW), fields=(), err=None),
        # Not a mapping at all.
        "garbage",
        # Unknown mismatch field string -> fail-closed to malformed.
        diff_envelope(_ts(NOW), fields=("nonexistent_field",)),
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.total_compared == 2
    assert report.mismatches == 0
    assert report.new_engine_errors == 0
    # 5 malformed: bad ts, missing ts, empty-fields diff, non-mapping,
    # unknown-field diff.
    assert report.malformed_records == 5
    assert report.consistency == 1.0


# ----------------------------------------------------------------------
# 14. min_sample floor blocks ready even at perfect consistency
# ----------------------------------------------------------------------


def test_min_sample_floor_blocks_ready() -> None:
    # Perfect consistency but only 3 decisions: below any sane floor.
    report = analyze_consistency(_agree_stream(3, start=NOW), now=NOW)
    assert report.consistency == 1.0
    # Default floor is 100 (ADJ-3) -> a 3-decision window must NOT be ready.
    default_criterion = check_flip_criterion(report)
    assert default_criterion.ready is False
    assert "min_sample" in default_criterion.reason or "sample" in default_criterion.reason
    # An explicit higher floor also blocks.
    explicit = check_flip_criterion(report, min_sample=1000)
    assert explicit.ready is False


# ----------------------------------------------------------------------
# 15. total_compared_override path (opt-in denominator)
# ----------------------------------------------------------------------


def test_total_compared_override_path() -> None:
    # Override supplies a larger denominator than the observed heartbeats.
    stream = _agree_stream(10, start=NOW) + [
        diff_envelope(_ts(NOW), fields=("allow",)),
    ]
    report = analyze_consistency(stream, now=NOW, total_compared_override=1000)
    assert report.total_compared == 1000
    assert report.mismatches == 1
    assert report.agreements == 999
    assert report.consistency == pytest.approx(999 / 1000)

    # Override below the observed diff count clamps agreements at 0.
    clamp_stream = _agree_stream(2, start=NOW) + [
        diff_envelope(_ts(NOW), fields=("allow",)),
        diff_envelope(_ts(NOW), fields=("mode",)),
    ]
    clamped = analyze_consistency(
        clamp_stream, now=NOW, total_compared_override=1
    )
    assert clamped.total_compared == 1
    assert clamped.mismatches == 2
    assert clamped.agreements == 0  # max(0, ...) -- never negative
    assert clamped.consistency == 0.0


# ----------------------------------------------------------------------
# 16. Analyzer emits no audit, does no I/O, consumes a generator once
# ----------------------------------------------------------------------


def test_analyzer_emits_no_audit_and_does_no_io() -> None:
    # The analyzer has NO sink parameter at all: inspect the signature.
    sig = inspect.signature(analyze_consistency)
    param_names = set(sig.parameters.keys())
    assert "sink" not in param_names
    assert "audit_sink" not in param_names
    # Only the documented parameters exist.
    assert param_names == {
        "envelopes",
        "now",
        "window",
        "total_compared_override",
    }

    # Feed a single-pass generator; materialization is internal and a
    # generator must be consumed correctly in one pass.
    def gen():
        yield from _agree_stream(4, start=NOW)
        yield diff_envelope(_ts(NOW), fields=("risk_level",))

    report = analyze_consistency(gen(), now=NOW)
    assert report.total_compared == 4
    assert report.mismatches == 1
    assert report.agreements == 3
    assert report.consistency == pytest.approx(3 / 4)


# ----------------------------------------------------------------------
# 17. Error-wins-plus-malformed: one envelope, double outcome
# ----------------------------------------------------------------------


def test_error_wins_also_counts_malformed_not_double_counted() -> None:
    # One shadow diff envelope with BOTH new_engine_error set AND non-empty
    # mismatched_fields. NEW_ENGINE_ERROR wins the classification (sole
    # bucket), the envelope is ALSO flagged malformed, and it is NOT
    # double-counted as a field mismatch.
    stream = _agree_stream(1, start=NOW) + [
        diff_envelope(_ts(NOW), fields=("allow",), err="boom"),
    ]
    report = analyze_consistency(stream, now=NOW)
    assert report.new_engine_errors == 1
    assert report.malformed_records == 1
    assert report.mismatches == 0
    # Sole bucket is NEW_ENGINE_ERROR.
    assert report.mismatch_by_field.get(MismatchField.NEW_ENGINE_ERROR) == 1
    assert sum(report.mismatch_by_field.values()) == 1
    record = report.mismatch_records[0]
    assert record.categories == (MismatchField.NEW_ENGINE_ERROR,)
    assert record.new_engine_error == "boom"
    # Heartbeat denominator is unaffected.
    assert report.total_compared == 1


# ----------------------------------------------------------------------
# 18. Timestamp-format normalization lands inside the window (parametrized)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "timestamp_str",
    [
        # Z-suffixed: exactly at now -> in window.
        "2026-08-03T12:00:00Z",
        # Non-UTC offset: naive reading is 12:00 (== now, edge) but the UTC
        # equivalent is 06:30, comfortably inside the window.
        "2026-08-03T12:00:00+05:30",
        # Naive (no suffix): treated as UTC, exactly at now -> in window.
        "2026-08-03T12:00:00",
    ],
)
def test_timestamp_format_normalization_in_window(timestamp_str: str) -> None:
    # Each of these ISO-8601 spellings must normalize to UTC and land
    # inside the window, contributing one in-window heartbeat.
    report = analyze_consistency([heartbeat(timestamp_str)], now=NOW)
    assert report.total_compared == 1
    assert report.agreements == 1
    assert report.malformed_records == 0


# ----------------------------------------------------------------------
# Constructor / argument validation (fail-closed)
# ----------------------------------------------------------------------


def test_analyze_rejects_non_positive_window() -> None:
    with pytest.raises(ValueError):
        analyze_consistency([], now=NOW, window=timedelta(0))
    with pytest.raises(ValueError):
        analyze_consistency([], now=NOW, window=timedelta(days=-1))


def test_check_flip_criterion_rejects_bad_threshold_and_min_sample() -> None:
    report = analyze_consistency(_agree_stream(5, start=NOW), now=NOW)
    with pytest.raises(ValueError):
        check_flip_criterion(report, threshold=-0.1)
    with pytest.raises(ValueError):
        check_flip_criterion(report, threshold=1.5)
    with pytest.raises(ValueError):
        check_flip_criterion(report, min_sample=0)


def test_defaults_match_documented_constants() -> None:
    assert DEFAULT_FLIP_THRESHOLD == 0.995
    assert DEFAULT_WINDOW == timedelta(days=7)
    # Naive `now` is treated as UTC; aware non-UTC converted to UTC.
    naive = datetime(2026, 8, 3, 12, 0, 0)
    report = analyze_consistency(_agree_stream(1, start=NOW), now=naive)
    assert report.now.tzinfo is UTC


def test_window_membership_uses_injected_now_not_clock() -> None:
    # A record far in the future relative to `now` is out of window.
    future = heartbeat(_ts(NOW + DAY))
    past = heartbeat(_ts(NOW - DEFAULT_WINDOW - DAY))
    report = analyze_consistency([future, past], now=NOW)
    assert report.total_compared == 0
    assert report.consistency is None
