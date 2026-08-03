# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/scheduler/triggers.py + openworker/scheduler/tasks.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; reduced the upstream
#     three-trigger taxonomy (Timer / Completion / Event) to a four-kind
#     TriggerKind enum (CRON / INTERVAL / MANUAL / ONCE) so the scheduler
#     can choose dispatch semantics without an upstream-style event bus.
#   - Replaced the upstream Trigger / CompletionTrigger / EventTrigger
#     polymorphic class hierarchy with a single frozen ScheduleSpec
#     dataclass that carries all trigger-mode payloads (cron fields,
#     interval seconds, one-shot fire_at, initial delay) and stays
#     hashable for downstream caching.
#   - Cron fields are typed as ``frozenset[int] | None`` (None = wildcard
#     "any") so the dataclass remains immutable under @dataclass(frozen=True);
#     this drops upstream's mutable-list cron field implementation in
#     favour of a clean frozen surface that round-trips through JSONL.
#   - ScheduleHandler payload is enforced JSON-serializable
#     (``str | int | float | bool | None | dict | list``) so the
#     envelope log can rebuild scheduler state without pickling.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Self-wake protocol data model for OIagent Coworker (W2-3.1).

This module defines the OIagent-facing self-wake surface as seven frozen
dataclasses (plus two ``(str, Enum)`` discriminator enums):

    * :class:`TriggerKind` -- four-kind string enum that selects the
      trigger semantics of a :class:`ScheduleSpec`: standard 5-field
      cron, "every N seconds", one-shot manual fire, or a one-shot
      scheduled fire_at. The taxonomy is intentionally flat (one
      discriminator per registered task); the scheduler dispatches
      through the matching ``_should_fire_*`` helper.

    * :class:`TriggerStatus` -- six-state lifecycle enum
      (``pending`` / ``running`` / ``succeeded`` / ``failed`` /
      ``cancelled`` / ``disabled``). The status field on a
      :class:`ScheduledTask` is the last-observed state for dashboard
      and audit purposes; the durable record of every transition lives
      in :class:`TaskFireEnvelope`.

    * :class:`ScheduleSpec` -- the immutable specification of *how* a
      task fires. Cron fields are typed as ``frozenset[int] | None``
      (None = wildcard) so the dataclass stays hashable and
      ``dataclasses.replace``-safe. The ``interval_seconds`` /
      ``fire_at`` fields are mutually exclusive with the cron fields
      (each task picks one mode via ``kind``).

    * :class:`ScheduleHandler` -- the *what*. ``handler_id`` is the
      dispatch key the scheduler uses to resolve a registered callable
      on :meth:`OIagentCoworkerSelfWakeScheduler.tick`. ``payload`` is
      JSON-serializable so the envelope log can replay registered tasks
      without pickling arbitrary Python objects.

    * :class:`ScheduledTask` -- a single registered unit. ``task_id`` is
      uuid4 hex; ``enabled`` controls eligibility for ``tick``;
      ``last_status`` is updated after every fire so the read side can
      surface failures without traversing envelopes.

    * :class:`TaskFireEnvelope` -- one line in the append-only log. The
      six ``action`` values mirror the lifecycle operations
      (``register`` / ``tick_fire`` / ``succeed`` / ``fail`` /
      ``cancel`` / ``disable`` / ``enable``). ``envelope_id`` is the
      monotonically-increasing sequence number that doubles as the
      resume cursor; ``task`` carries the full task payload only for
      ``register`` envelopes (later transitions derive state from the
      in-memory index).

    * :class:`TaskQuery` -- explicit read-side filter. Empty
      ``frozenset`` fields mean "no constraint on this dimension";
      ``after_id`` is the durable resume cursor used by
      :meth:`OIagentCoworkerSelfWakePersistence.replay`.

Frozen-by-default discipline:

  Every dataclass is decorated with ``@dataclass(frozen=True)`` so
  ``dataclasses.asdict`` round-trips cleanly through the JSONL
  persistence layer, and so callers cannot mutate scheduler state by
  reaching into returned instances. The only mutable data structure
  is the ``metadata: dict[str, Any]`` field (a stdlib
  ``field(default_factory=dict)``); this is the one slot that must
  carry free-form JSON-serializable extension keys, and the service
  treats it as opaque.

