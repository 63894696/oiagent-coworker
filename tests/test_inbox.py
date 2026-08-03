# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/inbox/test_inbox.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Adapted to OIagent Coworker five-kind inbox surface
#     (notification / task / message / webhook / alert).
#   - Tests cover W2-2.1 (models + service) + W2-2.2 (persistence + audit)
#     -- 19 tests total across models, service CRUD, query filters,
#     expire / purge, LRU eviction, persistence replay, and E2E.
#   - No server runtime; pure synchronous service exercised under
#     pytest's tmp_path fixtures for cross-platform safety.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Comprehensive tests for oiagent_coworker.inbox (W2-2.3).

Covers the W2-2.1 (models + service) + W2-2.2 (persistence + audit)
ship surface:

  * Section 1 -- Models / dataclass invariants (3 tests)
  * Section 2 -- Service append / ack / dismiss CRUD (5 tests)
  * Section 3 -- Query filtering (4 tests)
  * Section 4 -- Expire + purge (2 tests)
  * Section 5 -- LRU eviction (2 tests)
  * Section 6 -- Persistence replay (2 tests)
  * Section 7 -- End-to-end integration (1 test)

Total: 19 tests, no external deps beyond pytest.

Anti-flattery boundary (see plan §3.2 / §8.2.1):
    - No ``import openworker`` anywhere in this file.
    - No Slack / GitHub / Linear / Notion / Calendar connector stubs.
    - No cron / MCP / server-runtime code paths.
    - Borrowed design only (test surface + envelope shape), not runtime.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.inbox import (
    InboxItem,
    InboxItemKind,
    InboxItemPriority,
    InboxQuery,
    OIagentCoworkerInboxFullError,
    OIagentCoworkerInboxService,
)
from oiagent_coworker.permissions.audit import AuditDecision

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _CapturedAudit:
    """Captured subset of an AuditDecision for test assertions."""

    kind: str
    inbox_action: str
    item_id: str
    actor: str
    envelope_id: int


@pytest.fixture
def captured_audit() -> list[_CapturedAudit]:
    return []


@pytest.fixture
def audit_sink(captured_audit: list[_CapturedAudit]):
    def sink(decision: AuditDecision) -> None:
        # ``inbox`` audits land in AuditDecision.metadata; the AuditDecision
        # dataclass has no ``envelope`` attribute. Read directly from
        # metadata, which carries inbox_action / item_id / actor /
        # envelope_id.
        inbox_action = decision.metadata.get("inbox_action")
        item_id = decision.metadata.get("item_id")
        actor = decision.metadata.get("actor")
        env_id = decision.metadata.get("envelope_id")
        if inbox_action is None or item_id is None:
            return
        captured_audit.append(
            _CapturedAudit(
                kind=decision.kind,
                inbox_action=inbox_action,
                item_id=item_id,
                actor=actor or "",
                envelope_id=env_id if isinstance(env_id, int) else 0,
            )
        )
    return sink


@pytest.fixture
def service(
    tmp_path: Path,
    audit_sink,
) -> OIagentCoworkerInboxService:
    """Default service: large max_items (10_000), no clock injection.

    Per-service-path isolation: every test gets its own ``tmp_path``
    JSONL file, so tests can run in any order without cross-pollution.
    """
    return OIagentCoworkerInboxService(
        storage_path=tmp_path / "inbox.jsonl",
        audit_sink=audit_sink,
    )


@pytest.fixture
def fixed_clock() -> callable:
    """Pin clock to a deterministic UTC datetime for expiry tests.

    Returns a zero-arg callable returning the fixed datetime; the
    service accepts it via the ``clock=`` constructor parameter.
    """
    fixed = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def service_with_fixed_clock(
    tmp_path: Path,
    audit_sink,
    fixed_clock,
) -> OIagentCoworkerInboxService:
    """Service pre-wired with the fixed clock + audit_sink for expiry tests."""
    return OIagentCoworkerInboxService(
        storage_path=tmp_path / "inbox.jsonl",
        audit_sink=audit_sink,
        clock=fixed_clock,
    )


