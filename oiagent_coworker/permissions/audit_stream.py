# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W3-2 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Implements the W3-2 audit-stream
#     persistence + ingestion layer: a standalone append-only JSONL sink
#     (fsync-per-line, lazy create) plus a tolerant single-pass reader
#     feeding the W3-1 consistency analyzer. Read-side timestamp
#     pre-filter is a coarse I/O optimization only; the analyzer window
#     is authoritative.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""OIagent Coworker -- W3-2 audit-stream persistence + ingestion layer.

This module provides the durable, append-only on-disk form of the
``kind="permission"`` audit stream that the W3-1 consistency analyzer
(:mod:`oiagent_coworker.permissions.consistency`) consumes. It has two
halves:

  * :class:`AuditStreamSink` -- a standalone append-only JSONL sink that
    conforms to the ``AuditSink`` Protocol. Each ``__call__`` writes one
    JSON line (``write + flush + fsync``) so a crash leaves a parseable
    prefix. Construction is lazy: NO filesystem touch until the first
    ``__call__``.
  * :class:`AuditStreamReader` (and the functional
    :func:`read_audit_stream`) -- a single-pass, never-raising generator
    that yields only clean parsed envelope mappings and reports
    storage-level corruption via :class:`ReadStats`.

Adjudicated design (W3-2 contract)
----------------------------------
T1 -- standalone sink. Fan-out to ``oiagent.audit.P2_10`` is a
    deployment concern, NOT this module. The sink writes to its own
    file only; there is no built-in tee.
T2 -- reader/analyzer separation. :class:`ReadStats` reports
    STORAGE-level corruption (unparseable JSON lines, non-dict lines,
    truncated tail). This is DISTINCT from the analyzer's
    ``malformed_records`` (gate-signal: structurally malformed
    ENVELOPES). They are never folded together.
T3 -- single append-only file + read-side timestamp pre-filter.
    STRENGTHENED RED LINE: reader ``since``/``until`` bounds are a
    coarse I/O optimization ONLY; the analyzer's ``window`` is
    authoritative. When in doubt, pass loose reader bounds or NONE and
    let the analyzer decide. The probe recipe below uses bounds purely
    as an I/O optimization; they are semantically redundant because the
    reader bounds are a superset of the analyzer window.
T4 -- fsync-per-line. Repo convention (mirrors persistence.py). The
    explicit crash contract is: "all lines before the last are
    complete." A torn final line is therefore expected and tolerated.
Probe -- documented recipe, NOT a shipped function (preserves the
    no-clock-read purity red line). See "Probe recipe" below.
No min_sample / traffic assumption anywhere in this module. Those stay
    runtime parameters of ``check_flip_criterion``/``analyze_consistency``.

Serialization contract
----------------------
On-disk keys per line:
  ``kind``            -- verbatim from the decision.
  ``timestamp``       -- ``decision.timestamp.isoformat()``.
  ``engine_decision`` -- ``verdict.to_dict()`` or ``None``.
  ``metadata``        -- verbatim.
  ``error``           -- verbatim.
Line = ``json.dumps(..., ensure_ascii=False, sort_keys=True) + "\\n"``.
The round-trip satisfies ``consistency.py``'s ``_parse_timestamp`` and
``_classify_diff`` (``mismatched_fields`` as a JSON list is accepted by
the analyzer).

Reader contract
---------------
Single-pass generator; yields only clean ``Mapping``; never materializes
the file, except in the explicit one-shot :func:`read_audit_stream`,
which consumes eagerly to return a final :class:`ReadStats`. Never raises
on corrupt content. Bounds (``since``/``until``)
are INCLUSIVE both ends, normalized via the same ``_to_utc`` semantics
as the analyzer (naive treated as UTC), and are a coarse I/O pre-filter
only. Out-of-bounds lines are skipped WITHOUT counting as corrupt. A
line with an unparseable/missing timestamp is NOT bounds-dropped -- it
is yielded so the analyzer routes it to ``malformed_records``. A missing
file yields an empty iterator + zeroed :class:`ReadStats`. The reader
emits ZERO audit.

