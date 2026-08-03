# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/inbox/items.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; reduced upstream
#     five-item taxonomy to the OIagent Coworker five-kind inbox surface
#     (notification / task / message / webhook / alert) and dropped the
#     upstream CrossSessionItem / IdempotentItem / DurableResumeItem /
#     Ack / Receipt shaping in favour of one minimal frozen dataclass
#     set plus a single append-only envelope for status transitions.
#   - Replaced the upstream idempotency_key / sequence / resume cursor
#     concerns with a per-envelope monotonically increasing envelope_id
#     that doubles as the resume cursor; idempotency is intentionally
#     out of scope for this milestone (see extraction-plan §3.2).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Inbox data model for OIagent Coworker (W2-2.1).

This module defines the OIagent-facing inbox surface as five frozen
dataclasses:

    * :class:`InboxItemKind` -- five-kind string Enum that discriminates
      between notifications (UI toast / tray), tasks (action-required
      follow-ups), messages (chat payloads), webhook callbacks and
      alerts (imminent-attention items). The taxonomy is intentionally
      flat: there is exactly one discriminator per item, and downstream
      routing logic (system tray, IM bridges, audit pipelines) decides
      how each kind surfaces to the desktop companion.

    * :class:`InboxItemPriority` -- four-tier string Enum ("low",
      "normal", "high", "critical"). Used by the read-side query to
      filter and by the service's LRU eviction when the inbox is full.

    * :class:`InboxItem` -- the unit of inbox state. One item carries a
      uuid4 hex identifier, a kind + priority, a short title, a
      free-form Markdown-friendly body, a source channel label, the
      UTC creation timestamp, an optional expiry (``None`` means
      "never expires"), and an open-ended ``metadata`` dict for
      per-source extension keys (Slack thread id, GitHub PR number,
      webhook delivery id, etc.).

    * :class:`InboxItemEnvelope` -- one line in the append-only inbox
      log. Each envelope carries the action that produced it
      (``append`` / ``ack`` / ``dismiss`` / ``expire``), the item id it
      acts on, the actor identity and a reference back to the original
      item payload for ``action='append'`` envelopes. The
      monotonically-increasing ``envelope_id`` is the durable resume
      cursor.

    * :class:`InboxQuery` -- explicit read-side filter object. Using
      explicit named fields (instead of ``**kwargs``) lets us type-check
      query construction in callers and gives the persistence layer a
      stable contract for serializing queries into the durable cursor.

Anti-flattery boundary (see plan \xc2\xa73.2 / \xc2\xa78.2.1):
    - No ``import openworker`` anywhere in this file.
    - No upstream inbox daemon process / Slack / GitHub / Linear / Notion
      / Calendar connector references.
    - Borrowed design only (dataclass shape + envelope), not runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = [
    "InboxItem",
    "InboxItemEnvelope",
    "InboxItemKind",
    "InboxItemPriority",
    "InboxQuery",
]


class InboxItemKind(str, Enum):
    """Five-kind inbox item discriminator.

    Attributes:
        NOTIFICATION: Generic notification surfaced via system tray /
            desktop toast. No mandatory follow-up.
        TASK: Action-required follow-up. The downstream UX layer is
            expected to render these in a "todo" panel.
        MESSAGE: Chat-style payload (DM, mention, etc.). Carries the
            channel in ``metadata['channel']`` for routing.
        WEBHOOK: Webhook callback from an upstream system (CI, billing,
            vendor API). Carries the delivery id in
            ``metadata['delivery_id']`` for de-duplication.
        ALERT: Imminent-attention item. Critical / high-priority ALERT
            items are surfaced before any other kind.
    """

    NOTIFICATION = "notification"
    TASK = "task"
    MESSAGE = "message"
    WEBHOOK = "webhook"
    ALERT = "alert"


