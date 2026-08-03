# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W3-1 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Implements the W3-1 shadow-mode
#     verdict-diff consistency analyzer: a rolling agreement consistency
#     metric over the kind="permission" audit stream (engine-heartbeat
#     denominator), per-field mismatch classification (allow /
#     requires_approval / risk_level / mode / new_engine_error), and the
#     W3 Phase C shadow->enforce flip-criterion check.
#   - Pure analysis: zero I/O, zero audit emission, injected evaluation
#     instant (no datetime.now(), no vault resolution, no env vars).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""OIagent Coworker -- W3-1 shadow-mode verdict-diff consistency analyzer.

This module consumes an injected iterable of ``kind="permission"``
audit-envelope dicts (the serialized form of
:class:`~oiagent_coworker.permissions.audit.AuditDecision`) and produces:

  * a rolling agreement consistency metric
    (:class:`ConsistencyReport`),
  * per-field mismatch classification
    (:class:`MismatchField` / :class:`MismatchRecord`), and
  * a flip-criterion verdict (:class:`FlipCriterion`) for the W3 Phase C
    shadow -> enforce default flip.

Core assumption (ADJ-1)
------------------------
W3-1 assumes **one engine heartbeat per compared decision**. In SHADOW
mode the new engine emits exactly one ``AuditDecision(kind="permission",
engine_decision=verdict)`` per ``check()`` call, and the shadow gate
emits a ``metadata["policy_gate"]["diff"]`` envelope only on mismatch or
new-engine error. The per-check heartbeat IS the total-compared
denominator. Stream-provenance filtering (separating streams when
multiple gates feed one sink) is deferred to the later ingestion task;
callers are expected to hand this analyzer a single homogeneous stream.

Stream record classes
---------------------
Three record classes appear in the ``kind="permission"`` stream:

1. **Gate diff envelope** -- ``kind == "permission"`` AND
   ``metadata["policy_gate"]["diff"]`` present AND
   ``metadata["policy_gate"]["mode"] == "shadow"``. Each is one
   compared-and-disagreed (or new-engine-errored) decision.
2. **Engine heartbeat** -- ``kind == "permission"`` AND NO
   ``metadata["policy_gate"]`` key. Each is one sidecar run (one
   compared decision).
3. **Everything else** -- other ``kind`` values; gate envelopes with
   ``mode == "enforce"`` (including enforce-mode fallback envelopes) --
   ignored, not counted.

Anti-flattery boundary (see plan §3.1 / §8.1.1):
    - No ``import openworker`` anywhere in this file.
    - No ``${OIAGENT_VAULT}`` resolution, no env-var reads, no
      ``oiagent.vault.path`` import.
    - Zero audit emission; there is no sink parameter at all.
    - No file / network / log I/O; input is the injected iterable only.
    - No ``datetime.now()`` -- ``now`` is always a parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping

__all__ = [
    "MismatchField",
    "MismatchRecord",
    "ConsistencyReport",
    "FlipCriterion",
    "DEFAULT_FLIP_THRESHOLD",
    "DEFAULT_WINDOW",
    "analyze_consistency",
    "check_flip_criterion",
]

#: Default consistency threshold for the shadow -> enforce flip (99.5%).
DEFAULT_FLIP_THRESHOLD: float = 0.995

#: Default rolling analysis window (7 days).
DEFAULT_WINDOW: timedelta = timedelta(days=7)

#: Decision-bearing fields the shadow gate compares (mirrors
#: ``policy_gate._DIFF_FIELDS``) plus the new-engine-error category.
_KNOWN_MISMATCH_FIELDS: tuple[str, ...] = (
    "allow",
    "requires_approval",
    "risk_level",
    "mode",
)

_FIELD_TO_CATEGORY: dict[str, "MismatchField"] = {}


class MismatchField(Enum):
    """Classification bucket for a single mismatching decision field.

    A diff envelope with ``new_engine_error`` set is classified ONLY as
    :attr:`NEW_ENGINE_ERROR`, never double-counted as a field mismatch.
    """

    ALLOW = "allow"
    REQUIRES_APPROVAL = "requires_approval"
    RISK_LEVEL = "risk_level"
    MODE = "mode"
    NEW_ENGINE_ERROR = "new_engine_error"