def _make_item_envelope(
    kind: InboxItemKind = InboxItemKind.NOTIFICATION,
    priority: InboxItemPriority = InboxItemPriority.NORMAL,
    title: str = "test item",
    body: str = "body text",
    source: str = "system",
    expires_at: datetime | None = None,
    metadata: dict | None = None,
) -> InboxItem:
    """Build a bare InboxItem for direct dataclass tests (no service)."""
    now = datetime.now(UTC)
    return InboxItem(
        item_id=uuid.uuid4().hex,
        kind=kind,
        priority=priority,
        title=title,
        body=body,
        source=source,
        created_at=now,
        expires_at=expires_at,
        metadata=dict(metadata) if metadata else {},
    )


# ===========================================================================
# Section 1: Models / dataclass invariants (3 tests)
# ===========================================================================


def test_models_inbox_item_is_frozen() -> None:
    """``InboxItem`` is ``frozen=True``; any field reassignment raises
    ``FrozenInstanceError``. Use ``dataclasses.replace`` to derive a new
    instance instead."""
    item = _make_item_envelope(
        kind=InboxItemKind.NOTIFICATION,
        priority=InboxItemPriority.LOW,
        title="frozen test",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.title = "mutated"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.priority = InboxItemPriority.CRITICAL  # type: ignore[misc]
    # The escape hatch: dataclasses.replace produces a new instance.
    updated = dataclasses.replace(item, title="replaced")
    assert item.title == "frozen test"
    assert updated.title == "replaced"
    assert updated.item_id == item.item_id


def test_models_inbox_item_priority_ordering() -> None:
    """The 4 ``InboxItemPriority`` values are distinct string-enum members
    and sort by declaration order (used by LRU eviction policy)."""
    values = [p.value for p in InboxItemPriority]
    assert values == ["low", "normal", "high", "critical"]
    assert len(set(values)) == 4, "priority enum values must be unique"
    # Each member is a str-Enum; equality is by string value.
    assert InboxItemPriority.LOW != InboxItemPriority.NORMAL
    assert InboxItemPriority.CRITICAL == "critical"
    # Same test for InboxItemKind (5 values, all unique).
    kinds = [k.value for k in InboxItemKind]
    assert kinds == ["notification", "task", "message", "webhook", "alert"]
    assert len(set(kinds)) == 5, "kind enum values must be unique"


def test_models_inbox_query_defaults_are_empty() -> None:
    """``InboxQuery()`` defaults: empty frozensets, limit=1000, after_id=0,
    include_expired=False, include_dismissed=False."""
    q = InboxQuery()
    assert q.kinds == frozenset()
    assert q.priorities == frozenset()
    assert q.sources == frozenset()
    assert q.limit == 1000
    assert q.after_id == 0
    assert q.include_expired is False
    assert q.include_dismissed is False
    # The dataclass is also frozen -- can't mutate in place.
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.limit = 5  # type: ignore[misc]


# ===========================================================================
# Section 2: Service - append / ack / dismiss 核心 CRUD (5 tests)
# ===========================================================================


def test_service_append_returns_item_with_uuid(
    service: OIagentCoworkerInboxService,
) -> None:
    """``append`` returns an ``InboxItem`` whose ``item_id`` is a valid
    uuid4 hex (32 chars) and ``created_at`` is a UTC tz-aware datetime."""
    item = service.append(
        kind=InboxItemKind.NOTIFICATION,
        priority=InboxItemPriority.NORMAL,
        title="hello",
        body="world",
        source="system",
    )
    # item_id is 32-char lowercase hex (uuid4().hex).
    assert isinstance(item.item_id, str)
    assert len(item.item_id) == 32
    # uuid.UUID(hex=...) parses; the canonical string form inserts
    # dashes, so compare the parsed object's ``hex`` attribute, which
    # returns the original 32-char form without dashes.
    parsed = uuid.UUID(hex=item.item_id)
    assert parsed.hex == item.item_id
    # created_at is tz-aware UTC.
    assert item.created_at.tzinfo is not None
    assert item.created_at.utcoffset() == timedelta(0)
    # And the service holds it in its index.
    assert service.get(item.item_id) is item


def test_service_append_emits_inbox_audit_envelope(
    service: OIagentCoworkerInboxService,
    captured_audit: list[_CapturedAudit],
) -> None:
    """``append`` emits exactly one ``AuditDecision(kind='inbox')`` whose
    ``metadata['inbox_action'] == 'append'`` and ``metadata['item_id']``
    matches the returned item."""
    item = service.append(
        kind=InboxItemKind.TASK,
        priority=InboxItemPriority.HIGH,
        title="audit me",
        body="",
        source="cron",
    )
    assert len(captured_audit) == 1
    captured = captured_audit[0]
    assert captured.kind == "inbox"
    assert captured.inbox_action == "append"
    assert captured.item_id == item.item_id
    assert captured.actor == "system"


def test_service_ack_is_idempotent(
    service: OIagentCoworkerInboxService,
    captured_audit: list[_CapturedAudit],
) -> None:
    """Calling ``ack`` on the same item twice returns ``True`` then
    ``False``; only the first call emits an ``'ack'`` audit envelope."""
    item = service.append(
        kind=InboxItemKind.MESSAGE,
        priority=InboxItemPriority.NORMAL,
        title="ack me",
        body="",
        source="slack",
    )
    # The append already emitted one audit; capture-ack is the second.
    assert captured_audit[-1].inbox_action == "append"

    first = service.ack(item.item_id)
    assert first is True
    assert captured_audit[-1].inbox_action == "ack"
    assert captured_audit[-1].item_id == item.item_id

    second = service.ack(item.item_id)
    assert second is False
    # Exactly one 'ack' envelope in captured audits.
    ack_actions = [c for c in captured_audit if c.inbox_action == "ack"]
    assert len(ack_actions) == 1

    # ack on unknown item_id is also a no-op False.
    assert service.ack("nonexistent-uuid") is False


def test_service_dismiss_hides_item_from_default_query(
    service: OIagentCoworkerInboxService,
) -> None:
    """After ``dismiss``, default ``query()`` does not return the item;
    ``query(include_dismissed=True)`` does."""
    item = service.append(
        kind=InboxItemKind.ALERT,
        priority=InboxItemPriority.CRITICAL,
        title="dismiss me",
        body="",
        source="system",
    )
    # Default query includes it.
    assert any(it.item_id == item.item_id for it in service.query())
    # Dismiss and re-query.
    assert service.dismiss(item.item_id) is True
    assert not any(it.item_id == item.item_id for it in service.query())
    # include_dismissed=True brings it back.
    visible = service.query(InboxQuery(include_dismissed=True))
    assert any(it.item_id == item.item_id for it in visible)
    # count() mirrors query()'s default filter.
    assert service.count() == 0
    assert service.count(InboxQuery(include_dismissed=True)) == 1
    # Idempotent: dismissing again returns False.
    assert service.dismiss(item.item_id) is False


def test_service_get_unknown_item_returns_none(
    service: OIagentCoworkerInboxService,
) -> None:
    """``get`` on an unknown id returns ``None`` without raising."""
    # Even an obvious non-uuid4 hex string is tolerated; the service
    # never parses the id, so any string is a valid lookup key.
    assert service.get("not_a_uuid") is None
    assert service.get("") is None
    # A well-formed uuid4 hex that was never appended is also None.
    phantom = uuid.uuid4().hex
    assert service.get(phantom) is None


# ===========================================================================
# Section 3: Query filtering (4 tests)
# ===========================================================================


def test_service_query_filter_by_kind(
    service: OIagentCoworkerInboxService,
) -> None:
    """``query(kinds={K})`` returns only items with ``item.kind == K``."""
    # Append one of each kind.
    for k in InboxItemKind:
        service.append(
            kind=k,
            priority=InboxItemPriority.NORMAL,
            title=f"{k.value} item",
            body="",
            source="system",
        )
    # All 5 visible by default.
    assert service.count() == 5
    # Single-kind filter returns exactly 1.
    only_alerts = service.query(
        InboxQuery(kinds=frozenset({InboxItemKind.ALERT}))
    )
    assert len(only_alerts) == 1
    assert only_alerts[0].kind == InboxItemKind.ALERT
    # Multi-kind filter returns matching subset.
    two_kinds = service.query(
        InboxQuery(
            kinds=frozenset({InboxItemKind.MESSAGE, InboxItemKind.WEBHOOK})
        )
    )
    assert len(two_kinds) == 2
    assert {it.kind for it in two_kinds} == {
        InboxItemKind.MESSAGE,
        InboxItemKind.WEBHOOK,
    }


def test_service_query_filter_by_priority_and_source(
    service: OIagentCoworkerInboxService,
) -> None:
    """``query`` AND-combines ``priorities`` and ``sources`` filters."""
    # Mix of priorities + sources. Two HIGH (one slack, one github);
    # two LOW (one slack, one github).
    service.append(InboxItemKind.NOTIFICATION, InboxItemPriority.HIGH,
                   "n-high-slack", "", "slack")
    service.append(InboxItemKind.NOTIFICATION, InboxItemPriority.LOW,
                   "n-low-github", "", "github")
    service.append(InboxItemKind.TASK, InboxItemPriority.HIGH,
                   "t-high-slack", "", "slack")
    service.append(InboxItemKind.TASK, InboxItemPriority.LOW,
                   "t-low-github", "", "github")
    # Priority filter alone (HIGH) -> 2 items.
    highs = service.query(
        InboxQuery(priorities=frozenset({InboxItemPriority.HIGH}))
    )
    assert len(highs) == 2
    assert {it.priority for it in highs} == {InboxItemPriority.HIGH}
    # Source filter alone (slack) -> 2 items.
    slack = service.query(InboxQuery(sources=frozenset({"slack"})))
    assert len(slack) == 2
    assert {it.source for it in slack} == {"slack"}
    # Combined: HIGH + slack -> 2 items (n-high-slack, t-high-slack).
    combo = service.query(
        InboxQuery(
            priorities=frozenset({InboxItemPriority.HIGH}),
            sources=frozenset({"slack"}),
        )
    )
    assert len(combo) == 2
    assert {it.priority for it in combo} == {InboxItemPriority.HIGH}
    assert {it.source for it in combo} == {"slack"}


def test_service_query_limit_enforced(
    service: OIagentCoworkerInboxService,
) -> None:
    """``query(limit=N)`` returns at most ``N`` items, newest first by
    ``created_at`` descending."""
    # Append 10 items; each has a monotonic created_at because the
    # service uses the wall clock per append. Tiny sleeps are avoided by
    # relying on datetime.now(UTC) which has microsecond resolution on
    # most platforms; ties (rare) still sort deterministically because
    # ``created_at`` is unique enough for the test to pass.
    appended = []
    for i in range(10):
        it = service.append(
            kind=InboxItemKind.NOTIFICATION,
            priority=InboxItemPriority.NORMAL,
            title=f"item-{i}",
            body="",
            source="system",
        )
        appended.append(it)
    # limit=3 -> exactly 3 items.
    capped = service.query(InboxQuery(limit=3))
    assert len(capped) == 3
    # Newest-first ordering: the last 3 appended are at the front.
    expected_titles = {f"item-{i}" for i in (7, 8, 9)}
    assert {it.title for it in capped} == expected_titles


def test_service_query_after_id_cursor_real(
    service: OIagentCoworkerInboxService,
    captured_audit: list[_CapturedAudit],
) -> None:
    """``InboxQuery.after_id`` is a real resume cursor: ``query(after_id=N)``
    returns only items whose append ``envelope_id`` is strictly greater
    than ``N``.

    The mapping ``item_id -> append_envelope_id`` is durable across
    restarts (rebuilt from the JSONL log in ``_rebuild_from_disk``), so
    the cursor is stable across sessions.
    """
    items = []
    for i in range(5):
        items.append(
            service.append(
                kind=InboxItemKind.NOTIFICATION,
                priority=InboxItemPriority.NORMAL,
                title=f"cur-{i}",
                body="",
                source="system",
            )
        )
    # The 5 captured append envelopes carry envelope_id 1..5 in order.
    append_envs = [c for c in captured_audit if c.inbox_action == "append"]
    assert [c.envelope_id for c in append_envs] == [1, 2, 3, 4, 5]
    # InboxQuery.after_id is a frozen dataclass field; it round-trips.
    q = InboxQuery(after_id=2)
    assert q.after_id == 2
    q2 = dataclasses.replace(q, after_id=99)
    assert q2.after_id == 99
    assert q.after_id == 2
    # after_id=N returns items whose append envelope_id > N. Default
    # ordering is created_at desc; the 3 surviving items are cur-4,
    # cur-3, cur-2 (envelope_ids 5, 4, 3).
    cursor = service.query(InboxQuery(after_id=2))
    assert len(cursor) == 3
    assert {it.item_id for it in cursor} == {
        items[2].item_id, items[3].item_id, items[4].item_id,
    }
    # after_id=3 returns items with envelope_id > 3 = items 3, 4.
    cursor3 = service.query(InboxQuery(after_id=3))
    assert len(cursor3) == 2
    assert {it.item_id for it in cursor3} == {
        items[3].item_id, items[4].item_id,
    }
    # after_id=0 returns the full 5-item set (default behaviour).
    full = service.query(InboxQuery(after_id=0))
    assert {it.item_id for it in full} == {it.item_id for it in items}
    # after_id=5 returns nothing (cursor past the last envelope).
    past_tail = service.query(InboxQuery(after_id=5))
    assert past_tail == []
    # The cursor survives a restart: rebuild the service on the same
    # JSONL file and confirm the same filter still works.
    # ``service`` already wrote the file; rebuild via a fresh instance
    # pointing at the same storage_path.
    svc2 = OIagentCoworkerInboxService(
        storage_path=service.storage_path,
    )
    after_restart = svc2.query(InboxQuery(after_id=2))
    assert {it.item_id for it in after_restart} == {
        items[2].item_id, items[3].item_id, items[4].item_id,
    }


# ===========================================================================
# Section 4: Expire + purge (2 tests)
# ===========================================================================


def test_service_expired_item_excluded_from_default_query(
    service_with_fixed_clock: OIagentCoworkerInboxService,
) -> None:
    """An item with ``expires_at`` strictly in the past is hidden from
    ``query()`` but visible via ``query(include_expired=True)``."""
    svc = service_with_fixed_clock
    fixed_now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    # Active item: expires 1 hour in the future.
    svc.append(
        kind=InboxItemKind.NOTIFICATION,
        priority=InboxItemPriority.NORMAL,
        title="active",
        body="",
        source="system",
        expires_at=fixed_now + timedelta(hours=1),
    )
    # Expired item: expires 1 hour ago.
    expired = svc.append(
        kind=InboxItemKind.ALERT,
        priority=InboxItemPriority.CRITICAL,
        title="expired",
        body="",
        source="system",
        expires_at=fixed_now - timedelta(hours=1),
    )
    # Default query hides expired.
    visible = svc.query()
    titles = {it.title for it in visible}
    assert "active" in titles
    assert "expired" not in titles
    # include_expired=True brings it back.
    everything = svc.query(InboxQuery(include_expired=True))
    everything_titles = {it.title for it in everything}
    assert {"active", "expired"} <= everything_titles
    # count() mirrors query()'s default filter.
    assert svc.count() == 1
    # The item is still in the index (just hidden), get() returns it.
    assert svc.get(expired.item_id) is not None


def test_service_purge_expired_returns_count_and_emits_audit(
    tmp_path: Path,
    audit_sink,
    fixed_clock,
) -> None:
    """``purge_expired`` returns the count purged, emits one ``'expire'``
    audit envelope per removed item, AND the durable ``expire``
    tombstone prevents the purged item from resurrecting on restart.

    Issue 2 (W2-2.4): the previous ship was fake-green because the
    expire envelope was rewritten out of the JSONL log, so the
    ``append`` envelope that created the item rebuilt the item at
    startup. Fix: keep the ``expire`` envelope on disk and population
    an in-memory tombstone set ``_purged_item_ids`` on every code
    path that reads the index.
    """
    path = tmp_path / "inbox.jsonl"
    svc1 = OIagentCoworkerInboxService(
        storage_path=path,
        audit_sink=audit_sink,
        clock=fixed_clock,
    )
    fixed_now = fixed_clock()
    expired_items = []
    # 3 expired items.
    for i in range(3):
        expired_items.append(
            svc1.append(
                kind=InboxItemKind.NOTIFICATION,
                priority=InboxItemPriority.LOW,
                title=f"exp-{i}",
                body="",
                source="system",
                expires_at=fixed_now - timedelta(hours=1),
            )
        )
    # 2 active items.
    for i in range(2):
        svc1.append(
            kind=InboxItemKind.NOTIFICATION,
            priority=InboxItemPriority.NORMAL,
            title=f"act-{i}",
            body="",
            source="system",
            expires_at=fixed_now + timedelta(hours=1),
        )

    purged = svc1.purge_expired()
    assert purged == 3

    # In-memory state after purge: 3 expired gone, 2 active survive.
    assert svc1.count() == 2
    active_titles = {it.title for it in svc1.query()}
    assert active_titles == {"act-0", "act-1"}
    for item in expired_items:
        assert svc1.get(item.item_id) is None

    # A second purge with no expired items returns 0.
    assert svc1.purge_expired() == 0

    # Restart on the same path: the durable ``expire`` envelope
    # reconstructs the tombstone set, so the expired items DO NOT
    # resurrect. This is the new behaviour locked in by W2-2.4.
    svc2 = OIagentCoworkerInboxService(
        storage_path=path,
        audit_sink=audit_sink,
        clock=fixed_clock,
    )
    assert svc2.count() == 2
    for item in expired_items:
        assert svc2.get(item.item_id) is None
    # ``ack`` / ``dismiss`` on a purged item are no-ops (False).
    for item in expired_items:
        assert svc2.ack(item.item_id) is False
        assert svc2.dismiss(item.item_id) is False


# ===========================================================================
# Section 5: LRU eviction (2 tests)
# ===========================================================================


def test_service_lru_eviction_acked_first(
    tmp_path: Path,
    audit_sink,
) -> None:
    """With ``max_items=3``, after appending 5 items and acking all of
    them, the in-memory index holds only the 3 newest acked items.

    Per the eviction policy: Pass 1 picks the oldest acked + not-dismissed
    item; since all 5 are acked, the 2 oldest are evicted in order.
    """
    svc = OIagentCoworkerInboxService(
        storage_path=tmp_path / "inbox.jsonl",
        audit_sink=audit_sink,
        max_items=3,
    )
    appended = []
    for i in range(5):
        it = svc.append(
            kind=InboxItemKind.NOTIFICATION,
            priority=InboxItemPriority.LOW,
            title=f"lru-{i}",
            body="",
            source="system",
        )
        appended.append(it)
        svc.ack(it.item_id)  # ack every one so they're eviction-eligible
    # In-memory index holds at most 3.
    assert svc.count() == 3
    # The 2 oldest are evicted; the 3 newest survive.
    surviving_ids = {it.item_id for it in appended[-3:]}
    assert svc.get(appended[0].item_id) is None
    assert svc.get(appended[1].item_id) is None
    assert svc.get(appended[2].item_id) is not None
    assert svc.get(appended[3].item_id) is not None
    assert svc.get(appended[4].item_id) is not None
    # And the surviving set matches the 3 newest appended.
    assert surviving_ids == {
        svc.get(it.item_id).item_id  # type: ignore[union-attr]
        for it in appended[-3:]
    }


def test_service_inbox_full_error_via_mocked_eviction(
    tmp_path: Path,
    audit_sink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue 3 (W2-2.4): ``OIagentCoworkerInboxFullError`` is a defensive
    boundary that the public ``append`` API cannot organically trigger.

    The eviction policy's pass-3 always finds a victim (dismissed items
    are last-resort victims), so the only way to raise the exception in
    a test is to monkey-patch the internal eviction helper to return
    ``False`` directly. This simulates the future-policy case (e.g. a
    "preserve-dismissed" mode) where the policy might legitimately
    produce a no-candidate state. See
    :class:`OIagentCoworkerInboxFullError` docstring for the full
    rationale.
    """
    svc = OIagentCoworkerInboxService(
        storage_path=tmp_path / "inbox.jsonl",
        audit_sink=audit_sink,
        max_items=2,
    )
    # First 2 appends succeed; both un-acked.
    svc.append(
        kind=InboxItemKind.ALERT,
        priority=InboxItemPriority.HIGH,
        title="alert-1",
        body="",
        source="system",
    )
    svc.append(
        kind=InboxItemKind.ALERT,
        priority=InboxItemPriority.HIGH,
        title="alert-2",
        body="",
        source="system",
    )
    # Force the eviction policy to fail so the next append raises.
    # The class-level monkey-patch is the documented escape hatch
    # (see :class:`OIagentCoworkerInboxFullError` docstring).
    monkeypatch.setattr(
        OIagentCoworkerInboxService,
        "_evict_one_locked",
        lambda self: False,
    )
    with pytest.raises(OIagentCoworkerInboxFullError) as excinfo:
        svc.append(
            kind=InboxItemKind.ALERT,
            priority=InboxItemPriority.HIGH,
            title="alert-3",
            body="",
            source="system",
        )
    assert excinfo.value.max_items == 2
    # In-memory state is unchanged: still 2 items.
    assert svc.count() == 2


# ===========================================================================
# Section 6: Persistence (2 tests)
# ===========================================================================


def test_persistence_replay_after_restart_returns_state(
    tmp_path: Path,
    audit_sink,
) -> None:
    """Append + ack + dismiss across one service instance; constructing
    a fresh service on the same JSONL path rebuilds equivalent state."""
    path = tmp_path / "inbox.jsonl"
    svc1 = OIagentCoworkerInboxService(
        storage_path=path,
        audit_sink=audit_sink,
    )
    items = []
    for i in range(5):
        items.append(
            svc1.append(
                kind=InboxItemKind.NOTIFICATION,
                priority=InboxItemPriority.NORMAL,
                title=f"persist-{i}",
                body="",
                source="system",
            )
        )
    # Ack items 0, 1; dismiss item 2.
    svc1.ack(items[0].item_id)
    svc1.ack(items[1].item_id)
    svc1.dismiss(items[2].item_id)
    snapshot_before_titles = sorted(
        it.title for it in svc1.query(InboxQuery(include_dismissed=True))
    )
    assert svc1.count() == 4  # 5 total - 1 dismissed
    assert svc1.count(InboxQuery(include_dismissed=True)) == 5
    # Restart: build a fresh service on the same file.
    svc2 = OIagentCoworkerInboxService(
        storage_path=path,
        audit_sink=audit_sink,
    )
    # Same observable state after replay: 4 active, 5 including dismissed,
    # and the same set of titles survives.
    assert svc2.count() == 4
    assert svc2.count(InboxQuery(include_dismissed=True)) == 5
    snapshot_after_titles = sorted(
        it.title for it in svc2.query(InboxQuery(include_dismissed=True))
    )
    assert snapshot_before_titles == snapshot_after_titles
    # Default query on svc2 hides dismissed.
    visible = svc2.query()
    assert items[2].item_id not in {it.item_id for it in visible}
    assert {items[0].item_id, items[1].item_id} <= {
        it.item_id for it in visible
    }
    # Ack set survives the restart: ack the already-acked items again
    # and verify it returns False (proves the in-memory acked set was
    # rebuilt from the JSONL).
    assert svc2.ack(items[0].item_id) is False
    assert svc2.ack(items[1].item_id) is False
    # dismiss the already-dismissed item again: also returns False.
    assert svc2.dismiss(items[2].item_id) is False


def test_persistence_corruption_skip_with_warning(
    tmp_path: Path,
    audit_sink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hand-written corrupt line in the JSONL is skipped at replay-
    time with a WARNING; surrounding valid lines still load."""
    import logging

    path = tmp_path / "inbox.jsonl"
    # Pre-populate the file: 5 valid lines + 1 trailing corrupt line.
    valid_lines = []
    for i in range(5):
        item_id = uuid.uuid4().hex
        envelope = {
            "envelope_id": i + 1,
            "timestamp": datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC).isoformat(),
            "action": "append",
            "item_id": item_id,
            "actor": "system",
            "item": {
                "item_id": item_id,
                "kind": "notification",
                "priority": "normal",
                "title": f"line-{i}",
                "body": "",
                "source": "system",
                "created_at": datetime(
                    2026, 8, 2, 12, 0, 0, tzinfo=UTC
                ).isoformat(),
                "expires_at": None,
                "metadata": {},
            },
        }
        valid_lines.append(json.dumps(envelope, ensure_ascii=False))
    corrupt_line = "NOT JSON{ this is broken"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(valid_lines + [corrupt_line]) + "\n",
        encoding="utf-8",
    )

    # Replay via a fresh service; capture warnings on the inbox logger.
    with caplog.at_level(
        logging.WARNING, logger="oiagent_coworker.inbox.persistence"
    ):
        svc = OIagentCoworkerInboxService(
            storage_path=path,
            audit_sink=audit_sink,
        )

    # 5 valid appends survived; the corrupt trailing line was skipped.
    assert svc.count() == 5
    titles = sorted(it.title for it in svc.query(InboxQuery(include_dismissed=True)))
    assert titles == ["line-0", "line-1", "line-2", "line-3", "line-4"]
    # The corruption warning was emitted.
    warning_records = [
        r for r in caplog.records if "corrupt" in r.getMessage().lower()
    ]
    assert len(warning_records) >= 1


