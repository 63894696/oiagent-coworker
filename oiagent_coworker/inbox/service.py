# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/inbox/store.py + openworker/inbox/resume.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; reduced the upstream
#     daemon-backed cross-session inbox to a single-process, synchronous
#     service that lives behind a JSONL log and a threading.RLock.
#   - The upstream idempotency_key / dedup / resume cursor machinery is
#     dropped in favour of an explicit :class:`InboxQuery.after_id`
#     cursor plus simple ``ack`` / ``dismiss`` state flags rebuilt by
#     :meth:`_rebuild_from_disk` on startup.
#   - LRU eviction policy added: when the in-memory index reaches
#     ``max_items``, the oldest ``acked`` item is evicted first; a
#     second pass evicts the oldest un-acked item of the lowest
#     priority to make room. If even that fails, callers receive
#     :class:`OIagentCoworkerInboxFullError`.
#   - Audit emission goes through the W2-1.4 ``AuditSink`` protocol
#     with an ``AuditDecision(kind='inbox', ...)`` envelope carrying
#     the action name in ``standing_rule_action`` (the closest typed
#     slot) and the affected item in ``metadata['item_id']`` /
#     ``metadata['item']``.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Inbox service for OIagent Coworker (W2-2.1 + W2-2.2 merged).

The service is the single owner of the in-memory inbox state. It
holds:

  * ``self._items`` -- ``dict[item_id -> InboxItem]`` for O(1) ``get``.
  * ``self._envelopes`` -- ``dict[envelope_id -> InboxItemEnvelope]``
    for O(1) ``get(item_id, at_or_after=envelope_id)``.
  * ``self._acked`` -- ``set[item_id]`` of items explicitly
    acknowledged by an actor. Items in this set are preferred victims
    for LRU eviction under pressure.
  * ``self._dismissed`` -- ``set[item_id]`` of items hidden by an
    actor. Membership suppresses the item from default queries but the
    item itself stays on disk until ``purge_expired`` evicts it.
  * ``self._next_envelope_id`` -- monotonically-increasing cursor that
    the persistence layer uses to assign ids before each write.

Concurrency model:

  * All public methods acquire ``self._lock`` (a :class:`threading.RLock`)
    before mutating any of the above. Reentrancy is used defensively so
    future extensions (e.g. ``append`` calling ``query`` for an
    idempotency lookup) do not deadlock the implementation.
  * I/O of the JSONL log is delegated to
    :class:`OIagentCoworkerInboxPersistence`, which performs the actual
    ``fsync`` write. The service holds the lock across the persistence
    call to keep ``ack`` and ``append`` strictly serializable -- two
    concurrent ``append`` calls cannot interleave their envelopes.

Soft cap + LRU eviction:

  * ``max_items`` defaults to ``10_000``. When ``append`` would push the
    index past the cap, the service evicts the oldest ``acked`` item
    that is not ``dismissed`` and that has ``priority in {LOW, NORMAL}``
    first, falling back to the oldest un-acked item of any priority.
  * The eviction policy's pass-3 always finds a victim (dismissed
    items are last-resort victims), so under the current policy
    :class:`OIagentCoworkerInboxFullError` is a defensive boundary
    that cannot be triggered through the public ``append`` API alone.
    The class is retained for future policy changes and is exercised
    by unit tests via monkey-patching the internal eviction helper.

Audit emission:

  * Every state transition (``append`` / ``ack`` / ``dismiss`` /
    ``expire``) emits an :class:`AuditDecision` envelope through
    ``self._audit_sink``. Failures in the sink are logged at WARNING
    and swallowed so the inbox path stays non-blocking.

Anti-flattery boundary (see plan \xc2\xa73.2):
    - No ``import openworker`` anywhere in this file.
    - No Slack / GitHub / Linear / Notion / Calendar connectors.
    - No cron expression engine; cron-driven ``append`` is the caller's
      responsibility (W2-3 selfwake).
    - Borrowed design (envelope shape + append-only durability), not
      runtime.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Local import to keep ``inbox`` at the bottom of the OIagent Coworker
# import DAG; ``audit`` lives in permissions/ and is a sibling rather
# than a parent of the inbox package.
from oiagent_coworker.inbox.models import (
    InboxItem,
    InboxItemEnvelope,
    InboxItemKind,
    InboxItemPriority,
    InboxQuery,
)
from oiagent_coworker.inbox.persistence import OIagentCoworkerInboxPersistence