Corruption taxonomy (T2)
------------------------
  * Truncated LAST line (valid-prefix JSON fragment, e.g. a torn tail):
    benign. ``corrupt_last_line_truncated=True``; NOT counted in
    ``corrupt_lines``; no WARNING.
  * Corrupt MIDDLE line (any line before the last that fails to parse
    or is not a JSON object): WARNING log + ``corrupt_lines`` increment.
  * The reader NEVER raises on corrupt content.

Probe recipe (NOT shipped -- run ad hoc)
----------------------------------------
To sample the recent stream for a flip-criterion read, construct a
reader with loose bounds and let the analyzer decide::

    reader = AuditStreamReader(stream_path)
    report = analyze_consistency(
        reader.envelopes(since=now - 8*day, until=now),  # I/O hint only
        now=now,
        window=7*day,                                     # authoritative
    )
    flip = check_flip_criterion(report, min_sample=1000)

The reader bounds here are purely an I/O optimization (semantically
redundant, since reader bounds are a superset of the analyzer window).
When in doubt, omit them entirely and let the analyzer window decide.
``now`` must be supplied by the caller; this module never reads a clock.

Anti-flattery boundary (see plan §3.1 / §8.1.1):
    - No upstream OpenWorker import anywhere in this file.
    - No vault-path resolution, no environment-variable reads, no
      vault-path-helper import -- ``stream_path`` is injected as a
      ``Path`` (like PolicyGate ``flags_path``).
    - The module never reads a clock.
    - The reader emits ZERO audit (no sink parameter).
    - No ``min_sample`` / traffic assumption anywhere.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from oiagent_coworker.permissions.audit import AuditDecision

__all__ = [
    "AuditStreamSink",
    "AuditStreamReader",
    "ReadStats",
    "read_audit_stream",
    "serialize_envelope",
]

_LOGGER = logging.getLogger(__name__)


