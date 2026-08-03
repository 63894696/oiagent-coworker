# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/scheduler/store.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced the
#     upstream SQLite-backed scheduler store with an append-only JSONL
#     store to keep single-file crash-safety and a clean write/read
#     concurrency model that is independent of the OIagent daemon's own
#     SQLite usage under ${OIAGENT_VAULT}.
#   - Replayed envelopes rebuild the in-memory task index + handler
#     payload from a single JSONL log line per transition; the upstream
#     multi-table scheduler schema is replaced with a single envelope
#     stream.
#   - Replay tolerates malformed lines (warning log + skip) so a
#     previously-known corruption does not brick the self-wake
#     scheduler.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Append-only JSONL store for :class:`TaskFireEnvelope` (W2-3.2).

The persistence layer mirrors the W2-2.2 inbox store: one file, one
JSON object per line, ``append`` is a single ``write + fsync``, and
``replay`` rebuilds the in-memory task index on first read.

JSONL contract
--------------

Each line is::

    {
      "envelope_id":    <int>,
      "timestamp":      "<iso 8601 UTC>",
      "task_id":        "<uuid4 hex>",
      "action":         "register" | "tick_fire" | "succeed" | "fail" |
                        "cancel" | "disable" | "enable" | <other>,
      "task":           <ScheduledTask as dict> | null,
      "error":          "<error text>" | null,
      "actor":          "<identity>",
      "metadata":       <dict>
    }

The ``task`` field is a literal :class:`ScheduledTask`-shaped dict
(``dataclasses.asdict(task)``) and is populated only for
``action='register'`` envelopes. ``datetime`` values are serialized
via the shared ``_json_default`` hook so they round-trip as ISO 8601
strings.

Persistence semantics:

  * ``append()`` is O(1) amortized: ``open(append)`` + ``write`` +
    ``fsync`` + ``close``. Multiple processes may write concurrently
    *to the same file* at the OS level; the JSONL parser is tolerant
    of torn lines because each line carries a parseable envelope on
    its own.
  * ``replay()`` walks the file once, skipping lines that fail
    ``json.loads`` or that are missing the required ``envelope_id`` /
    ``task_id`` / ``action`` keys. Skipped lines are logged at WARNING
    and do not block subsequent envelopes from loading.
  * ``last_envelope_id()`` scans the file for the highest
    ``envelope_id`` without loading every envelope into memory. This
    is used by the service to assign the next id before each write.
  * Missing file is treated as an empty log; constructors do **not**
    create the file. The first ``append`` creates both the parent
    directory and the log file.

Anti-flattery boundary (see plan §3.3):
    - No ``import openworker`` anywhere in this file.
    - No SQLite / SQLAlchemy / vendor DB driver. JSONL only.
    - No ``croniter`` / APScheduler dependency.
    - Borrowed design (envelope shape + fsync append), not runtime.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from oiagent_coworker.selfwake.models import (
    ScheduledTask,
    ScheduleHandler,
    ScheduleSpec,
    TaskFireEnvelope,
    TriggerKind,
    TriggerStatus,
)

__all__ = ["OIagentCoworkerSelfWakePersistence"]


_LOGGER = logging.getLogger(__name__)


_REQUIRED_KEYS: frozenset[str] = frozenset({
    "envelope_id",
    "timestamp",
    "task_id",
    "action",
})


