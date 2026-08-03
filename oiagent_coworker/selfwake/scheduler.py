# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/scheduler/scheduler.py +
#                     openworker/scheduler/handler_registry.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; reduced the
#     upstream asyncio daemon Scheduler to a single-process, synchronous
#     service that lives behind a JSONL log and a threading.RLock.
#   - Replaced the upstream trigger.evaluate() async coroutine with a
#     synchronous tick(now=None) entry point; loop / cron / asyncio
#     scheduling is the caller's responsibility (W2-6 CLI / server).
#   - Handler registry is exposed as an explicit two-step registration:
#     set_handler(id, callable) FIRST, then register(task) with a
#     ScheduleHandler that references the registered id. The upstream
#     "register(handler, callable)" combined form is dropped so the
#     pre-flight existence check can fail fast with a typed exception.
#   - Audit emission goes through the W2-1.4 ``AuditSink`` protocol
#     with an ``AuditDecision(kind='selfwake', ...)`` envelope carrying
#     the action name + affected task in ``metadata`` and the optional
#     error text in ``error``. W2-3.1a widens the closed ``AuditKind``
#     Literal to include ``"selfwake"``.
#   - Cron evaluation is a deliberately simple in-file implementation
#     (``_should_fire_cron``) using ANY-match semantics on the five
#     fields; ``croniter`` is intentionally not depended on.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Self-wake scheduler for OIagent Coworker (W2-3.1 + W2-3.2 merged).

The scheduler is the single owner of the in-memory scheduled-task
state. It holds:

  * ``self._tasks`` -- ``dict[task_id -> ScheduledTask]`` for O(1) get.
  * ``self._envelopes`` -- ``dict[envelope_id -> TaskFireEnvelope]``
    appended on every state transition.
  * ``self._handlers`` -- ``dict[handler_id -> Callable]`` populated by
    :meth:`set_handler`. Registering a task with an unknown
    handler_id raises :class:`OIagentCoworkerSelfWakeUnknownHandlerError`
    immediately; ``tick`` therefore never encounters an unbound handler.
  * ``self._next_envelope_id`` -- monotonically-increasing counter that
    the persistence layer uses to assign ids before each write.

Concurrency model:
  All public methods acquire ``self._lock`` (a :class:`threading.RLock`)
  before mutating state. Handlers are invoked with the lock held --
  handlers are expected to be quick side-effects; blocking I/O is the
  caller's job, not the scheduler's.

tick(now) synchronous API:
  ``tick(now=None)`` is a synchronous entry point; no asyncio runtime,
  no background threads, no ``threading.Timer``. CLI (W2-6 ``oic-selfwake
  tick``) calls ``tick`` once per process invocation; the daemon tier
  (W2-6) wraps ``tick`` in whatever loop it prefers (cron facade,
  asyncio sleep loop, or simple ``while True`` + ``time.sleep(1)``).
  The loop is the server's problem; the scheduler only provides the
  per-tick evaluation.

Anti-flattery boundary (see plan §3.3 / §8.3):
    - No ``import openworker`` anywhere in this file.
    - No ``croniter`` / APScheduler / asyncio daemon.
    - No Slack / GitHub / Linear / Notion / Calendar connectors.
    - No ``openai`` / ``anthropic`` direct SDK calls.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oiagent_coworker.selfwake.models import (
    ScheduledTask,
    ScheduleHandler,
    ScheduleSpec,
    TaskFireEnvelope,
    TaskQuery,
    TriggerKind,
    TriggerStatus,
)
from oiagent_coworker.selfwake.persistence import OIagentCoworkerSelfWakePersistence

__all__ = [
    "OIagentCoworkerSelfWakeScheduler",
    "OIagentCoworkerSelfWakeUnknownHandlerError",
]


_LOGGER = logging.getLogger(__name__)


Clock = Callable[[], datetime]
HandlerCallable = Callable[[dict[str, Any]], None]


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _new_task_id() -> str:
    return uuid.uuid4().hex


class OIagentCoworkerSelfWakeUnknownHandlerError(KeyError):
    """Raised when :meth:`register` gets an unregistered ``handler_id``.

    Inherits from :class:`KeyError` (missing-key lookup semantics).
    """

    def __init__(self, handler_id: str) -> None:
        super().__init__(
            f"OIagentCoworkerSelfWakeScheduler: handler_id {handler_id!r} "
            f"not registered; call set_handler() first"
        )
        self.handler_id = handler_id