_FIELD_TO_CATEGORY.update(
    {
        "allow": MismatchField.ALLOW,
        "requires_approval": MismatchField.REQUIRES_APPROVAL,
        "risk_level": MismatchField.RISK_LEVEL,
        "mode": MismatchField.MODE,
    }
)


@dataclass(frozen=True)
class MismatchRecord:
    """One in-window mismatching (or new-engine-errored) decision.

    Attributes:
        timestamp: When the compared decision was made (UTC).
        action_kind: ``Action.kind`` of the compared call (from the diff).
        action_target: ``Action.target`` of the compared call (from the diff).
        categories: Buckets this record counts toward. A record with a
            ``new_engine_error`` carries exactly
            ``(MismatchField.NEW_ENGINE_ERROR,)``; a field-mismatch record
            carries one entry per diverging field. A record counts once
            PER category in ``mismatch_by_field``.
        new_engine_error: ``str(exc)`` when the sidecar new engine raised.
    """

    timestamp: datetime
    action_kind: str
    action_target: str
    categories: tuple[MismatchField, ...]
    new_engine_error: str | None = None


@dataclass(frozen=True)
class ConsistencyReport:
    """Rolling agreement consistency metric over the injected stream.

    Attributes:
        now: Injected evaluation instant (window anchor).
        window: Rolling analysis window.
        total_compared: In-window engine heartbeats (or
            ``total_compared_override`` when injected).
        agreements: ``total_compared - mismatches - new_engine_errors``,
            clamped at 0.
        mismatches: In-window shadow diff envelopes with non-empty
            ``mismatched_fields`` and null ``new_engine_error``.
        new_engine_errors: In-window shadow diff envelopes with non-null
            ``new_engine_error``.
        malformed_records: Stream records that failed parsing or were
            structurally malformed (excluded from window math).
        consistency: ``agreements / total_compared`` when
            ``total_compared > 0``, else ``None``.
        mismatch_by_field: Record counts once PER category (sum of
            buckets >= ``mismatches``).
        mismatch_records: In-window mismatch records, in stream order.
    """

    now: datetime
    window: timedelta
    total_compared: int
    agreements: int
    mismatches: int
    new_engine_errors: int
    malformed_records: int
    consistency: float | None
    mismatch_by_field: dict[MismatchField, int] = field(default_factory=dict)
    mismatch_records: tuple[MismatchRecord, ...] = ()