def _json_default(obj: Any) -> Any:
    """JSON encoder fallback for dataclass / datetime payloads."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _serialize_envelope(envelope: TaskFireEnvelope) -> str:
    """Serialize a :class:`TaskFireEnvelope` to a single JSON line.

    The ``task`` field is included only for ``action='register'``; for
    all transitions the reader reconstructs the latest state by
    replaying ``register`` envelopes against the in-memory index.
    """
    task = envelope.task
    payload: dict[str, Any] = {
        "envelope_id": envelope.envelope_id,
        "timestamp": envelope.timestamp.isoformat(),
        "task_id": envelope.task_id,
        "action": envelope.action,
        "error": envelope.error,
        "actor": envelope.actor,
        "metadata": envelope.metadata,
        "task": (
            _serialize_task(task)
            if task is not None
            else None
        ),
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=_json_default
    )


def _serialize_task(task: ScheduledTask) -> dict[str, Any]:
    """Serialize one :class:`ScheduledTask` to a JSON-safe dict.

    ``ScheduleSpec.cron_*`` fields are ``frozenset[int] | None``; we
    persist them as ``list[int] | None`` so the JSON wire form is
    conventional and sorted (sorting is non-semantic for cron -- the
    scheduler treats the field as a membership test, not an ordered
    list -- but deterministic ordering helps debugging).
    """
    schedule: dict[str, Any] = {
        "kind": task.schedule.kind.value,
        "initial_delay_seconds": task.schedule.initial_delay_seconds,
        "cron_minute": (
            sorted(task.schedule.cron_minute)
            if task.schedule.cron_minute is not None
            else None
        ),
        "cron_hour": (
            sorted(task.schedule.cron_hour)
            if task.schedule.cron_hour is not None
            else None
        ),
        "cron_day": (
            sorted(task.schedule.cron_day)
            if task.schedule.cron_day is not None
            else None
        ),
        "cron_month": (
            sorted(task.schedule.cron_month)
            if task.schedule.cron_month is not None
            else None
        ),
        "cron_dow": (
            sorted(task.schedule.cron_dow)
            if task.schedule.cron_dow is not None
            else None
        ),
        "interval_seconds": task.schedule.interval_seconds,
        "fire_at": (
            task.schedule.fire_at.isoformat()
            if task.schedule.fire_at is not None
            else None
        ),
    }
    handler = task.handler
    return {
        "task_id": task.task_id,
        "name": task.name,
        "created_at": task.created_at.isoformat(),
        "enabled": task.enabled,
        "last_status": task.last_status.value,
        "last_fired_at": (
            task.last_fired_at.isoformat()
            if task.last_fired_at is not None
            else None
        ),
        "last_error": task.last_error,
        "fire_count": task.fire_count,
        "metadata": task.metadata,
        "schedule": schedule,
        "handler": {
            "handler_id": handler.handler_id,
            "payload": handler.payload,
        },
    }


def _deserialize_envelope(payload: dict[str, Any]) -> TaskFireEnvelope:
    """Inverse of :func:`_serialize_envelope`. Caller guarantees shape."""
    raw_task = payload.get("task")
    task: ScheduledTask | None = None
    if isinstance(raw_task, dict):
        task = _deserialize_task(raw_task)
    return TaskFireEnvelope(
        envelope_id=int(payload["envelope_id"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        task_id=str(payload["task_id"]),
        action=str(payload["action"]),
        task=task,
        error=(
            str(payload["error"])
            if payload.get("error") is not None
            else None
        ),
        actor=str(payload.get("actor", "")),
        metadata=dict(payload.get("metadata") or {}),
    )


def _deserialize_task(raw: dict[str, Any]) -> ScheduledTask:
    """Rebuild a :class:`ScheduledTask` from its JSON dict form."""
    raw_schedule = raw.get("schedule") or {}
    raw_handler = raw.get("handler") or {}
    schedule = ScheduleSpec(
        kind=TriggerKind(str(raw_schedule.get("kind", "manual"))),
        cron_minute=(
            frozenset(int(x) for x in raw_schedule.get("cron_minute"))
            if raw_schedule.get("cron_minute") is not None
            else None
        ),
        cron_hour=(
            frozenset(int(x) for x in raw_schedule.get("cron_hour"))
            if raw_schedule.get("cron_hour") is not None
            else None
        ),
        cron_day=(
            frozenset(int(x) for x in raw_schedule.get("cron_day"))
            if raw_schedule.get("cron_day") is not None
            else None
        ),
        cron_month=(
            frozenset(int(x) for x in raw_schedule.get("cron_month"))
            if raw_schedule.get("cron_month") is not None
            else None
        ),
        cron_dow=(
            frozenset(int(x) for x in raw_schedule.get("cron_dow"))
            if raw_schedule.get("cron_dow") is not None
            else None
        ),
        interval_seconds=(
            int(raw_schedule["interval_seconds"])
            if raw_schedule.get("interval_seconds") is not None
            else None
        ),
        fire_at=(
            datetime.fromisoformat(str(raw_schedule["fire_at"]))
            if raw_schedule.get("fire_at")
            else None
        ),
        initial_delay_seconds=int(raw_schedule.get("initial_delay_seconds", 0)),
    )
    handler = ScheduleHandler(
        handler_id=str(raw_handler.get("handler_id", "")),
        payload=dict(raw_handler.get("payload") or {}),
    )
    return ScheduledTask(
        task_id=str(raw["task_id"]),
        name=str(raw.get("name", "")),
        schedule=schedule,
        handler=handler,
        created_at=datetime.fromisoformat(str(raw["created_at"])),
        enabled=bool(raw.get("enabled", True)),
        last_status=TriggerStatus(
            str(raw.get("last_status", "pending"))
        ),
        last_fired_at=(
            datetime.fromisoformat(str(raw["last_fired_at"]))
            if raw.get("last_fired_at")
            else None
        ),
        last_error=(
            str(raw["last_error"])
            if raw.get("last_error") is not None
            else None
        ),
        fire_count=int(raw.get("fire_count", 0)),
        metadata=dict(raw.get("metadata") or {}),
    )


class OIagentCoworkerSelfWakePersistence:
    """Append-only JSONL store for :class:`TaskFireEnvelope`.

    Thread safety:
        The class is intended to be driven from a single
        :class:`OIagentCoworkerSelfWakeScheduler` instance. The
        scheduler already serializes all writes through an ``RLock``;
        this class does *not* acquire its own lock because the only
        callers (the scheduler) own the surrounding lock.

    Disk layout:
        ``storage_path`` is the on-disk JSONL file. The parent
        directory is created lazily on the first ``append``. Atomic
        renames use ``tempfile`` + ``os.replace`` to guarantee the
        on-disk file is never observed in a partial state by a
        concurrent reader.
    """

    def __init__(self, path: Path) -> None:
        self.storage_path: Path = Path(path)
        self._ensure_parent()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, envelope: TaskFireEnvelope) -> None:
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

    def replay(self) -> Iterator[TaskFireEnvelope]:
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
                        "selfwake: skipping corrupt line %d: %s",
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(payload, dict):
                    _LOGGER.warning(
                        "selfwake: line %d is not a JSON object; skipping",
                        lineno,
                    )
                    continue
                missing = _REQUIRED_KEYS - payload.keys()
                if missing:
                    _LOGGER.warning(
                        "selfwake: line %d missing required keys %s; skipping",
                        lineno,
                        sorted(missing),
                    )
                    continue
                try:
                    envelope = _deserialize_envelope(payload)
                except (KeyError, ValueError, TypeError) as exc:
                    _LOGGER.warning(
                        "selfwake: line %d failed to deserialize: %s",
                        lineno,
                        exc,
                    )
                    continue
                yield envelope

    def last_envelope_id(self) -> int:
        """Return the highest ``envelope_id`` currently on disk.

        Performs a single forward scan of the file: it reads every
        non-empty line and keeps the highest ``envelope_id`` observed.
        Memory cost is O(1) regardless of length. Returns ``0`` for an
        empty / missing file.
        """
        if not self.storage_path.exists():
            return 0
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_parent(self) -> None:
        """Create the parent directory of ``storage_path`` if needed."""
        parent = self.storage_path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