def _should_fire_cron(spec: ScheduleSpec, now: datetime) -> bool:
    """ANY-match cron evaluation.

    Each field with ``None`` is the wildcard. A field with a concrete
    ``frozenset[int]`` requires ``now.<value> in frozenset``. When
    BOTH ``cron_day`` and ``cron_dow`` are non-None, the "union"
    semantics mirror classic ``cron(5)`` (the fire happens if EITHER
    matches).
    """
    if spec.cron_minute is not None and now.minute not in spec.cron_minute:
        return False
    if spec.cron_hour is not None and now.hour not in spec.cron_hour:
        return False
    if spec.cron_month is not None and now.month not in spec.cron_month:
        return False
    if spec.cron_day is not None and spec.cron_dow is not None:
        if not (now.day in spec.cron_day or now.weekday() in spec.cron_dow):
            return False
    elif (
        spec.cron_day is not None and now.day not in spec.cron_day
    ) or (
        spec.cron_dow is not None and now.weekday() not in spec.cron_dow
    ):
        return False
    return True


def _should_fire_interval(
    spec: ScheduleSpec,
    now: datetime,
    last_fired_at: datetime | None,
    created_at: datetime,
) -> bool:
    """INTERVAL gate: fire when ``interval_seconds`` has elapsed since
    the last fire (or since ``created_at`` for the first fire) AND the
    initial delay has elapsed.
    """
    if spec.interval_seconds is None or spec.interval_seconds < 1:
        return False
    elapsed_since_created = (now - created_at).total_seconds()
    if elapsed_since_created < spec.initial_delay_seconds:
        return False
    if last_fired_at is None:
        return elapsed_since_created >= spec.initial_delay_seconds
    return (now - last_fired_at).total_seconds() >= spec.interval_seconds


def _should_fire_manual(task: ScheduledTask, now: datetime) -> bool:
    """MANUAL triggers fire only on explicit dispatch, never via ``tick``."""
    _ = (task, now)
    return False


def _should_fire_once(
    spec: ScheduleSpec,
    now: datetime,
    last_fired_at: datetime | None,
) -> bool:
    """ONCE gate: fire once when ``spec.fire_at <= now``."""
    if spec.fire_at is None or last_fired_at is not None:
        return False
    return spec.fire_at <= now


