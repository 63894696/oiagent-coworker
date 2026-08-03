# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_audit_stream.py (new file)
#   Upstream commit:  not present (W3-2 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W3-2; tests the audit-stream persistence +
#     ingestion layer (sink serialization, fsync-per-line, lazy create,
#     reader corruption taxonomy, read-side pre-filter vs analyzer
#     window authority, and the round-trip keystone into the real W3-1
#     analyzer).
#   - 19 tests, no external deps beyond pytest. tmp_path fixtures are
#     used for synthetic stream files.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Tests for oiagent_coworker.permissions.audit_stream -- W3-2 acceptance.

Covers the W3-2 contract: sink append + formatting, round-trip keystone
through the real ``analyze_consistency``, corruption taxonomy (truncated
tail benign vs corrupt middle), read-side pre-filter vs analyzer window
authority, empty/missing file handling, non-JSONL / non-dict lines,
Mapping shape, unparseable-timestamp passthrough, source purity red
lines (no ``datetime.now(``, no forbidden tokens), lazy creation,
AuditSink Protocol conformance, and fsync-per-line.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No vault-path resolution; all roots are tmp_path fixtures.
    - No audit assertions on the reader (it emits zero audit).
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.audit import AuditDecision, AuditSink
from oiagent_coworker.permissions.audit_stream import (
    AuditStreamReader,
    AuditStreamSink,
    ReadStats,
    read_audit_stream,
    serialize_envelope,
)
from oiagent_coworker.permissions.consistency import analyze_consistency
from oiagent_coworker.permissions.engine import PermissionMode, Verdict


def _verdict() -> Verdict:
    return Verdict(
        allow=True,
        mode=PermissionMode.SYNC,
        reason="ok",
        risk_level="read",
        requires_approval=False,
    )


def _heartbeat(ts: datetime) -> AuditDecision:
    """Engine heartbeat: kind=permission, no policy_gate metadata."""
    return AuditDecision(kind="permission", timestamp=ts, engine_decision=_verdict())


def _shadow_diff(ts: datetime, mismatched: list[str]) -> AuditDecision:
    """Shadow-mode diff envelope (one compared-and-disagreed decision)."""
    return AuditDecision(
        kind="permission",
        timestamp=ts,
        engine_decision=_verdict(),
        metadata={
            "policy_gate": {
                "mode": "shadow",
                "diff": {
                    "action_kind": "write_file",
                    "action_target": "/tmp/x",
                    "legacy_verdict": {"allow": True},
                    "new_verdict": {"allow": False},
                    "mismatched_fields": mismatched,
                    "new_engine_error": None,
                },
            }
        },
    )


def _write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------
# 1. Sink appends one valid JSONL line per __call__ (all 5 keys present)
# ----------------------------------------------------------------------


def test_01_sink_appends_one_valid_jsonl_line(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    decision = _heartbeat(datetime(2026, 8, 3, tzinfo=UTC))
    sink(decision)
    sink(decision)

    lines = stream.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        payload = json.loads(line)
        assert set(payload.keys()) == {
            "kind", "timestamp", "engine_decision", "metadata", "error",
        }


# ----------------------------------------------------------------------
# 2. Sink line uses repo formatting flags (sorted keys, ensure_ascii=False,
#    exactly one trailing newline)
# ----------------------------------------------------------------------


def test_02_sink_line_formatting_flags(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    decision = AuditDecision(
        kind="permission",
        timestamp=datetime(2026, 8, 3, tzinfo=UTC),
        engine_decision=_verdict(),
        metadata={"note": "héllo"},  # non-ASCII to prove ensure_ascii=False
    )
    sink(decision)

    raw = stream.read_bytes().decode("utf-8")
    # LF-deterministic (sink opens with newline="\n"): no CR bytes at all.
    assert "\r" not in raw
    # Exactly one trailing LF.
    assert raw.endswith("\n") and not raw.endswith("\n\n")
    body = raw[:-1]  # strip the single trailing LF
    # Sorted keys: json.dumps with sort_keys=True reproduces the body.
    assert body == json.dumps(
        json.loads(body), ensure_ascii=False, sort_keys=True
    )
    # ensure_ascii=False: the non-ASCII char is literal, not escaped.
    assert "héllo" in body
    assert "\\u00e9" not in body


# ----------------------------------------------------------------------
# 3. KEYSTONE: round-trip sink -> reader -> real analyzer
# ----------------------------------------------------------------------


def test_03_keystone_roundtrip_through_real_analyzer(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    now = datetime(2026, 8, 3, tzinfo=UTC)

    N = 5  # heartbeats
    M = 2  # shadow diff mismatches
    for i in range(N):
        sink(_heartbeat(now - timedelta(hours=i + 1)))
    for i in range(M):
        sink(_shadow_diff(now - timedelta(minutes=i + 1), ["allow"]))

    reader = AuditStreamReader(stream)
    report = analyze_consistency(reader.envelopes(), now=now)

    assert report.total_compared == N
    assert report.mismatches == M
    expected = (N - M) / N
    assert report.consistency == pytest.approx(expected)
    assert report.malformed_records == 0


# ----------------------------------------------------------------------
# 4. Corrupt LAST line tolerated (truncated tail is benign)
# ----------------------------------------------------------------------


def test_04_corrupt_last_line_tolerated(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    for i in range(3):
        sink(_heartbeat(datetime(2026, 8, 3, tzinfo=UTC)))
    # Append a torn tail (no trailing newline).
    with open(stream, "a", encoding="utf-8") as fp:
        fp.write('{"kind": "perm')

    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    stats = reader.last_stats
    assert len(envelopes) == 3
    assert stats.corrupt_lines == 0
    assert stats.corrupt_last_line_truncated is True


# ----------------------------------------------------------------------
# 5. Corrupt MIDDLE line skipped-and-counted
# ----------------------------------------------------------------------


def test_05_corrupt_middle_line_counted(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    sink(_heartbeat(datetime(2026, 8, 3, tzinfo=UTC)))
    with open(stream, "a", encoding="utf-8") as fp:
        fp.write("this is not json\n")
    sink(_heartbeat(datetime(2026, 8, 3, tzinfo=UTC)))

    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    stats = reader.last_stats
    assert len(envelopes) == 2
    assert stats.corrupt_lines == 1
    assert stats.corrupt_last_line_truncated is False


# ----------------------------------------------------------------------
# 6. Window slicing: reader since pre-filter drops out-of-window lines
# ----------------------------------------------------------------------


def test_06_reader_since_prefilter(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    t10 = now - timedelta(days=10)
    t3 = now - timedelta(days=3)
    t1 = now - timedelta(days=1)
    for ts in (t10, t3, t1):
        sink(_heartbeat(ts))

    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes(since=now - timedelta(days=7)))
    stamps = {e["timestamp"] for e in envelopes}
    assert stamps == {t3.isoformat(), t1.isoformat()}
    # Out-of-window line is skipped WITHOUT counting as corrupt.
    assert reader.last_stats.corrupt_lines == 0


# ----------------------------------------------------------------------
# 7. Reader bounds are pre-filter; analyzer window is authoritative
# ----------------------------------------------------------------------


def test_07_analyzer_window_is_authority(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    t10 = now - timedelta(days=10)
    t3 = now - timedelta(days=3)
    t1 = now - timedelta(days=1)
    for ts in (t10, t3, t1):
        sink(_heartbeat(ts))

    # Read with NO bounds; analyzer window=7d decides.
    reader = AuditStreamReader(stream)
    report = analyze_consistency(
        reader.envelopes(), now=now, window=timedelta(days=7)
    )
    # Only t3 and t1 are in-window -> 2 heartbeats.
    assert report.total_compared == 2


# ----------------------------------------------------------------------
# 8. Empty file -> yields nothing, stats zero, no raise
# ----------------------------------------------------------------------


def test_08_empty_file(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    stream.write_text("", encoding="utf-8")
    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    assert envelopes == []
    assert reader.last_stats == ReadStats()


# ----------------------------------------------------------------------
# 9. Missing file -> yields nothing, stats zero, no raise (lazy)
# ----------------------------------------------------------------------


def test_09_missing_file(tmp_path: Path) -> None:
    stream = tmp_path / "does_not_exist.jsonl"
    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    assert envelopes == []
    assert reader.last_stats == ReadStats()


# ----------------------------------------------------------------------
# 10. Non-JSONL plain-text line -> skipped-and-counted, not yielded
# ----------------------------------------------------------------------


def test_10_plain_text_line_counted(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    _write_lines(stream, [
        json.dumps({"kind": "permission", "timestamp": "2026-08-03T00:00:00+00:00"}) + "\n",
        "hello world plain text\n",
        json.dumps({"kind": "permission", "timestamp": "2026-08-03T00:01:00+00:00"}) + "\n",
    ])
    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    assert len(envelopes) == 2
    assert reader.last_stats.corrupt_lines == 1


# ----------------------------------------------------------------------
# 11. Non-dict JSON line -> skipped-and-counted, not yielded
# ----------------------------------------------------------------------


def test_11_non_dict_json_line_counted(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    _write_lines(stream, [
        json.dumps({"kind": "permission", "timestamp": "2026-08-03T00:00:00+00:00"}) + "\n",
        "[1, 2, 3]\n",
        json.dumps({"kind": "permission", "timestamp": "2026-08-03T00:01:00+00:00"}) + "\n",
    ])
    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    assert len(envelopes) == 2
    assert reader.last_stats.corrupt_lines == 1


# ----------------------------------------------------------------------
# 12. Every yielded item isinstance(Mapping); analyzer completes clean
# ----------------------------------------------------------------------


def test_12_all_yielded_are_mappings_and_analyzer_clean(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    sink = AuditStreamSink(stream)
    now = datetime(2026, 8, 3, tzinfo=UTC)
    for i in range(4):
        sink(_heartbeat(now - timedelta(hours=i)))

    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    assert envelopes and all(isinstance(e, Mapping) for e in envelopes)

    reader2 = AuditStreamReader(stream)
    report = analyze_consistency(reader2.envelopes(), now=now)
    assert report.malformed_records == 0


# ----------------------------------------------------------------------
# 13. Unparseable timestamp is yielded even when bounds set
# ----------------------------------------------------------------------


def test_13_unparseable_timestamp_yielded_despite_bounds(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    _write_lines(stream, [
        json.dumps({"kind": "permission", "timestamp": "not-a-date"}) + "\n",
    ])
    reader = AuditStreamReader(stream)
    envelopes = list(
        reader.envelopes(
            since=datetime(2026, 8, 1, tzinfo=UTC),
            until=datetime(2026, 8, 2, tzinfo=UTC),
        )
    )
    # Not bounds-dropped; analyzer routes it to malformed_records.
    assert len(envelopes) == 1
    assert envelopes[0]["timestamp"] == "not-a-date"


# ----------------------------------------------------------------------
# 14. No datetime.now( in module source
# ----------------------------------------------------------------------


def test_14_no_datetime_now_in_module_source() -> None:
    import oiagent_coworker.permissions.audit_stream as mod

    source = inspect.getsource(mod)
    assert "datetime.now(" not in source


# ----------------------------------------------------------------------
# 15. No forbidden tokens in module source
# ----------------------------------------------------------------------


def test_15_no_forbidden_tokens_in_module_source() -> None:
    import oiagent_coworker.permissions.audit_stream as mod

    source = inspect.getsource(mod)
    for token in ("OIAGENT_VAULT", "os.environ", "oiagent.vault.path", "import openworker"):
        assert token not in source, f"forbidden token present: {token}"


# ----------------------------------------------------------------------
# 16. Sink lazy creation: construct does NOT create file; first call does
# ----------------------------------------------------------------------


def test_16_sink_lazy_creation(tmp_path: Path) -> None:
    stream = tmp_path / "nested" / "dir" / "audit.jsonl"
    sink = AuditStreamSink(stream)
    assert not stream.exists()
    assert not stream.parent.exists()

    sink(_heartbeat(datetime(2026, 8, 3, tzinfo=UTC)))
    assert stream.exists()
    assert stream.parent.exists()


# ----------------------------------------------------------------------
# 17. Sink conforms to AuditSink Protocol (@runtime_checkable)
# ----------------------------------------------------------------------


def test_17_sink_conforms_to_auditsink_protocol(tmp_path: Path) -> None:
    sink = AuditStreamSink(tmp_path / "audit.jsonl")
    assert isinstance(sink, AuditSink)


# ----------------------------------------------------------------------
# 18. fsync-per-line: os.fsync called once per __call__
# ----------------------------------------------------------------------


def test_18_fsync_per_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import oiagent_coworker.permissions.audit_stream as mod

    calls: list[int] = []
    real_fsync = mod.os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", spy)

    sink = AuditStreamSink(tmp_path / "audit.jsonl")
    sink(_heartbeat(datetime(2026, 8, 3, tzinfo=UTC)))
    sink(_heartbeat(datetime(2026, 8, 3, tzinfo=UTC)))
    assert len(calls) == 2


# ----------------------------------------------------------------------
# 19. F-B1 PIN: a NON-DICT last line is corrupt (NOT a torn tail).
#     Only a JSONDecodeError last line sets the truncated flag (test 4).
# ----------------------------------------------------------------------


def test_19_non_dict_last_line_is_corrupt_not_truncated(tmp_path: Path) -> None:
    stream = tmp_path / "audit.jsonl"
    _write_lines(stream, [
        json.dumps({"kind": "permission", "timestamp": "2026-08-03T00:00:00+00:00"}) + "\n",
        "[1, 2, 3]",  # well-formed complete JSON, but wrong-shaped, NO newline
    ])
    reader = AuditStreamReader(stream)
    envelopes = list(reader.envelopes())
    stats = reader.last_stats
    # Producer bug must surface: counted as corrupt, NOT a benign torn tail.
    assert len(envelopes) == 1
    assert stats.corrupt_lines == 1
    assert stats.corrupt_last_line_truncated is False