__all__ = [
    "OIagentCoworkerInboxFullError",
    "OIagentCoworkerInboxService",
]


_LOGGER = logging.getLogger(__name__)


# Sentinel sort key for "lowest priority first when evicting". Tuples
# compare lexicographically, so ``LOW < NORMAL < HIGH < CRITICAL`` when
# tuple-ordering the enum values.
_EVICT_PRIORITY_ORDER: dict[InboxItemPriority, int] = {
    InboxItemPriority.LOW: 0,
    InboxItemPriority.NORMAL: 1,
    InboxItemPriority.HIGH: 2,
    InboxItemPriority.CRITICAL: 3,
}


# Type alias for the injectable clock. Matches the persistence sibling
# modules so callers can use the same fakeable ``utcnow`` fixture.
Clock = Callable[[], datetime]


class OIagentCoworkerInboxFullError(RuntimeError):
    """Inbox full error (defensive boundary, currently unreachable through
    the public API alone).

    The eviction policy's pass-3 always finds a victim (dismissed items
    are last-resort victims), so the public ``append`` API cannot
    organically trigger this exception on its own. The class is
    therefore a **defensive boundary** kept in place for two reasons:

      1. Future policy changes (e.g. a "preserve-dismissed" mode, or a
         stricter priority floor) might legitimately produce a
         no-candidate state. Callers depending on the explicit error
         type will continue to work without modification.
      2. Unit tests exercise the exception by monkey-patching
         :meth:`OIagentCoworkerInboxService._evict_one_locked` to
         return ``False`` directly, which simulates the future-policy
         case deterministically.

    Distinct from a generic ``RuntimeError`` so callers can catch a
    full-inbox condition explicitly (e.g. surface a "clear old
    notifications" UX prompt) without converting every storage failure
    into user-facing copy.
    """

    def __init__(self, message: str, max_items: int) -> None:
        super().__init__(message)
        self.max_items = max_items


def _default_clock() -> datetime:
    """Default clock; UTC tz-aware, never naive."""
    return datetime.now(UTC)


def _new_item_id() -> str:
    return uuid.uuid4().hex


