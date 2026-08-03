# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/inbox/__init__.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     broad re-export surface with a curated public API for the five-
#     item inbox surface.
#   - The upstream inbox daemon process + connector surface is dropped
#     entirely; this __init__ only exposes the three OIagent Coworker
#     building blocks: models, persistence, service.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Inbox package for OIagent Coworker (W2-2).

Public API:

    * :class:`InboxItem` -- the inbox unit (frozen dataclass).
    * :class:`InboxItemKind`, :class:`InboxItemPriority` -- enums
      used by the item dataclass and by :class:`InboxQuery`.
    * :class:`InboxItemEnvelope` -- one line in the append-only log.
    * :class:`InboxQuery` -- explicit read-side filter.
    * :class:`OIagentCoworkerInboxService` -- business core; owns the
      in-memory index, the LRU eviction policy, and the audit-sink
      wiring.
    * :class:`OIagentCoworkerInboxPersistence` -- append-only JSONL
      store for envelopes.
    * :class:`OIagentCoworkerInboxFullError` -- defensive boundary
      kept for future policy changes. The current eviction policy's
      pass-3 always finds a victim, so the public ``append`` API
      cannot organically raise this exception; tests exercise it by
      monkey-patching the internal eviction helper.

Anti-flattery boundary (see plan \xc2\xa73.2):
    - No ``import openworker`` anywhere in this package.
    - No Slack / GitHub / Linear / Notion / Calendar connectors.
    - Borrowed design (envelope shape + append-only durability), not
      runtime.
"""

from oiagent_coworker.inbox.models import (
    InboxItem,
    InboxItemEnvelope,
    InboxItemKind,
    InboxItemPriority,
    InboxQuery,
)
from oiagent_coworker.inbox.persistence import OIagentCoworkerInboxPersistence
from oiagent_coworker.inbox.service import (
    OIagentCoworkerInboxFullError,
    OIagentCoworkerInboxService,
)

__all__ = [
    "InboxItem",
    "InboxItemEnvelope",
    "InboxItemKind",
    "InboxItemPriority",
    "InboxQuery",
    "OIagentCoworkerInboxFullError",
    "OIagentCoworkerInboxPersistence",
    "OIagentCoworkerInboxService",
]