def _to_utc(dt: datetime) -> datetime:
    """Normalize a ``datetime`` to aware UTC.

    Naive datetimes are treated as UTC; aware non-UTC datetimes are
    converted to UTC. Mirrors ``consistency._to_utc`` semantics exactly.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@dataclass(frozen=True)
class ReadStats:
    """Storage-level read accounting for one pass over the stream.

    This is DISTINCT from the analyzer's ``malformed_records`` (T2):
    ``ReadStats`` reports STORAGE corruption (lines that are not clean
    JSON objects); the analyzer reports GATE-signal malformation
    (structurally bad envelopes). Never fold them together.

    Attributes:
        lines_read: Total non-empty physical lines examined.
        envelopes_yielded: Clean parsed envelope mappings yielded.
        corrupt_lines: Corrupt MIDDLE lines skipped (each WARNING-logged).
            Excludes the truncated-last-line case.
        corrupt_last_line_truncated: True when the final physical line is
            a torn/truncated fragment (benign crash artifact, per the T4
            fsync-per-line contract). Not counted in ``corrupt_lines``.
    """

    lines_read: int = 0
    envelopes_yielded: int = 0
    corrupt_lines: int = 0
    corrupt_last_line_truncated: bool = False


def serialize_envelope(decision: AuditDecision) -> dict[str, Any]:
    """Serialize an ``AuditDecision`` to a JSON-safe envelope dict.

    Args:
        decision: The decision envelope to serialize.

    Returns:
        A dict with keys ``kind`` / ``timestamp`` / ``engine_decision`` /
        ``metadata`` / ``error`` per the W3-2 serialization contract.

    Raises:
        TypeError: If ``decision`` is not an ``AuditDecision``.
    """
    if not isinstance(decision, AuditDecision):
        raise TypeError(
            f"serialize_envelope requires AuditDecision, "
            f"got {type(decision).__name__}"
        )
    verdict = decision.engine_decision
    return {
        "kind": decision.kind,
        "timestamp": decision.timestamp.isoformat(),
        "engine_decision": verdict.to_dict() if verdict is not None else None,
        "metadata": decision.metadata,
        "error": decision.error,
    }


class AuditStreamSink:
    """Standalone append-only JSONL audit sink (W3-2, T1).

    Conforms to the ``AuditSink`` Protocol. Each ``__call__`` appends one
    JSON line with ``write + flush + fsync`` (T4) so a crash leaves a
    parseable prefix: the explicit contract is "all lines before the last
    are complete."

    Lazy creation: ``__init__`` does NOT touch the filesystem. The stream
    file (and its parent directory) is created on the first ``__call__``.

    Error contract: ``__call__`` raises ``TypeError`` on a
    non-``AuditDecision`` argument. I/O errors are logged (WARNING) and
    swallowed -- mirroring the engine.py audit best-effort contract so a
    failing stream never breaks the verdict path. Type errors raise;
    I/O errors do not.
    """

    def __init__(self, stream_path: Path) -> None:
        self._stream_path: Path = Path(stream_path)

    @property
    def stream_path(self) -> Path:
        """The append-only stream file path (lazy; may not exist yet)."""
        return self._stream_path

    def __call__(self, decision: AuditDecision) -> None:
        """Append one envelope line (write + flush + fsync).

        Raises:
            TypeError: If ``decision`` is not an ``AuditDecision``.
        """
        envelope = serialize_envelope(decision)  # raises TypeError on bad type
        try:
            parent = self._stream_path.parent
            if parent and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            # newline="\n": LF-deterministic on-disk JSONL (no platform
            # newline translation), so the format is LF-stable for
            # cross-platform replay.
            with open(
                self._stream_path, "a", encoding="utf-8", newline="\n"
            ) as fp:
                fp.write(json.dumps(envelope, ensure_ascii=False, sort_keys=True))
                fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
        except OSError as exc:
            _LOGGER.warning(
                "AuditStreamSink failed to append to %s (%s); decision dropped",
                self._stream_path, exc,
            )


class AuditStreamReader:
    """Single-pass, tolerant reader over the append-only stream (W3-2).

    Construction is lazy: NO filesystem touch until ``envelopes()`` is
    iterated. The reader never raises on corrupt content and never
    materializes the file. It emits ZERO audit.
    """

    def __init__(self, stream_path: Path) -> None:
        self._stream_path: Path = Path(stream_path)
        self._last_stats: ReadStats = ReadStats()

    @property
    def stream_path(self) -> Path:
        """The stream file path (lazy; may not exist)."""
        return self._stream_path

    @property
    def last_stats(self) -> ReadStats:
        """Stats from the most recent ``envelopes()`` pass.

        Populated only after the generator is fully consumed. Before any
        pass, returns a zeroed :class:`ReadStats`.
        """
        return self._last_stats

    def envelopes(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> Iterator[Mapping[str, Any]]:
        """Yield clean parsed envelope mappings, single pass.

        Bounds are INCLUSIVE both ends and normalized via ``_to_utc``
        (naive treated as UTC). They are a coarse I/O pre-filter ONLY;
        the analyzer's ``window`` is authoritative (T3). Out-of-bounds
        lines are skipped WITHOUT counting as corrupt. A line with an
        unparseable/missing timestamp is NOT bounds-dropped -- it is
        yielded so the analyzer routes it to ``malformed_records``.

        Args:
            since: Optional inclusive lower bound on ``timestamp``.
            until: Optional inclusive upper bound on ``timestamp``.

        Yields:
            Clean parsed envelope mappings (JSON objects).
        """
        since_utc = _to_utc(since) if since is not None else None
        until_utc = _to_utc(until) if until is not None else None

        lines_read = 0
        yielded = 0
        corrupt = 0
        truncated_last = False

        if not self._stream_path.exists():
            self._last_stats = ReadStats()
            return

        # Single-pass, O(1)-memory: buffer only the PREVIOUS non-empty
        # line. A buffered line is known to be a MIDDLE line as soon as a
        # NEXT non-empty line is seen; only the FINAL buffered line (no
        # successor) is the torn-tail candidate. Never slurp the file.
        with open(self._stream_path, "r", encoding="utf-8") as fp:
            prev: tuple[int, str] | None = None  # (index, raw) last non-empty
            idx = -1
            for raw in fp:
                idx += 1
                if not raw.strip():
                    continue
                if prev is not None:
                    # prev is NOT the last non-empty line -> MIDDLE line.
                    r = self._process_line(
                        prev[0], prev[1], False, since_utc, until_utc
                    )
                    lines_read += r[0]
                    corrupt += r[1]
                    if r[2]:
                        truncated_last = True
                    if r[3] is not None:
                        yielded += 1
                        yield r[3]
                prev = (idx, raw)
            if prev is not None:
                # prev IS the last non-empty line -> torn-tail carve-out.
                r = self._process_line(
                    prev[0], prev[1], True, since_utc, until_utc
                )
                lines_read += r[0]
                corrupt += r[1]
                if r[2]:
                    truncated_last = True
                if r[3] is not None:
                    yielded += 1
                    yield r[3]

        self._last_stats = ReadStats(
            lines_read=lines_read,
            envelopes_yielded=yielded,
            corrupt_lines=corrupt,
            corrupt_last_line_truncated=truncated_last,
        )

    def _process_line(
        self,
        index: int,
        raw: str,
        is_last: bool,
        since_utc: datetime | None,
        until_utc: datetime | None,
    ) -> tuple[int, int, bool, Mapping[str, Any] | None]:
        """Classify + maybe-emit one non-empty physical line.

        Args:
            index: Zero-based physical line index (for WARNING logs).
            raw: The raw line text (unstripped).
            is_last: True iff this is the LAST non-empty line in the file
                (torn-tail carve-out applies to it).
            since_utc: Inclusive lower bound (coarse I/O pre-filter).
            until_utc: Inclusive upper bound (coarse I/O pre-filter).

        Returns:
            ``(lines_read, corrupt, truncated_last, payload)`` where
            ``lines_read`` is always 1; ``corrupt`` is 1 iff the line is a
            corrupt MIDDLE line; ``truncated_last`` is True iff this is a
            torn-tail last line; ``payload`` is the clean envelope mapping
            to yield, or ``None`` to emit nothing.
        """
        line = raw.strip()
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            if is_last:
                # Torn tail: a truncated JSON FRAGMENT (writer died
                # mid-write). Benign crash artifact (T4). Not counted.
                return (1, 0, True, None)
            _LOGGER.warning(
                "audit_stream: skipping corrupt line %d in %s: %s",
                index + 1, self._stream_path, exc,
            )
            return (1, 1, False, None)
        if not isinstance(payload, dict):
            # A well-formed but non-dict JSON value (list/scalar) is NOT a
            # torn tail -- the writer finished; the payload is wrong-shaped.
            # This is a real producer bug: ALWAYS count + WARNING, even when
            # it is the last line.
            _LOGGER.warning(
                "audit_stream: line %d in %s is not a JSON object; "
                "skipping", index + 1, self._stream_path,
            )
            return (1, 1, False, None)

        # Coarse I/O pre-filter (T3). A missing/unparseable timestamp is
        # NOT bounds-dropped -- yield it and let the analyzer route it to
        # malformed_records.
        if since_utc is not None or until_utc is not None:
            ts = _parse_line_timestamp(payload.get("timestamp"))
            if ts is not None:
                if since_utc is not None and ts < since_utc:
                    return (1, 0, False, None)  # out-of-bounds, NOT corrupt
                if until_utc is not None and ts > until_utc:
                    return (1, 0, False, None)  # out-of-bounds, NOT corrupt

        return (1, 0, False, payload)


def _parse_line_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp into aware UTC, or ``None``.

    Used ONLY for the reader's coarse I/O pre-filter. ``None`` means the
    line is NOT bounds-dropped (yielded so the analyzer routes it to
    malformed_records). Mirrors ``consistency._parse_timestamp``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _to_utc(parsed)


def read_audit_stream(
    stream_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> tuple[Iterator[Mapping[str, Any]], ReadStats]:
    """Functional one-shot read of the audit stream.

    Convenience wrapper around :class:`AuditStreamReader`. The returned
    iterator is lazy; the returned :class:`ReadStats` is populated only
    after the iterator is fully consumed (it is the reader's
    ``last_stats``). Because the stats object is replaced per pass, this
    one-shot consumes the stream eagerly into a list internally so the
    stats are final when returned.

    Args:
        stream_path: Path to the append-only JSONL stream.
        since: Optional inclusive lower bound (coarse I/O pre-filter).
        until: Optional inclusive upper bound (coarse I/O pre-filter).

    Returns:
        ``(envelopes, stats)`` where ``envelopes`` is an iterator over
        clean parsed mappings and ``stats`` is the final :class:`ReadStats`.
    """
    reader = AuditStreamReader(stream_path)
    envelopes = list(reader.envelopes(since=since, until=until))
    return iter(envelopes), reader.last_stats
