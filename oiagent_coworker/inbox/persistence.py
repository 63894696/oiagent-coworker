# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/inbox/store.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced the
#     upstream SQLite backend with an append-only JSONL store to keep
#     single-file crash-safety and a clean write/read concurrency
#     model that is independent of the OIagent daemon's own SQLite
#     usage under ${OIAGENT_VAULT}.
#   - The envelope_id is now assigned at write-time by the service and
#     persisted as part of every line; the upstream sequence / cursor
#     bookkeeping is dropped in favour of the simpler
#     ``last_envelope_id()`` tail-scan.
#   - Replay tolerates malformed lines (warning log + skip) so a
#     previously-known corruption does not brick the inbox.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Append-only JSONL store for :class:`InboxItemEnvelope` (W2-2.2).

The persistence layer mirrors the W2-1.3 standing-rule store: one file,
one JSON object per line, ``append`` is a single ``write + fsync``, and
``replay`` rebuilds the in-memory envelope index on first read.

JSONL contract
--------------

Each line is::

    {
      "envelope_id":  <int>,
      "timestamp":    "<iso 8601 UTC>",
      "action":       "append" | "ack" | "dismiss" | "expire" | <other>,
      "item_id":      "<uuid4 hex>",
      "actor":        "<identity>",
      "item":         <InboxItem as dict> | null
    }

The ``item`` field is a literal :class:`InboxItem`-shaped dict (i.e.
``dataclasses.asdict(item)`` with ``InboxItem``'s ``metadata`` already
JSON-serializable). ``datetime`` values are serialized via the shared
``_json_default`` hook so they round-trip as ISO 8601 strings.

Persistence semantics:

  * ``append()`` is O(1) amortized: ``open(append)`` + ``write`` +
    ``fsync`` + ``close``. Multiple processes may write concurrently
    *to the same file* at the OS level; the JSONL parser is tolerant
    of torn lines because each line carries a parseable envelope on
    its own.
  * ``replay()`` walks the file once, skipping lines that fail
    ``json.loads`` or that are missing the required ``envelope_id`` /
    ``action`` / ``item_id`` keys. Skipped lines are logged at WARNING
    and do not block subsequent envelopes from loading.
  * ``last_envelope_id()`` scans the file tail for the highest
    ``envelope_id`` without loading every envelope into memory. This
    is used by the service to assign the next id before each write.
  * Missing file is treated as an empty log; constructors do **not**
    create the file. The first ``append`` creates both the parent
    directory and the log file.

Anti-flattery boundary (see plan \xc2\xa73.2):
    - No ``import openworker`` anywhere in this file.
    - No Slack / GitHub / Linear / Notion / Calendar connector calls.
    - No sqlite3 / SQLAlchemy / vendor DB driver. JSONL only.
    - Borrowed design (envelope shape + fsync append), not runtime.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from oiagent_coworker.inbox.models import InboxItem, InboxItemEnvelope

__all__ = ["OIagentCoworkerInboxPersistence"]


_LOGGER = logging.getLogger(__name__)