class InboxItemPriority(str, Enum):
    """Four-tier priority ordering.

    Used by the read-side query to filter, by the service's LRU eviction
    to pick the next victim when ``max_items`` is reached, and by
    downstream UX to pick the loudest notification channel for a given
    item (toast vs. modal vs. tray + sound).
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class InboxItem:
    """A single inbox unit.

    Attributes:
        item_id: Globally-unique identifier (uuid4 hex). Used as the
            primary key by the service's in-memory index and by every
            :class:`InboxItemEnvelope` that references this item.
        kind: One of :class:`InboxItemKind`. Drives the UX surface and
            the default eviction behaviour.
        priority: One of :class:`InboxItemPriority`. Drives the LRU
            ordering when the inbox hits its soft cap.
        title: Short (<= 200 chars) one-line summary. Suitable for
            tray titles and "toast" headers.
        body: Full Markdown-friendly body. May be empty for items that
            only carry structured ``metadata``.
        source: Channel label (``"slack"``, ``"github"``,
            ``"webhook"``, ``"system"``, ...). Always lowercase ASCII
            per OIagent convention; the service does not normalize.
        created_at: UTC datetime when the item was first appended.
        expires_at: Optional UTC datetime; ``None`` means "never
            expires". The service's ``purge_expired`` is a no-op for
            items with ``expires_at is None``.
        metadata: Free-form JSON-serializable extension dict. Keys are
            sourced-defined; the service MUST NOT mutate this map after
            construction.
    """

    item_id: str
    kind: InboxItemKind
    priority: InboxItemPriority
    title: str
    body: str
    source: str
    created_at: datetime
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InboxItemEnvelope:
    """One line in the append-only inbox log.

    Each transition (``append`` / ``ack`` / ``dismiss`` / ``expire``)
    on an item produces exactly one envelope. ``envelope_id`` is a
    monotonically-increasing counter assigned at write-time by the
    service; it is the durable resume cursor and the last-id stamp the
    service exposes via ``last_envelope_id()``.

    Attributes:
        envelope_id: Sequence id assigned by the service; starts at 1
            for the very first append of a fresh inbox file.
        timestamp: UTC datetime when the envelope was constructed.
        action: ``"append"`` / ``"ack"`` / ``"dismiss"`` / ``"expire"``.
            Other strings are tolerated but treated as opaque by the
            service.
        item: For ``action='append'``, the freshly-created item. For
            status transitions this is ``None`` and the corresponding
            item is reconstructed from the latest append envelope at
            replay-time.
        item_id: Identifier of the item the envelope acts on. Matches
            ``item.item_id`` for ``action='append'`` envelopes.
        actor: Identity that triggered the envelope (``"user"``,
            ``"system"``, ``"cron"``, ``"slack_bridge"``, ...).
    """

    envelope_id: int
    timestamp: datetime
    action: str
    item: InboxItem | None
    item_id: str
    actor: str


@dataclass(frozen=True)
class InboxQuery:
    """Explicit read-side query filter.

    Empty / unset fields mean "no constraint on this dimension". Using a
    dataclass instead of ``**kwargs`` lets callers compose queries
    programmatically and lets the service layer normalize defaults
    (e.g. ``limit=1000``) in one place.

    Attributes:
        kinds: Filter by :class:`InboxItemKind`. Empty = all kinds.
        priorities: Filter by :class:`InboxItemPriority`. Empty = all
            priorities.
        sources: Filter by ``InboxItem.source``. Empty = all sources.
        include_expired: If ``False`` (default), items with
            ``expires_at`` strictly in the past are hidden from the
            query results. They are still on disk until
            ``purge_expired`` evicts them.
        include_dismissed: If ``False`` (default), ``dismiss``-stamped
            items are excluded. ``ack``-stamped items remain visible;
            they are not hidden by default.
        limit: Maximum number of items returned. ``1000`` by default;
            the service clamps to ``[1, 1_000_000]``.
        after_id: Resume cursor; only items whose ``envelope_id`` of the
            append action is strictly greater than ``after_id`` are
            returned. The default ``0`` returns the entire window.
    """

    kinds: frozenset[InboxItemKind] = field(default_factory=frozenset)
    priorities: frozenset[InboxItemPriority] = field(default_factory=frozenset)
    sources: frozenset[str] = field(default_factory=frozenset)
    include_expired: bool = False
    include_dismissed: bool = False
    limit: int = 1000
    after_id: int = 0