# ===========================================================================
# Section 7: Integration / 端到端 (1 test)
# ===========================================================================


def test_end_to_end_full_pipeline(
    tmp_path: Path,
    audit_sink,
) -> None:
    """End-to-end: append(5) -> ack(2) -> dismiss(1) -> purge_expired(1)
    -> query -> count -> restart -> state consistent; audit pipeline
    receives 9 envelopes (5 append + 2 ack + 1 dismiss + 1 expire).
    """
    path = tmp_path / "inbox.jsonl"
    captured: list[AuditDecision] = []

    def _capturing_sink(decision: AuditDecision) -> None:
        captured.append(decision)

    svc = OIagentCoworkerInboxService(
        storage_path=path,
        audit_sink=_capturing_sink,
    )
    fixed_now = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

    # 5 appends: 4 long-lived + 1 pre-expired (expires 1h before now).
    items = []
    for i in range(4):
        items.append(
            svc.append(
                kind=InboxItemKind.NOTIFICATION,
                priority=InboxItemPriority.NORMAL,
                title=f"e2e-{i}",
                body="",
                source="system",
                expires_at=fixed_now + timedelta(hours=1),
            )
        )
    expired_item = svc.append(
        kind=InboxItemKind.ALERT,
        priority=InboxItemPriority.HIGH,
        title="e2e-expired",
        body="",
        source="system",
        expires_at=fixed_now - timedelta(hours=1),
    )

    # Ack items 0 and 1.
    assert svc.ack(items[0].item_id) is True
    assert svc.ack(items[1].item_id) is True

    # Dismiss item 2.
    assert svc.dismiss(items[2].item_id) is True

    # At this point: 5 visible (include_dismissed), 4 active (default)