class OIagentCoworkerSelfWakeScheduler:
    """Synchronous in-memory + JSONL-backed self-wake scheduler.

    Public API:
        * :meth:`set_handler` -- register / replace a handler callable.
        * :meth:`register` -- create + persist a new task.
        * :meth:`tick` -- evaluate + fire due tasks.
        * :meth:`cancel` / :meth:`disable` / :meth:`enable` -- lifecycle.
        * :meth:`query` / :meth:`get` / :meth:`count` -- read side.

    Thread safety: All public methods acquire a re-entrant lock so
    handlers that call back into the scheduler are safe.
    """

    def __init__(
        self,
        storage_path: Path,
        audit_sink: Callable[[Any], None] | None = None,
        clock: Clock | None = None,
    ) -> None:
        if audit_sink is not None and not callable(audit_sink):
            raise TypeError(
                f"audit_sink must be callable or None; "
                f"got {type(audit_sink).__name__}"
            )
        self.storage_path: Path = Path(storage_path)
        self._audit_sink = audit_sink
        self._clock: Clock = clock if clock is not None else _default_clock
        self._persistence = OIagentCoworkerSelfWakePersistence(self.storage_path)
        self._lock = threading.RLock()
        self._tasks: dict[str, ScheduledTask] = {}
        self._envelopes: dict[int, TaskFireEnvelope] = {}
        self._handlers: dict[str, HandlerCallable] = {}
        self._next_envelope_id: int = 1
        self._task_to_envelope_id: dict[str, int] = {}
        self._rebuild_from_disk()

    # ------------------------------------------------------------------
    # Handler registry
    # ------------------------------------------------------------------

    def set_handler(
        self,
        handler_id: str,
        callable_: HandlerCallable,
    ) -> None:
        """Register (or replace) the callable for a handler_id.

        Idempotent: re-registering the same handler_id replaces the
        previous callable. ``callable_`` MUST be
        ``Callable[[dict[str, Any]], None]`` and MUST be safe to
        invoke from the scheduler's lock context.
        """
        if not handler_id:
            raise ValueError(
                "OIagentCoworkerSelfWakeScheduler.set_handler: "
                "handler_id must be a non-empty string"
            )
        if not callable(callable_):
            raise TypeError(
                "OIagentCoworkerSelfWakeScheduler.set_handler: "
                f"callable must be callable, got {type(callable_).__name__}"
            )
        with self._lock:
            self._handlers[handler_id] = callable_

    # ------------------------------------------------------------------
    # Lifecycle -- register / cancel / disable / enable
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        schedule: ScheduleSpec,
        handler: ScheduleHandler,
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> ScheduledTask:
        """Create + persist a new scheduled task.

        Raises:
            OIagentCoworkerSelfWakeUnknownHandlerError: If
                ``handler.handler_id`` has not been pre-registered via
                :meth:`set_handler`.
        """
        if not isinstance(schedule, ScheduleSpec):
            raise TypeError(
                f"schedule must be ScheduleSpec, "
                f"got {type(schedule).__name__}"
            )
        if not isinstance(handler, ScheduleHandler):
            raise TypeError(
                f"handler must be ScheduleHandler, "
                f"got {type(handler).__name__}"
            )
        if not name:
            raise ValueError(
                "OIagentCoworkerSelfWakeScheduler.register: "
                "name must be a non-empty string"
            )
        with self._lock:
            if handler.handler_id not in self._handlers:
                raise OIagentCoworkerSelfWakeUnknownHandlerError(
                    handler.handler_id
                )
            now = self._clock()
            task = ScheduledTask(
                task_id=_new_task_id(),
                name=name,
                schedule=schedule,
                handler=handler,
                created_at=now,
                enabled=enabled,
                last_status=TriggerStatus.PENDING,
                last_fired_at=None,
                last_error=None,
                fire_count=0,
                metadata=dict(metadata) if metadata else {},
            )
            envelope = self._build_envelope_locked(
                "register", task, "user", error=None
            )
            self._tasks[task.task_id] = task
            self._envelopes[envelope.envelope_id] = envelope
            self._task_to_envelope_id[task.task_id] = envelope.envelope_id
            self._persistence.append(envelope)
            self._audit_register_locked(envelope)
            return task

    def cancel(self, task_id: str, actor: str = "user") -> bool:
        """Permanently cancel (idempotent). Cancelled tasks cannot be re-enabled."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.last_status == TriggerStatus.CANCELLED:
                return False
            new_task = _dc_replace(task, last_status=TriggerStatus.CANCELLED)
            self._tasks[task_id] = new_task
            envelope = self._build_envelope_locked(
                "cancel", new_task, actor, error=None
            )
            self._envelopes[envelope.envelope_id] = envelope
            self._task_to_envelope_id[task_id] = envelope.envelope_id
            self._persistence.append(envelope)
            self._audit_cancel_locked(envelope)
            return True

    def disable(self, task_id: str, actor: str = "user") -> bool:
        """Temporarily disable (idempotent). Re-enable with :meth:`enable`."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or not task.enabled:
                return False
            if task.last_status == TriggerStatus.CANCELLED:
                return False
            new_task = _dc_replace(task, enabled=False)
            self._tasks[task_id] = new_task
            envelope = self._build_envelope_locked(
                "disable", new_task, actor, error=None
            )
            self._envelopes[envelope.envelope_id] = envelope
            self._task_to_envelope_id[task_id] = envelope.envelope_id
            self._persistence.append(envelope)
            self._audit_disable_or_enable_locked(envelope)
            return True

    def enable(self, task_id: str, actor: str = "user") -> bool:
        """Re-enable a disabled task (idempotent)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.enabled:
                return False
            if task.last_status == TriggerStatus.CANCELLED:
                return False
            new_task = _dc_replace(task, enabled=True)
            self._tasks[task_id] = new_task
            envelope = self._build_envelope_locked(
                "enable", new_task, actor, error=None
            )
            self._envelopes[envelope.envelope_id] = envelope
            self._task_to_envelope_id[task_id] = envelope.envelope_id
            self._persistence.append(envelope)
            self._audit_disable_or_enable_locked(envelope)
            return True

    # ------------------------------------------------------------------
    # tick
    # ------------------------------------------------------------------

    def tick(
        self,
        now: datetime | None = None,
    ) -> list[tuple[ScheduledTask, str | None]]:
        """Evaluate all enabled tasks; fire those whose schedule matches.

        Side effects:
          * Emits a ``tick_fire`` envelope per fire attempt.
          * Emits a ``succeed`` or ``fail`` envelope per fire.
          * Catches handler exceptions; no retry, no backoff.

        Returns:
            ``[(task, error_or_None), ...]`` for tasks that fired,
            in evaluation order. Tasks that did not fire are not
            in the returned list.
        """
        with self._lock:
            current = now if now is not None else self._clock()
            results: list[tuple[ScheduledTask, str | None]] = []
            for task in list(self._tasks.values()):
                if not task.enabled:
                    continue
                if task.last_status == TriggerStatus.CANCELLED:
                    continue
                if not self._should_fire_locked(task, current):
                    continue
                results.append(self._fire_one_locked(task, current))
            return results

    # ------------------------------------------------------------------
    # Read side
    # ------------------------------------------------------------------

    def query(self, q: TaskQuery | None = None) -> list[ScheduledTask]:
        """Return tasks matching the query, sorted by created_at asc."""
        query = q if q is not None else TaskQuery()
        limit = max(1, min(int(query.limit), 1_000_000))
        with self._lock:
            results: list[ScheduledTask] = []
            for task in self._tasks.values():
                if query.enabled_only and not task.enabled:
                    continue
                if (
                    query.handler_ids
                    and task.handler.handler_id not in query.handler_ids
                ):
                    continue
                if query.statuses and task.last_status not in query.statuses:
                    continue
                cursor = self._task_to_envelope_id.get(task.task_id, 0)
                if cursor <= query.after_id:
                    continue
                results.append(task)
            results.sort(key=lambda t: t.created_at)
            return results[:limit]

    def get(self, task_id: str) -> ScheduledTask | None:
        """Fetch one task by id; ``None`` if not found."""
        with self._lock:
            return self._tasks.get(task_id)

    def count(self, q: TaskQuery | None = None) -> int:
        """Count tasks matching the query without hydration."""
        query = q if q is not None else TaskQuery()
        with self._lock:
            total = 0
            for task in self._tasks.values():
                if query.enabled_only and not task.enabled:
                    continue
                if (
                    query.handler_ids
                    and task.handler.handler_id not in query.handler_ids
                ):
                    continue
                if query.statuses and task.last_status not in query.statuses:
                    continue
                total += 1
            return total

    # ------------------------------------------------------------------
    # Internal: fire dispatch
    # ------------------------------------------------------------------

    def _should_fire_locked(
        self, task: ScheduledTask, now: datetime
    ) -> bool:
        """Dispatch to the kind-specific gate."""
        spec = task.schedule
        kind = spec.kind
        if kind == TriggerKind.CRON:
            return _should_fire_cron(spec, now)
        if kind == TriggerKind.INTERVAL:
            return _should_fire_interval(
                spec, now, task.last_fired_at, task.created_at
            )
        if kind == TriggerKind.MANUAL:
            return _should_fire_manual(task, now)
        if kind == TriggerKind.ONCE:
            return _should_fire_once(spec, now, task.last_fired_at)
        return False

    def _fire_one_locked(
        self, task: ScheduledTask, now: datetime
    ) -> tuple[ScheduledTask, str | None]:
        """Fire one task; emit ``tick_fire`` then ``succeed`` / ``fail``.

        Updates ``self._tasks[task.task_id]`` and returns the
        ``(updated_task, error_or_None)`` tuple.
        """
        running_task = _dc_replace(
            task,
            last_status=TriggerStatus.RUNNING,
            last_fired_at=now,
            fire_count=task.fire_count + 1,
        )
        self._tasks[task.task_id] = running_task
        fire_envelope = self._build_envelope_locked(
            "tick_fire", running_task, "tick", error=None
        )
        self._envelopes[fire_envelope.envelope_id] = fire_envelope
        self._task_to_envelope_id[task.task_id] = fire_envelope.envelope_id
        self._persistence.append(fire_envelope)
        self._audit_tick_fire_locked(fire_envelope)

        callable_ = self._handlers.get(running_task.handler.handler_id)
        if callable_ is None:
            error_msg = (
                f"handler_id {running_task.handler.handler_id!r} "
                f"not registered at tick time"
            )
            return self._record_failure_locked(running_task, error_msg)
        try:
            callable_(dict(running_task.handler.payload))
        except Exception as exc:  # noqa: BLE001 -- scheduler catches all
            return self._record_failure_locked(running_task, repr(exc))

        success_task = _dc_replace(
            self._tasks[task.task_id],
            last_status=TriggerStatus.SUCCEEDED,
            last_error=None,
        )
        self._tasks[task.task_id] = success_task
        success_envelope = self._build_envelope_locked(
            "succeed", success_task, "tick", error=None
        )
        self._envelopes[success_envelope.envelope_id] = success_envelope
        self._task_to_envelope_id[task.task_id] = success_envelope.envelope_id
        self._persistence.append(success_envelope)
        self._audit_succeed_locked(success_envelope)
        return success_task, None

    def _record_failure_locked(
        self,
        running_task: ScheduledTask,
        error_msg: str,
    ) -> tuple[ScheduledTask, str | None]:
        """Persist a failed fire; emit ``fail`` envelope; return tuple."""
        failed_task = _dc_replace(
            running_task,
            last_status=TriggerStatus.FAILED,
            last_error=error_msg,
        )
        self._tasks[running_task.task_id] = failed_task
        fail_envelope = self._build_envelope_locked(
            "fail", failed_task, "tick", error=error_msg
        )
        self._envelopes[fail_envelope.envelope_id] = fail_envelope
        self._task_to_envelope_id[running_task.task_id] = (
            fail_envelope.envelope_id
        )
        self._persistence.append(fail_envelope)
        self._audit_fail_locked(fail_envelope)
        return failed_task, error_msg

    # ------------------------------------------------------------------
    # Internal: envelope construction + audit
    # ------------------------------------------------------------------

    def _build_envelope_locked(
        self,
        action: str,
        task: ScheduledTask | None,
        actor: str,
        *,
        error: str | None,
    ) -> TaskFireEnvelope:
        """Build a fresh envelope and bump the next-id counter."""
        env_id = self._next_envelope_id
        self._next_envelope_id += 1
        return TaskFireEnvelope(
            envelope_id=env_id,
            timestamp=self._clock(),
            task_id=task.task_id if task is not None else "",
            action=action,
            task=task,
            error=error,
            actor=actor,
            metadata={},
        )

    def _audit_register_locked(
        self, envelope: TaskFireEnvelope
    ) -> None:
        """Emit a :class:`AuditDecision` for ``register``.

        ``kind="selfwake"`` lands in the ``AuditKind`` Literal after
        W2-3.1a widens it; runtime semantics (action + task in
        ``metadata``, error in ``error``) are unaffected.
        """
        if self._audit_sink is None:
            return
        try:
            from oiagent_coworker.permissions.audit import AuditDecision

            metadata: dict[str, Any] = {
                "selfwake_action": "register",
                "task_id": envelope.task_id,
                "actor": envelope.actor,
                "envelope_id": envelope.envelope_id,
            }
            if envelope.task is not None:
                metadata["task"] = envelope.task
            decision = AuditDecision(kind="selfwake", timestamp=envelope.timestamp, standing_rule_action=None, metadata=metadata, error=envelope.error)
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001 -- audit must not break lifecycle path
            _LOGGER.warning(
                "selfwake audit_sink raised %s; ignored", exc
            )

    def _audit_tick_fire_locked(
        self, envelope: TaskFireEnvelope
    ) -> None:
        """Emit a :class:`AuditDecision` for ``tick_fire``."""
        if self._audit_sink is None:
            return
        try:
            from oiagent_coworker.permissions.audit import AuditDecision

            metadata: dict[str, Any] = {
                "selfwake_action": "tick_fire",
                "task_id": envelope.task_id,
                "actor": envelope.actor,
                "envelope_id": envelope.envelope_id,
            }
            if envelope.task is not None:
                metadata["task"] = envelope.task
            decision = AuditDecision(kind="selfwake", timestamp=envelope.timestamp, standing_rule_action=None, metadata=metadata, error=envelope.error)
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "selfwake audit_sink raised %s; ignored", exc
            )

    def _audit_succeed_locked(
        self, envelope: TaskFireEnvelope
    ) -> None:
        """Emit a :class:`AuditDecision` for ``succeed``."""
        if self._audit_sink is None:
            return
        try:
            from oiagent_coworker.permissions.audit import AuditDecision

            metadata: dict[str, Any] = {
                "selfwake_action": "succeed",
                "task_id": envelope.task_id,
                "actor": envelope.actor,
                "envelope_id": envelope.envelope_id,
            }
            if envelope.task is not None:
                metadata["task"] = envelope.task
            decision = AuditDecision(kind="selfwake", timestamp=envelope.timestamp, standing_rule_action=None, metadata=metadata, error=envelope.error)
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "selfwake audit_sink raised %s; ignored", exc
            )

    def _audit_fail_locked(
        self, envelope: TaskFireEnvelope
    ) -> None:
        """Emit a :class:`AuditDecision` for ``fail`` (carries error text)."""
        if self._audit_sink is None:
            return
        try:
            from oiagent_coworker.permissions.audit import AuditDecision

            metadata: dict[str, Any] = {
                "selfwake_action": "fail",
                "task_id": envelope.task_id,
                "actor": envelope.actor,
                "envelope_id": envelope.envelope_id,
            }
            if envelope.task is not None:
                metadata["task"] = envelope.task
            decision = AuditDecision(kind="selfwake", timestamp=envelope.timestamp, standing_rule_action=None, metadata=metadata, error=envelope.error)
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "selfwake audit_sink raised %s; ignored", exc
            )

    def _audit_cancel_locked(
        self, envelope: TaskFireEnvelope
    ) -> None:
        """Emit a :class:`AuditDecision` for ``cancel``."""
        if self._audit_sink is None:
            return
        try:
            from oiagent_coworker.permissions.audit import AuditDecision

            metadata: dict[str, Any] = {
                "selfwake_action": "cancel",
                "task_id": envelope.task_id,
                "actor": envelope.actor,
                "envelope_id": envelope.envelope_id,
            }
            if envelope.task is not None:
                metadata["task"] = envelope.task
            decision = AuditDecision(kind="selfwake", timestamp=envelope.timestamp, standing_rule_action=None, metadata=metadata, error=envelope.error)
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "selfwake audit_sink raised %s; ignored", exc
            )

    def _audit_disable_or_enable_locked(
        self, envelope: TaskFireEnvelope
    ) -> None:
        """Emit a :class:`AuditDecision` for ``disable`` / ``enable``.

        Both lifecycle ops share this helper because the emitted
        ``AuditDecision`` shape is identical -- only ``action`` differs
        (carried in ``metadata['selfwake_action']``).
        """
        if self._audit_sink is None:
            return
        try:
            from oiagent_coworker.permissions.audit import AuditDecision

            metadata: dict[str, Any] = {
                "selfwake_action": envelope.action,
                "task_id": envelope.task_id,
                "actor": envelope.actor,
                "envelope_id": envelope.envelope_id,
            }
            if envelope.task is not None:
                metadata["task"] = envelope.task
            decision = AuditDecision(kind="selfwake", timestamp=envelope.timestamp, standing_rule_action=None, metadata=metadata, error=envelope.error)
            self._audit_sink(decision)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "selfwake audit_sink raised %s; ignored", exc
            )

    # ------------------------------------------------------------------
    # Internal: replay
    # ------------------------------------------------------------------

    def _rebuild_from_disk(self) -> None:
        """Replay envelopes to rebuild the in-memory index on startup.

        The handler registry is intentionally NOT rebuilt from disk --
        handlers are in-process callables the daemon owner wires
        explicitly via :meth:`set_handler` on every startup. A task
        whose handler_id is no longer registered after restart fails
        at the next ``tick`` with a clear message in ``last_error``.
        """
        self._tasks.clear()
        self._envelopes.clear()
        self._task_to_envelope_id.clear()
        max_id = 0
        for envelope in self._persistence.replay():
            self._envelopes[envelope.envelope_id] = envelope
            max_id = max(max_id, envelope.envelope_id)
            if envelope.action == "register" and envelope.task is not None:
                self._tasks[envelope.task.task_id] = envelope.task
                self._task_to_envelope_id[envelope.task.task_id] = (
                    envelope.envelope_id
                )
            elif (
                envelope.action
                in {"cancel", "disable", "enable", "tick_fire", "succeed", "fail"}
                and envelope.task_id in self._tasks
                and envelope.task is not None
            ):
                self._tasks[envelope.task_id] = envelope.task
                self._task_to_envelope_id[envelope.task_id] = (
                    envelope.envelope_id
                )
        self._next_envelope_id = max_id + 1