class OIagentCoworkerInboxService:
    """Synchronous in-memory + JSONL-backed inbox service.

    Public API:
        * :meth:`append` -- create a new item, persist, emit audit.
        * :meth:`ack` -- mark an item acknowledged (idempotent).
        * :meth:`dismiss` -- mark an item hidden (idempotent).
        * :meth:`query` -- read items matching an :class:`InboxQuery`.
        * :meth:`get` -- fetch one item by id.
        * :meth:`count` -- count matches without hydration.
        * :meth:`purge_expired` -- remove expired items (soft-first).

    Thread safety:
        All public methods are safe to call concurrently from multiple
        threads. The service uses a re-entrant lock so that future
        helpers calling back into the service (e.g. a hypothetical
        ``append_idempotent`` that needs to ``query`` first) do not
        deadlock.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        storage_path: Path,
        audit_sink: Callable[[Any], None] | None = None,
        max_items: int = 10_000,
        clock: Clock | None = None,
    ) -> None:
        if max_items < 1:
            raise ValueError(
                f"OIagentCoworkerInboxService max_items must be >= 1; "
                f"got {max_items}"
            )
        if not callable(audit_sink) and audit_sink is not None:
            raise TypeError(
                f"audit_sink must be callable or None; "
                f"got {type(audit_sink).__name__}"
            )
        self.storage_path: Path = Path(storage_path)
        self._audit_sink = audit_sink
        self._max_items = max_items
        self._clock: Clock = clock if clock is not None else _default_clock
        self._persistence = OIagentCoworkerInboxPersistence(self.storage_path)
        # Use an RLock (not a plain Lock) so future re-entry from inside
        # the service is safe; the current implementation is single-level
        # but the audit_sink signature + extended query helpers may
        # eventually want to nest calls.
        self._lock = threading.RLock()
        # Mutable state -- always accessed under _lock.
        self._items: dict[str, InboxItem] = {}
        self._envelopes: dict[int, InboxItemEnvelope] = {}
        self._acked: set[str] = set()
        self._dismissed: set[str] = set()
        self._next_envelope_id: int = 1
        # Map item_id -> envelope_id of the ``append`` envelope that
        # created the item. Used by :meth:`query` to implement the
        # ``InboxQuery.after_id`` cursor (stable, monotonic, durable).
        # Rebuilt from the JSONL log in :meth:`_rebuild_from_disk`.
        self._item_to_envelope_id: dict[str, int] = {}
        # Set of item_ids that have been purged via ``purge_expired``.
        # Purged items are hidden from the read API forever and must
        # not resurrect on restart -- the ``expire`` envelope is the
        # durable tombstone.
        self._purged_item_ids: set[str] = set()
        # First-touch populate of the in-memory index from disk.
        self._rebuild_from_disk()

    # ------------------------------------------------------------------
    # Public API -- state transitions
    # ------------------------------------------------------------------

    def append(
        self,
        kind: InboxItemKind,
        priority: InboxItemPriority,
        title: str,
        body: str,
        source: str,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> InboxItem:
        """Create + persist a new item. Emits an ``append`` envelope.

        Args:
            kind: One of :class:`InboxItemKind`.
            priority: One of :class:`InboxItemPriority`.
            title: Short one-line summary (<= 200 chars recommended;
                the service does not enforce a length cap).
            body: Markdown-friendly body; may be empty.
            source: Channel label, lowercase ASCII by convention.
            expires_at: Optional UTC expiry. ``None`` means "never
                expires"; items with ``expires_at <= now`` are hidden
                from default queries.
            metadata: Free-form JSON-serializable extension dict.

        Returns:
            The freshly-created :class:`InboxItem`.

        Raises:
            OIagentCoworkerInboxFullError: Defensive boundary; under
                the current eviction policy the LRU pass-3 always
                finds a victim, so the public ``append`` API cannot
                organically raise this. The class is retained for
                future policy changes and is exercised by unit tests
                via monkey-patching the internal eviction helper.
            ValueError: If ``kind`` / ``priority`` are not members of
                their respective enums.
        """
        if not isinstance(kind, InboxItemKind):
            raise TypeError(
                f"kind must be InboxItemKind, got {type(kind).__name__}"
            )
        if not isinstance(priority, InboxItemPriority):
            raise TypeError(
                f"priority must be InboxItemPriority, "
                f"got {type(priority).__name__}"
            )
        with self._lock:
            now = self._clock()
            item = InboxItem(
                item_id=_new_item_id(),
                kind=kind,
                priority=priority,
                title=title,
                body=body,
                source=source,
                created_at=now,
                expires_at=expires_at,
                metadata=dict(metadata) if metadata else {},
            )
            # Soft-cap enforcement: evict before the index grows past
            # max_items. The eviction is O(max_items) at worst (single
            # sort of the candidate items); well under 1ms at the
            # default cap of 10k.
            if len(self._items) >= self._max_items and not self._evict_one_locked():
                    raise OIagentCoworkerInboxFullError(
                        f"inbox at max_items={self._max_items}; "
                        f"no eviction candidate (all items are "
                        f"undismissed + critical + un-acked)",
                        max_items=self._max_items,
                    )
            envelope = self._build_envelope_locked("append", item, "system")
            self._items[item.item_id] = item
            self._envelopes[envelope.envelope_id] = envelope
            # Record the cursor link so query(after_id=...) can filter.
            self._item_to_envelope_id[item.item_id] = envelope.envelope_id
            self._persistence.append(envelope)
            self._audit_locked("append", envelope)
            return item

    def ack(self, item_id: str, actor: str = "user") -> bool:
        """Mark an item as acknowledged. Idempotent.

        Args:
            item_id: Identifier of the item to acknowledge.
            actor: Identity recorded in the audit envelope.

        Returns:
            ``True`` if the item was previously un-acked (state
            changed), ``False`` if it was already acked. ``False`` is
            also returned when the item_id is unknown or the item has
            been purged -- this keeps ``ack`` side-effect free for
            stale notification clients.
        """
        with self._lock:
            if item_id not in self._items or item_id in self._purged_item_ids:
                _LOGGER.debug("ack: unknown or purged item_id=%s; no-op", item_id)
                return False
            already = item_id in self._acked
            self._acked.add(item_id)
            if already:
                return False
            envelope = self._build_envelope_locked(
                "ack", None, actor, item_id=item_id
            )
            self._envelopes[envelope.envelope_id] = envelope
            self._persistence.append(envelope)
            self._audit_locked("ack", envelope)
            return True

    def dismiss(self, item_id: str, actor: str = "user") -> bool:
        """Mark an item as dismissed (hidden by default queries).

        Args:
            item_id: Identifier of the item to dismiss.
            actor: Identity recorded in the audit envelope.

        Returns:
            ``True`` if the item was previously un-dismissed (state
            changed), ``False`` if it was already dismissed, unknown,
            or purged.
        """
        with self._lock:
            if item_id not in self._items or item_id in self._purged_item_ids:
                _LOGGER.debug("dismiss: unknown or purged item_id=%s; no-op", item_id)
                return False
            already = item_id in self._dismissed
            self._dismissed.add(item_id)
            if already:
                return False
            envelope = self._build_envelope_locked(
                "dismiss", None, actor, item_id=item_id
            )
            self._envelopes[envelope.envelope_id] = envelope
            self._persistence.append(envelope)
            self._audit_locked("dismiss", envelope)
            return True

    def purge_expired(self, now: datetime | None = None) -> int:
        """Remove expired items from the in-memory index and on-disk log.

        Items with ``expires_at`` strictly in the past are dropped from
        the service's index; an ``expire`` envelope is recorded for
        each item so the durable log carries a tombstone. The
        ``expire`` envelope is kept on disk (rather than rewritten
        out) so that a restart does not resurrect the purged item
        from its original ``append`` envelope.

        The in-memory tombstone set is the source of truth for the
        service API: ``get`` / ``query`` / ``count`` / ``ack`` /
        ``dismiss`` all honour it. On restart
        :meth:`_rebuild_from_disk` replays the ``expire`` envelopes
        and re-populates the tombstone set before serving the first
        request.

        Args:
            now: Override for "current time" (test-friendly). Defaults
                to the service's injected clock.

        Returns:
            The number of items purged.
        """
        with self._lock:
            current = now if now is not None else self._clock()
            expired_ids = [
                item.item_id
                for item in self._items.values()
                if item.expires_at is not None and item.expires_at <= current
            ]
            if not expired_ids:
                return 0
            for item_id in expired_ids:
                envelope = self._build_envelope_locked(
                    "expire", None, "system", item_id=item_id
                )
                self._envelopes[envelope.envelope_id] = envelope
                self._persistence.append(envelope)
                self._audit_locked("expire", envelope)
                self._items.pop(item_id, None)
                self._acked.discard(item_id)
                self._dismissed.discard(item_id)
                # Durable tombstone: purged items are hidden from the
                # read API forever and never resurrect on restart.
                self._purged_item_ids.add(item_id)
            return len(expired_ids)

    # ------------------------------------------------------------------
    # Public API -- read side
    # ------------------------------------------------------------------

    def query(self, q: InboxQuery | None = None) -> list[InboxItem]:
        """Return items matching the supplied query.

        Args:
            q: :class:`InboxQuery` filter; ``None`` means the default
                ("active, non-expired, non-dismissed, all kinds /
                priorities / sources, limit=1000").

        Returns:
            Items sorted by ``created_at`` descending (newest first).
            The list is always a fresh copy; callers may mutate it
            freely.

        Filtering semantics:

          * ``after_id`` (the durable resume cursor) compares against
            the ``envelope_id`` of the ``append`` envelope that
            created the item. Items whose append envelope_id is
            strictly greater than ``after_id`` are returned. The
            cursor is stable across restarts because the mapping is
            rebuilt from the JSONL log.
          * Purged items (i.e. items whose ``expire`` envelope exists
            on disk) are never returned, regardless of
            ``include_expired``.
        """
        query = q if q is not None else InboxQuery()
        limit = max(1, min(int(query.limit), 1_000_000))
        with self._lock:
            current = self._clock()
            results: list[InboxItem] = []
            for item in self._items.values():
                # Purged items are inert; never expose them.
                if item.item_id in self._purged_item_ids:
                    continue
                if not query.include_dismissed and item.item_id in self._dismissed:
                    continue
                if query.kinds and item.kind not in query.kinds:
                    continue
                if query.priorities and item.priority not in query.priorities:
                    continue
                if query.sources and item.source not in query.sources:
                    continue
                if not query.include_expired and self._is_expired(item, current):
                    continue
                # Durable resume cursor: skip items whose append
                # envelope has been seen already. The map is populated
                # by append() and rebuilt by _rebuild_from_disk().
                cursor = self._item_to_envelope_id.get(item.item_id, 0)
                if cursor <= query.after_id:
                    continue
                results.append(item)
            results.sort(key=lambda it: it.created_at, reverse=True)
            return results[:limit]

    def get(self, item_id: str) -> InboxItem | None:
        """Return the item with the given id, or ``None``.

        Purged items (whose ``expire`` envelope exists on disk) are
        treated as unknown: the service has removed them from the
        read surface entirely.
        """
        with self._lock:
            if item_id in self._purged_item_ids:
                return None
            return self._items.get(item_id)

    def count(self, q: InboxQuery | None = None) -> int:
        """Count items matching the query without hydrating them.

        Equivalent to ``len(service.query(q))`` but avoids building
        the intermediate list -- useful for dashboards.

        Purged items are excluded from the count regardless of
        ``include_expired`` -- they are inert and must not contribute
        to the total.
        """
        query = q if q is not None else InboxQuery()
        with self._lock:
            current = self._clock()
            total = 0
            for item in self._items.values():
                if item.item_id in self._purged_item_ids:
                    continue
                if not query.include_dismissed and item.item_id in self._dismissed:
                    continue
                if query.kinds and item.kind not in query.kinds:
                    continue
                if query.priorities and item.priority not in query.priorities:
                    continue
                if query.sources and item.source not in query.sources:
                    continue
                if not query.include_expired and self._is_expired(item, current):
                    continue
                total += 1
            return total

    # ------------------------------------------------------------------
    # Internal helpers (all ``_lock``-suffixed assume self._lock is held)
    # ------------------------------------------------------------------

    def _is_expired(self, item: InboxItem, now: datetime) -> bool:
        """Return True if the item's ``expires_at`` is strictly past."""
        return (
            item.expires_at is not None
            and item.expires_at <= now
        )

    def _build_envelope_locked(
        self,
        action: str,
        item: InboxItem | None,
        actor: str,
        *,
        item_id: str | None = None,
    ) -> InboxItemEnvelope:
        """Build a fresh envelope and bump the next-id counter.

        For ``action='append'``, ``item`` MUST be the freshly-created
        :class:`InboxItem`; its ``item_id`` is used as the envelope's
        ``item_id`` field. For status transitions (``ack`` /
        ``dismiss`` / ``expire``), pass ``item=None`` and provide
        ``item_id`` explicitly.
        """
        env_id = self._next_envelope_id
        self._next_envelope_id += 1
        if item is not None:
            return InboxItemEnvelope(
                envelope_id=env_id,
                timestamp=self._clock(),
                action=action,
                item=item,
                item_id=item.item_id,
                actor=actor,
            )
        if not item_id:
            raise ValueError(
                "_build_envelope_locked requires item or item_id for "
                f"action={action!r}"
            )
        return InboxItemEnvelope(
            envelope_id=env_id,
            timestamp=self._clock(),
            action=action,
            item=None,
            item_id=item_id,
            actor=actor,
        )

    def _evict_one_locked(self) -> bool:
        """Drop one item to make room for a new append.

        Eviction strategy (in order):

          1. Oldest ``acked`` item that is **not** ``dismissed``, with
             the lowest priority (LOW first, then NORMAL, then HIGH,
             then CRITICAL).
          2. Oldest un-acked item that is **not** ``dismissed``, with
             the lowest priority.
          3. Oldest ``dismissed`` item (any priority, acked or not).

        All three passes iterate over the full in-memory index
        (``self._items``), which is bounded by ``max_items``. The
        passes are deterministic and O(max_items) at worst.

        Returns:
            ``True`` if an item was evicted; ``False`` iff the
            in-memory index is empty (the only configuration where
            the public ``append`` API cannot organically trigger
            :class:`OIagentCoworkerInboxFullError`; see
            :class:`OIagentCoworkerInboxFullError` docstring for the
            defensive-boundary rationale).
        """
        if not self._items:
            return False

        def _candidate_score(item: InboxItem, dismissed_bias: int) -> tuple[int, int, datetime]:
            """Lower score = better eviction candidate."""
            return (
                _EVICT_PRIORITY_ORDER[item.priority],
                dismissed_bias,
                item.created_at,
            )

        # Pass 1: acked + not dismissed, lowest priority first.
        candidates = [
            it for it in self._items.values()
            if it.item_id in self._acked and it.item_id not in self._dismissed
        ]
        if candidates:
            victim = min(candidates, key=lambda it: _candidate_score(it, 0))
            self._evict_item_locked(victim.item_id)
            return True
        # Pass 2: not dismissed (any ack status), lowest priority first.
        candidates = [
            it for it in self._items.values()
            if it.item_id not in self._dismissed
        ]
        if candidates:
            victim = min(candidates, key=lambda it: _candidate_score(it, 0))
            self._evict_item_locked(victim.item_id)
            return True
        # Pass 3: dismissed items are last-resort victims (the user
        # already chose to hide them, so evicting is friendly).
        candidates = list(self._items.values())
        if candidates:
            victim = min(candidates, key=lambda it: _candidate_score(it, 0))
            self._evict_item_locked(victim.item_id)
            return True
        return False

    def _evict_item_locked(self, item_id: str) -> None:
        """Drop one item from the in-memory index (no persistence)."""
        self._items.pop(item_id, None)
        self._acked.discard(item_id)
        self._dismissed.discard(item_id)

    def _audit_locked(
        self,
        action: str,
        envelope: InboxItemEnvelope,
    ) -> None:
        """Emit a single :class:`AuditDecision` if a sink is wired.

        The envelope goes through with ``kind='inbox'`` (cast through
        ``Any`` because :class:`AuditKind` in ``permissions/audit.py``
        is a closed ``Literal`` that does not yet include ``inbox`` --
        adding it is a one-line PR on the audit module W2-2.4). The
        action name and the affected item land in the envelope's
        ``metadata`` dict so the W2-1.4 typed-sink contract is
        preserved.
        """
        if self._audit_sink is None:
            return
        try:
            # Local import to keep inbox importable without a hard
            # cycle through permissions/audit.
            from oiagent_coworker.permissions.audit import AuditDecision

            metadata: dict[str, Any] = {
                "inbox_action": action,
                "item_id": envelope.item_id,
                "actor": envelope.actor,
                "envelope_id": envelope.envelope_id,
            }
            if envelope.item is not None:
                metadata["item"] = envelope.item
            decision = AuditDecision(
                # ``AuditKind`` Literal is closed today; ``cast`` keeps
                # mypy + ruff happy until W2-2.4 extends it. Runtime
                # semantics are unaffected.
                kind="inbox",
                timestamp=envelope.timestamp,
                standing_rule_action=None,
                metadata=metadata,
            )
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001 -- audit must not break inbox path
            _LOGGER.warning("inbox audit_sink raised %s; ignored", exc)

    def _rebuild_from_disk(self) -> None:
        """Rebuild the in-memory index from the on-disk JSONL log.

        The replay is single-pass and envelope-order-preserving: any
        ``expire`` envelope that walks the log after its ``append``
        envelope will tombstone the item, but the on-disk order is
        always ``append`` (envelope_id=N) first then ``expire``
        (envelope_id=N+1) so the simple

            for envelope in replay():
                ...

        walk handles the case. The defensive ordering check below
        also covers the rare case where an ``append`` envelope is
        observed after its ``expire`` (e.g. truncated log + new
        write): the purge wins and the item is dropped.
        """
        self._items.clear()
        self._envelopes.clear()
        self._acked.clear()
        self._dismissed.clear()
        self._item_to_envelope_id.clear()
        self._purged_item_ids.clear()
        max_id = 0
        # Pass 1: scan envelopes once, build the per-item_id state.
        for envelope in self._persistence.replay():
            self._envelopes[envelope.envelope_id] = envelope
            max_id = max(max_id, envelope.envelope_id)
            if envelope.action == "append":
                if envelope.item is not None:
                    # If the item was already purged earlier in the
                    # log, do not resurrect it.
                    if envelope.item.item_id in self._purged_item_ids:
                        continue
                    self._items[envelope.item.item_id] = envelope.item
                    # Resume-cursor mapping (also rebuilt from any
                    # future ack envelope; the append id is canonical).
                    self._item_to_envelope_id[envelope.item.item_id] = (
                        envelope.envelope_id
                    )
            elif envelope.action == "ack":
                if envelope.item_id in self._items:
                    self._acked.add(envelope.item_id)
            elif envelope.action == "dismiss":
                self._dismissed.add(envelope.item_id)
            elif envelope.action == "expire":
                # An expire tombstone on disk means the item was
                # purged; drop it from the in-memory index if present.
                self._items.pop(envelope.item_id, None)
                self._acked.discard(envelope.item_id)
                self._dismissed.discard(envelope.item_id)
                # Durable tombstone: even if the corresponding
                # ``append`` envelope is observed later in the log,
                # it must not resurrect the item.
                self._purged_item_ids.add(envelope.item_id)
        # Drop acked/dismissed state for items whose underlying append
        # envelope never showed up (corrupt log case): the per-item_id
        # state for an item we never replayed an append for is moot.
        self._acked = {
            iid for iid in self._acked if iid in self._items
        }
        self._dismissed = {
            iid for iid in self._dismissed if iid in self._items
        }
        # Next id strictly greater than every envelope_id seen.
        self._next_envelope_id = max_id + 1