_REQUIRED_KEYS: frozenset[str] = frozenset({
    "envelope_id",
    "timestamp",
    "action",
    "item_id",
})


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for dataclass / datetime payloads."""
    if isinstance(obj, datetime):
        # ``datetime.isoformat`` always produces a parseable string for
        # both naive and tz-aware datetimes; ``fromisoformat`` is the
        # inverse. UTC tz-aware instances end with "+00:00".
        return obj.isoformat()
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _serialize_envelope(envelope: InboxItemEnvelope) -> str:
    """Serialize an :class:`InboxItemEnvelope` to a single JSON line."""
    item = envelope.item
    payload: dict[str, Any] = {
        "envelope_id": envelope.envelope_id,
        "timestamp": envelope.timestamp.isoformat(),
        "action": envelope.action,
        "item_id": envelope.item_id,
        "actor": envelope.actor,
        "item": (
            {
                "item_id": item.item_id,
                "kind": item.kind.value,
                "priority": item.priority.value,
                "title": item.title,
                "body": item.body,
                "source": item.source,
                "created_at": item.created_at.isoformat(),
                "expires_at": (
                    item.expires_at.isoformat() if item.expires_at else None
                ),
                "metadata": item.metadata,
            }
            if item is not None
            else None
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default)


def _deserialize_envelope(payload: dict[str, Any]) -> InboxItemEnvelope:
    """Inverse of :func:`_serialize_envelope`. Caller guarantees shape."""
    raw_item = payload.get("item")
    item: InboxItem | None = None
    if isinstance(raw_item, dict):
        item = InboxItem(
            item_id=str(raw_item["item_id"]),
            kind=str(raw_item["kind"]),
            priority=str(raw_item["priority"]),
            title=str(raw_item.get("title", "")),
            body=str(raw_item.get("body", "")),
            source=str(raw_item.get("source", "")),
            created_at=datetime.fromisoformat(str(raw_item["created_at"])),
            expires_at=(
                datetime.fromisoformat(str(raw_item["expires_at"]))
                if raw_item.get("expires_at")
                else None
            ),
            metadata=dict(raw_item.get("metadata") or {}),
        )
    return InboxItemEnvelope(
        envelope_id=int(payload["envelope_id"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        action=str(payload["action"]),
        item=item,
        item_id=str(payload["item_id"]),
        actor=str(payload.get("actor", "")),
    )


class OIagentCoworkerInboxPersistence:
    """Append-only JSONL store for :class:`InboxItemEnvelope`.

    Thread safety:
        The class is intended to be driven from a single
        :class:`OIagentCoworkerInboxService` instance. The service
        already serializes all writes through an ``RLock``; this class
        does *not* acquire its own lock because the only callers (the
        service) own the surrounding lock.

    Disk layout:
        ``storage_path`` is the on-disk JSONL file. The parent directory
        is created lazily on the first ``append``. Atomic renames use
        ``tempfile`` + ``os.replace`` to guarantee the on-disk file is
        never observed in a partial state by a concurrent reader.
    """

    def __init__(self, path: Path) -> None:
        self.storage_path: Path = Path(path)
        self._ensure_parent()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, envelope: InboxItemEnvelope) -> None:
        """Append an envelope to the JSONL log with ``fsync`` durability.

        Atomicity: ``open(mode='a')`` + ``write + flush + fsync``. JSON
        cannot be partially observed for a single ``write`` because the
        writer uses a single ``fp.write`` call per line, and ``os.write``
        under the hood is guaranteed atomic for buffers smaller than
        ``PIPE_BUF`` on POSIX. On Windows the append-mode guarantee is
        weaker but each envelope ends with ``\\n``, so a torn prefix is
        always discarded by :meth:`replay`.
        """
        self._ensure_parent()
        line = _serialize_envelope(envelope) + "\n"
        with open(self.storage_path, "a", encoding="utf-8") as fp:
            fp.write(line)
            fp.flush()
            os.fsync(fp.fileno())

    def replay(self) -> Iterator[InboxItemEnvelope]:
        """Yield every envelope in insertion order.

        Lines that fail to parse, or that are missing one of the
        required keys, are logged at WARNING and skipped. A missing
        file is treated as an empty log (the iterator yields nothing).
        """
        if not self.storage_path.exists():
            return
        with open(self.storage_path, "r", encoding="utf-8") as fp:
            for lineno, raw in enumerate(fp, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    _LOGGER.warning(
                        "inbox: skipping corrupt line %d: %s", lineno, exc
                    )
                    continue
                if not isinstance(payload, dict):
                    _LOGGER.warning(
                        "inbox: line %d is not a JSON object; skipping",
                        lineno,
                    )
                    continue
                missing = _REQUIRED_KEYS - payload.keys()
                if missing:
                    _LOGGER.warning(
                        "inbox: line %d missing required keys %s; skipping",
                        lineno,
                        sorted(missing),
                    )
                    continue
                try:
                    envelope = _deserialize_envelope(payload)
                except (KeyError, ValueError, TypeError) as exc:
                    _LOGGER.warning(
                        "inbox: line %d failed to deserialize: %s",
                        lineno,
                        exc,
                    )
                    continue
                yield envelope

    def last_envelope_id(self) -> int:
        """Return the highest ``envelope_id`` currently on disk.

        Performs a single backwards scan of the file: it reads the file
        in reverse and parses just the ``envelope_id`` field of each
        non-empty line. This avoids hydrating the whole log when the
        service only needs the next id. Returns ``0`` for an empty /
        missing file.
        """
        if not self.storage_path.exists():
            return 0
        # We read the file forwards (a single small file: <1MB for
        # 10k envelopes at the configured cap) but only keep the highest
        # id seen so far. Memory cost is O(1) regardless of length.
        highest = 0
        with open(self.storage_path, "rb") as fp:
            for raw in fp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                env_id = payload.get("envelope_id")
                if isinstance(env_id, int) and env_id > highest:
                    highest = env_id
        return highest

    def rewrite(self, envelopes: list[InboxItemEnvelope]) -> None:
        """Atomically replace the on-disk log with the given envelopes.

        Used by :meth:`OIagentCoworkerInboxService.purge_expired` to keep
        the on-disk file bounded. Writes to a sibling ``.tmp`` file,
        ``fsync`` it, then ``os.replace`` for the atomic-rename
        guarantee. If ``envelopes`` is empty the file is left in place
        but truncated to zero bytes; this keeps the JSONL parser happy
        (treats the file as an empty log).
        """
        self._ensure_parent()
        parent = self.storage_path.parent
        # Use NamedTemporaryFile-style sibling so os.replace is atomic
        # on the same filesystem. ``delete=False`` + manual unlink is
        # avoided in favour of an explicit temp + replace.
        fd, tmp_path = tempfile.mkstemp(
            prefix=self.storage_path.name + ".",
            suffix=".tmp",
            dir=str(parent) if parent else None,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                for envelope in envelopes:
                    fp.write(_serialize_envelope(envelope))
                    fp.write("\n")
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, self.storage_path)
        except Exception:
            # Best-effort cleanup of the temp on failure; never let
            # rewrite() raise from an IO cleanup.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_parent(self) -> None:
        """Create the parent directory of ``storage_path`` if needed."""
        parent = self.storage_path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