# because item 2 is dismissed and the expired item is hidden by the
# service's wall-clock-based _is_expired check (its expires_at is
# fixed_now - 1h which is in the past).
    assert svc.count() == 4
    assert svc.count(InboxQuery(include_dismissed=True)) == 5

    # purge_expired uses a fixed clock: since the service is built on
    # the real wall clock, we use the now= override parameter for a
    # deterministic result.
    purged = svc.purge_expired(now=fixed_now)
    assert purged == 1

    # Post-purge: 3 items (the expired one is gone from the index,
    # item 2 still dismissed).
    assert svc.count() == 3
    assert svc.get(expired_item.item_id) is None

    # Audit fan-out: exactly 5 append + 2 ack + 1 dismiss + 1 expire = 9.
    by_action: dict[str, int] = {}
    for decision in captured:
        action = decision.metadata.get("inbox_action", "?")
        by_action[action] = by_action.get(action, 0) + 1
    assert by_action == {"append": 5, "ack": 2, "dismiss": 1, "expire": 1}
    assert sum(by_action.values()) == 9
    # All 9 are kind='inbox'.
    assert all(d.kind == "inbox" for d in captured)

    # Restart on the same path; state is preserved.
    svc2 = OIagentCoworkerInboxService(
        storage_path=path,
        audit_sink=audit_sink,
    )
    # Default query after restart: 3 visible (item 2 dismissed + the
    # expired item is tombstoned by the durable ``expire`` envelope
    # and does NOT resurrect). Issue 2 (W2-2.4) fix: the previous
    # ship was fake-green because the on-disk rewrite stripped the
    # ``expire`` envelope, so the ``append`` envelope rebuilt the
    # item at startup. The new code keeps the ``expire`` envelope
    # on disk and rebuilds an in-memory tombstone set on startup.
    assert svc2.count() == 3
    # include_dismissed -> 4 (one dismissed, the expired item is
    # excluded even with include_expired because it is purged).
    assert svc2.count(InboxQuery(include_dismissed=True)) == 4
    # The expired item is still gone after restart.
    assert svc2.get(expired_item.item_id) is None
    # Acks / dismisses on the expired item are no-ops.
    assert svc2.ack(expired_item.item_id) is False
    assert svc2.dismiss(expired_item.item_id) is False
    # Acked set is rebuilt for items 0, 1.
    visible = svc2.query()
    visible_ids = {it.item_id for it in visible}
    assert {items[0].item_id, items[1].item_id} <= visible_ids
    assert items[2].item_id not in visible_ids
    # The dismissed item survives the restart: re-dismiss returns False.
    assert svc2.dismiss(items[2].item_id) is False
    # Re-acking items 0, 1 returns False too.
    assert svc2.ack(items[0].item_id) is False
    assert svc2.ack(items[1].item_id) is False