Anti-flattery boundary (see plan §3.3 / §8.3):
    - No ``import openworker`` anywhere in this file.
    - No ``croniter`` / APScheduler / asyncio daemon runtime.
    - No Slack / GitHub / Linear / Notion / Calendar connectors.
    - Borrowed design only (frozen dataclass + envelope + cron field
      ANY-match semantics), not runtime integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = [
    "ScheduleHandler",
    "ScheduleSpec",
    "ScheduledTask",
    "TaskFireEnvelope",
    "TaskQuery",
    "TriggerKind",
    "TriggerStatus",
]


class TriggerKind(str, Enum):
    """Four-kind trigger-mode discriminator.

    Attributes:
        CRON: Standard 5-field cron expression (minute / hour /
            day-of-month / month / day-of-week). Each field is a
            ``frozenset[int]`` of allowed values; ``None`` means
            "any value matches". The scheduler evaluates ANY-match
            semantics against the trigger's :class:`datetime`.
        INTERVAL: Repeat every ``interval_seconds``. First fire is
            ``initial_delay_seconds`` after registration.
        MANUAL: One-shot, fired only by explicit dispatcher action
            (not by the periodic ``tick``). Used for ad-hoc operator
            triggers.
        ONCE: One-shot at ``fire_at``. The scheduler marks the task
            ``SUCCEEDED`` once it fires; subsequent ticks are no-ops.
    """

    CRON = "cron"
    INTERVAL = "interval"
    MANUAL = "manual"
    ONCE = "once"