@dataclass(frozen=True)
class FlipCriterion:
    """Verdict on whether the shadow -> enforce default flip is safe.

    Attributes:
        ready: True only when every flip condition holds.
        consistency: The report's consistency ratio (may be ``None``).
        threshold: Threshold that was evaluated against.
        window: The report's analysis window.
        total_compared: The report's denominator.
        agreements: The report's agreements count.
        min_sample: Minimum sample floor that was evaluated against.
        reason: Human-readable basis for the verdict.
    """

    ready: bool
    consistency: float | None
    threshold: float
    window: timedelta
    total_compared: int
    agreements: int
    min_sample: int
    reason: str


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _to_utc(dt: datetime) -> datetime:
    """Normalize a ``datetime`` to aware UTC.

    Naive datetimes are treated as UTC; aware non-UTC datetimes are
    converted to UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string into aware UTC, or ``None``.

    ``None`` is returned for missing / non-string / unparseable input so
    the caller can route the record to ``malformed_records`` (fail-closed,
    never raise on bad stream content).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _to_utc(parsed)


def _classify_diff(
    timestamp: datetime,
    diff: Mapping[str, Any],
) -> tuple[MismatchRecord | None, bool]:
    """Classify one shadow diff envelope.

    Returns ``(record, malformed)``:

    * ``new_engine_error`` set -> NEW_ENGINE_ERROR sole bucket. If the
      envelope ALSO carries mismatched fields, NEW_ENGINE_ERROR wins and
      the envelope is additionally flagged malformed.
    * otherwise, known field strings -> one category each; any unknown
      field string flags the record malformed (fail-closed) but the known
      fields still classify.
    * empty ``mismatched_fields`` AND null ``new_engine_error`` ->
      ``(None, True)``: structurally malformed diff (counted only).
    """
    action_kind = diff.get("action_kind")
    action_target = diff.get("action_target")
    kind_str = action_kind if isinstance(action_kind, str) else ""
    target_str = action_target if isinstance(action_target, str) else ""

    new_engine_error = diff.get("new_engine_error")
    raw_fields = diff.get("mismatched_fields")
    if isinstance(raw_fields, (list, tuple)):
        field_strings = [f for f in raw_fields if isinstance(f, str)]
    else:
        field_strings = []

    if new_engine_error is not None:
        record = MismatchRecord(
            timestamp=timestamp,
            action_kind=kind_str,
            action_target=target_str,
            categories=(MismatchField.NEW_ENGINE_ERROR,),
            new_engine_error=str(new_engine_error),
        )
        # Defensive: error AND fields present is a malformed combination;
        # NEW_ENGINE_ERROR wins and the envelope is also counted malformed.
        malformed = bool(field_strings)
        return record, malformed

    if not field_strings:
        # Diff envelope with empty fields AND null error: malformed.
        return None, True

    categories: list[MismatchField] = []
    malformed = False
    for name in field_strings:
        category = _FIELD_TO_CATEGORY.get(name)
        if category is None:
            # Unknown field string -> fail-closed to malformed.
            malformed = True
            continue
        categories.append(category)

    if not categories:
        # All field strings were unknown: no classification possible.
        return None, True

    record = MismatchRecord(
        timestamp=timestamp,
        action_kind=kind_str,
        action_target=target_str,
        categories=tuple(categories),
        new_engine_error=None,
    )
    return record, malformed


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def analyze_consistency(
    envelopes: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    window: timedelta = DEFAULT_WINDOW,
    total_compared_override: int | None = None,
) -> ConsistencyReport:
    """Analyze a stream of kind="permission" envelopes into a report.

    The analyzer is pure: it performs no I/O, emits no audit, and reads
    no clock -- ``now`` is the injected evaluation instant. The input is
    consumed in a SINGLE PASS; records are materialized internally only
    as the small list of in-window :class:`MismatchRecord` objects
    (heartbeats are counted, not stored), so generators are safe.

    Core assumption (ADJ-1): one engine heartbeat per compared decision.
    See the module docstring.

    Args:
        envelopes: Iterable of audit-envelope mappings (dict-like).
            Records that are not mappings, lack a parseable timestamp, or
            are structurally malformed are counted in
            ``malformed_records`` and never raise.
        now: Injected evaluation instant; naive treated as UTC, aware
            non-UTC converted to UTC. Window membership is
            ``now - window <= timestamp <= now`` (both bounds inclusive).
        window: Rolling analysis window; must be positive.
        total_compared_override: Optional escape hatch overriding the
            heartbeat-derived denominator. Defaults to ``None`` (use the
            in-window heartbeat count). When the override is below the
            observed in-window diff count, ``agreements`` is clamped at 0
            (never negative) and consistency reflects the discrepancy.

    Returns:
        A frozen :class:`ConsistencyReport`.

    Raises:
        ValueError: If ``window <= 0``.
    """
    if window <= timedelta(0):
        raise ValueError(f"window must be positive, got {window!r}")

    anchor = _to_utc(now)
    window_start = anchor - window

    heartbeats = 0
    mismatches = 0
    new_engine_errors = 0
    malformed_records = 0
    mismatch_by_field: dict[MismatchField, int] = {}
    mismatch_records: list[MismatchRecord] = []

    for envelope in envelopes:
        if not isinstance(envelope, Mapping):
            malformed_records += 1
            continue
        if envelope.get("kind") != "permission":
            # Other audit kinds are out of scope, not malformed.
            continue

        metadata = envelope.get("metadata")
        gate_meta = metadata.get("policy_gate") if isinstance(metadata, Mapping) else None

        timestamp = _parse_timestamp(envelope.get("timestamp"))
        if timestamp is None:
            malformed_records += 1
            continue

        if gate_meta is None:
            # Engine heartbeat: one compared decision.
            if window_start <= timestamp <= anchor:
                heartbeats += 1
            continue

        # Gate envelope. Only SHADOW-mode diff envelopes participate.
        if not isinstance(gate_meta, Mapping):
            malformed_records += 1
            continue
        if gate_meta.get("mode") != "shadow":
            # enforce-mode gate envelopes (incl. fallback) are excluded.
            continue
        diff = gate_meta.get("diff")
        if not isinstance(diff, Mapping):
            malformed_records += 1
            continue

        record, malformed = _classify_diff(timestamp, diff)
        if malformed:
            malformed_records += 1
        if record is None:
            continue
        if not (window_start <= timestamp <= anchor):
            continue

        for category in record.categories:
            mismatch_by_field[category] = mismatch_by_field.get(category, 0) + 1
        if record.new_engine_error is not None:
            new_engine_errors += 1
        else:
            mismatches += 1
        mismatch_records.append(record)

    total_compared = (
        heartbeats if total_compared_override is None else total_compared_override
    )
    agreements = max(0, total_compared - mismatches - new_engine_errors)
    consistency = (
        (agreements / total_compared) if total_compared > 0 else None
    )

    return ConsistencyReport(
        now=anchor,
        window=window,
        total_compared=total_compared,
        agreements=agreements,
        mismatches=mismatches,
        new_engine_errors=new_engine_errors,
        malformed_records=malformed_records,
        consistency=consistency,
        mismatch_by_field=mismatch_by_field,
        mismatch_records=tuple(mismatch_records),
    )


def check_flip_criterion(
    report: ConsistencyReport,
    *,
    threshold: float = DEFAULT_FLIP_THRESHOLD,
    min_sample: int = 100,
) -> FlipCriterion:
    """Evaluate a :class:`ConsistencyReport` against the flip criteria.

    This function does NOT re-derive time; it evaluates the report that
    :func:`analyze_consistency` already produced.

    ``ready`` requires ALL of:

    1. ``consistency`` is not ``None`` (at least one compared decision);
    2. ``total_compared >= min_sample`` (enough evidence);
    3. ``consistency >= threshold`` (inclusive).

    Note (ADJ-2): ``new_engine_errors`` are NOT separately gated; they
    lower ``agreements`` and therefore ``consistency``, so they are
    already covered by the threshold check.

    The ``min_sample`` default is 100 (ADJ-3): a silently-ready verdict
    on a tiny window is exactly the failure mode this module exists to
    prevent. Production callers are still expected to pass an explicit
    floor appropriate to their traffic (e.g. 1000).

    Args:
        report: The report to evaluate.
        threshold: Consistency threshold in ``[0, 1]``; inclusive ``>=``.
        min_sample: Minimum ``total_compared`` floor; must be ``>= 1``.

    Returns:
        A frozen :class:`FlipCriterion`.

    Raises:
        ValueError: If ``threshold`` is outside ``[0, 1]`` or
            ``min_sample < 1``.
    """
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold must be in [0, 1], got {threshold!r}")
    if min_sample < 1:
        raise ValueError(f"min_sample must be >= 1, got {min_sample!r}")

    consistency = report.consistency
    total_compared = report.total_compared
    agreements = report.agreements

    if consistency is None:
        ready = False
        reason = (
            "consistency is None: no compared decisions in window "
            f"(total_compared={total_compared}); cannot evaluate flip"
        )
    elif total_compared < min_sample:
        ready = False
        reason = (
            f"insufficient sample: total_compared {total_compared} < "
            f"min_sample {min_sample} (consistency={consistency:.4f})"
        )
    elif consistency < threshold:
        ready = False
        reason = (
            f"consistency {consistency:.4f} < threshold {threshold:.4f} "
            f"(agreements={agreements}, total_compared={total_compared})"
        )
    else:
        ready = True
        reason = (
            f"consistency {consistency:.4f} >= threshold {threshold:.4f} "
            f"over {total_compared} dual-verdict decisions; legacy-crash "
            "calls are not observable in this stream"
        )

    return FlipCriterion(
        ready=ready,
        consistency=consistency,
        threshold=threshold,
        window=report.window,
        total_compared=total_compared,
        agreements=agreements,
        min_sample=min_sample,
        reason=reason,
    )