class TriggerStatus(str, Enum):
    """Six-state lifecycle enum.

    Attributes:
        PENDING: Registered and waiting for the first fire (or the
            first ``initial_delay_seconds`` elapse for INTERVAL).
        RUNNING: Currently executing inside a :class:`tick` cycle.
        SUCCEEDED: Last fire returned without an exception.
        FAILED: Last fire raised an exception; ``last_error`` carries
            the textual error.
        CANCELLED: Permanently cancelled by an explicit
            :meth:`OIagentCoworkerSelfWakeScheduler.cancel` call.
            Cannot be re-enabled.
        DISABLED: Temporarily disabled by an explicit
            :meth:`OIagentCoworkerSelfWakeScheduler.disable` call.
            Re-enable via :meth:`enable`.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DISABLED = "disabled"


@dataclass(frozen=True)
class ScheduleSpec:
    """Immutable trigger specification. One kind per task.

    Cron fields are ``frozenset[int] | None``. ``None`` is the
    wildcard ("any value matches") and lets the trigger pattern
    ``"every minute"`` simply leave all five fields as ``None``. The
    dataclass stays hashable under ``@dataclass(frozen=True)`` because
    ``frozenset`` is itself immutable.

    Attributes:
        kind: One of :class:`TriggerKind`.
        cron_minute: Cron minute field (0-59). ``None`` = any.
        cron_hour: Cron hour field (0-23). ``None`` = any.
        cron_day: Cron day-of-month field (1-31). ``None`` = any.
        cron_month: Cron month field (1-12). ``None`` = any.
        cron_dow: Cron day-of-week field (0-6, Monday=0). ``None`` = any.
        interval_seconds: Interval between fires for ``kind=INTERVAL``.
            Must be >= 1 when set; ``None`` otherwise.
        fire_at: One-shot trigger instant for ``kind=ONCE`` (UTC).
            ``None`` otherwise.
        initial_delay_seconds: Seconds to wait after registration
            before the first fire. ``0`` means "fire as soon as
            conditions allow".
    """

    kind: TriggerKind
    cron_minute: frozenset[int] | None = None
    cron_hour: frozenset[int] | None = None
    cron_day: frozenset[int] | None = None
    cron_month: frozenset[int] | None = None
    cron_dow: frozenset[int] | None = None
    interval_seconds: int | None = None
    fire_at: datetime | None = None
    initial_delay_seconds: int = 0


@dataclass(frozen=True)
class ScheduleHandler:
    """The *what* of a scheduled task -- dispatch key + JSON-safe payload.

    The handler is resolved by the scheduler from a registry
    pre-populated via
    :meth:`OIagentCoworkerSelfWakeScheduler.set_handler`. The
    ``handler_id`` MUST be present in the registry at registration time
    or :class:`OIagentCoworkerSelfWakeUnknownHandlerError` is raised.

    Attributes:
        handler_id: Dispatch key (e.g. ``"inbox.aggregate_notifications"``).
            Maps to a ``Callable[[dict[str, Any]], None]`` registered
            via :meth:`OIagentCoworkerSelfWakeScheduler.set_handler`.
        payload: JSON-serializable kwargs dict handed to the handler on
            each fire. MUST contain only ``str | int | float | bool |
            None | dict | list`` (no arbitrary Python objects).
    """

    handler_id: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduledTask:
    """A registered task. ``task_id`` is uuid4 hex.

    Attributes:
        task_id: Globally-unique identifier (uuid4 hex).
        name: Short (<= 100 chars) human-readable label.
        schedule: :class:`ScheduleSpec` -- when to fire.
        handler: :class:`ScheduleHandler` -- what to fire.
        created_at: UTC datetime when the task was registered.
        enabled: ``False`` makes the task ineligible for fire under
            ``tick``. Re-enable via :meth:`OIagentCoworkerSelfWakeScheduler.enable`.
        last_status: Last observed :class:`TriggerStatus`. Updated
            after every fire; dashboards use this for last-seen state.
        last_fired_at: UTC datetime of the most recent fire attempt;
            ``None`` if the task has never fired.
        last_error: Stringified error from the most recent failed
            fire; ``None`` if the task has not failed.
        fire_count: Cumulative number of fire attempts (success or
            failure). Useful for dashboards and rate-limit math.
        metadata: Free-form JSON-serializable extension dict.
    """

    task_id: str
    name: str
    schedule: ScheduleSpec
    handler: ScheduleHandler
    created_at: datetime
    enabled: bool = True
    last_status: TriggerStatus = TriggerStatus.PENDING
    last_fired_at: datetime | None = None
    last_error: str | None = None
    fire_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskFireEnvelope:
    """One line in the append-only fire log.

    The log is per-task (one envelope per state transition). On
    restart, :meth:`OIagentCoworkerSelfWakeScheduler._rebuild_from_disk`
    walks the log in order and applies the envelopes to rebuild the
    in-memory ``_tasks`` index.

    Attributes:
        envelope_id: Monotonically-increasing counter assigned by the
            service at write-time. Doubles as the durable resume cursor.
        timestamp: UTC datetime when the envelope was constructed.
        task_id: The task the envelope acts on.
        action: One of ``"register"`` / ``"tick_fire"`` / ``"succeed"``
            / ``"fail"`` / ``"cancel"`` / ``"disable"`` / ``"enable"``.
        task: The full :class:`ScheduledTask` for ``"register"``
            envelopes; ``None`` otherwise (the latest state is
            reconstructed from the in-memory index on replay).
        error: Error text on ``"fail"`` envelopes; ``None`` otherwise.
        actor: Identity that triggered the envelope (``"user"``,
            ``"system"``, ``"tick"``, ``"cli"`` ...).
        metadata: Free-form JSON-serializable auxiliary dict.
    """

    envelope_id: int
    timestamp: datetime
    task_id: str
    action: str
    task: ScheduledTask | None
    error: str | None
    actor: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskQuery:
    """Explicit read-side filter for :meth:`OIagentCoworkerSelfWakeScheduler.query`.

    Empty ``frozenset`` fields mean "no constraint on this dimension".
    Using a dataclass (instead of ``**kwargs``) lets callers compose
    queries programmatically and keeps the persistence-layer cursor
    contract stable.

    Attributes:
        enabled_only: If ``True``, only enabled tasks are returned.
        handler_ids: Filter by :class:`ScheduleHandler.handler_id`.
            Empty = all handler_ids.
        statuses: Filter by :class:`TriggerStatus`. Empty = all statuses.
        limit: Maximum number of tasks returned. ``1000`` by default;
            the service clamps to ``[1, 1_000_000]``.
        after_id: Resume cursor; only tasks whose ``envelope_id`` of
            the most recent envelope is strictly greater are returned.
            Default ``0`` returns the entire window.
    """

    enabled_only: bool = False
    handler_ids: frozenset[str] = field(default_factory=frozenset)
    statuses: frozenset[TriggerStatus] = field(default_factory=frozenset)
    limit: int = 1000
    after_id: int = 0
