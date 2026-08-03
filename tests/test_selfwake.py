# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_selfwake.py (new file)
#   Upstream commit:  not present (W2-3.3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W2-3.3; tests the W2-3.1 (models +
#     scheduler) + W2-3.2 (persistence) shipped surface.
#   - 21 tests, no external deps beyond pytest. Pure synchronous
#     service exercised under pytest's tmp_path fixtures for
#     cross-platform safety.
#   - Mirrors the section structure of test_inbox.py: models
#     (Section A) -> scheduler CRUD (B) -> tick (C) -> lifecycle
#     (D) -> query/count (E) -> persistence (F) -> E2E (G).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Comprehensive tests for oiagent_coworker.selfwake (W2-3.3).

Covers the W2-3.1 (models + scheduler) + W2-3.2 (persistence) ship
surface:

  * Section A -- Models / dataclass invariants (2 tests)
  * Section B -- Scheduler basic CRUD (4 tests)
  * Section C -- tick() synchronous API + trigger evaluation (4 tests)
  * Section D -- cancel / disable / enable lifecycle (3 tests)
  * Section E -- query + count (3 tests)
  * Section F -- Persistence + restart replay (3 tests)
  * Section G -- End-to-end (2 tests)

Total: 21 tests, no external deps beyond pytest.

Anti-flattery boundary (see plan §3.3 / §8.3):
    - No ``import openworker`` anywhere in this file.
    - No Slack / GitHub / Linear / Notion / Calendar connector stubs.
    - No ``croniter`` / APScheduler / asyncio / background thread runtime.
    - Borrowed design only (test surface + envelope shape), not runtime.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.audit import AuditDecision
from oiagent_coworker.selfwake import (
    OIagentCoworkerSelfWakePersistence,
    OIagentCoworkerSelfWakeScheduler,
    OIagentCoworkerSelfWakeUnknownHandlerError,
    ScheduledTask,
    ScheduleHandler,
    ScheduleSpec,
    TaskFireEnvelope,
    TaskQuery,
    TriggerKind,
    TriggerStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _CapturedAudit:
    """Captured subset of an AuditDecision for selfwake assertions.

    The W2-3.1a ship widened ``AuditKind`` to include ``"selfwake"`` but
    keeps all the action / task / envelope_id info inside
    ``metadata`` to avoid coupling the audit module to the selfwake
    import DAG. Tests that need the action name read it from
    ``metadata['selfwake_action']`` -- same pattern as
    ``test_inbox.py`` / ``test_compat_audit_facade.py``.
    """

    decision: AuditDecision


@dataclass
class _CapturedCall:
    """One invocation of a registered handler callable."""

    payload: dict[str, Any]


@pytest.fixture
def captured_audit() -> list[_CapturedAudit]:
    return []


@pytest.fixture
def audit_sink(
    captured_audit: list[_CapturedAudit],
) -> Callable[[AuditDecision], None]:
    def sink(decision: AuditDecision) -> None:
        captured_audit.append(_CapturedAudit(decision=decision))

    return sink


@pytest.fixture
def handler_calls() -> list[_CapturedCall]:
    return []


@pytest.fixture
def fixed_clock() -> Callable[[], datetime]:
    """Pin the scheduler's clock to a deterministic UTC datetime.

    The default datetime is 2026-08-02 12:00:00 UTC, matching the inbox
    tests' canonical "now" so cross-suite debugging is straightforward.
    Individual tests override the returned callable when they need a
    different "now" for trigger evaluation.
    """
    fixed = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path / "selfwake.jsonl"


@pytest.fixture
def scheduler(
    storage_path: Path,
    audit_sink: Callable[[AuditDecision], None],
    fixed_clock: Callable[[], datetime],
) -> OIagentCoworkerSelfWakeScheduler:
    return OIagentCoworkerSelfWakeScheduler(
        storage_path=storage_path,
        audit_sink=audit_sink,
        clock=fixed_clock,
    )


def _make_interval_task(
    handler_id: str = "noop.handler",
    interval_seconds: int = 60,
    initial_delay_seconds: int = 0,
    metadata: dict[str, Any] | None = None,
) -> tuple[ScheduleSpec, ScheduleHandler]:
    """Build a (spec, handler) pair for an INTERVAL trigger."""
    spec = ScheduleSpec(
        kind=TriggerKind.INTERVAL,
        interval_seconds=interval_seconds,
        initial_delay_seconds=initial_delay_seconds,
    )
    handler = ScheduleHandler(handler_id=handler_id, payload={"k": "v"})
    return spec, handler


def _make_cron_task(
    minute: frozenset[int],
    name: str = "cron-task",
    handler_id: str = "noop.handler",
) -> tuple[ScheduleSpec, ScheduleHandler]:
    """Build a (spec, handler) pair for a CRON trigger pinned to a minute set."""
    spec = ScheduleSpec(
        kind=TriggerKind.CRON,
        cron_minute=minute,
    )
    handler = ScheduleHandler(handler_id=handler_id, payload={"name": name})
    return spec, handler


# ===========================================================================
# Section A: Models / dataclass invariants (2 tests)
# ===========================================================================


def test_models_dataclasses_are_frozen() -> None:
    """All 7 selfwake dataclasses are ``@dataclass(frozen=True)``; any field
    reassignment raises ``FrozenInstanceError``. The 7 are: ``ScheduleSpec``,
    ``ScheduleHandler``, ``ScheduledTask``, ``TaskFireEnvelope``,
    ``TaskQuery``, and (counted via instantiation) two enum discriminators
    which are not dataclasses -- the 7-dataclass count above covers the
    dataclass surface; this test exercises the 5 actual dataclasses plus
    the dataclass-shape fields (TaskQuery, ScheduleSpec, ScheduleHandler,
    ScheduledTask, TaskFireEnvelope)."""
    spec = ScheduleSpec(kind=TriggerKind.MANUAL)
    handler = ScheduleHandler(handler_id="h")
    task = ScheduledTask(
        task_id="t1",
        name="n",
        schedule=spec,
        handler=handler,
        created_at=datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC),
    )
    envelope = TaskFireEnvelope(
        envelope_id=1,
        timestamp=datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC),
        task_id="t1",
        action="register",
        task=task,
        error=None,
        actor="user",
    )
    query = TaskQuery()

    for instance, attr in (
        (spec, "kind"),
        (handler, "handler_id"),
        (task, "name"),
        (envelope, "action"),
        (query, "limit"),
    ):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, attr, "mutated")  # type: ignore[misc]

    # dataclasses.replace is the supported escape hatch.
    replaced = dataclasses.replace(task, name="replaced")
    assert task.name == "n"
    assert replaced.name == "replaced"
    assert replaced.task_id == task.task_id


def test_trigger_kind_and_status_are_str_enum() -> None:
    """``TriggerKind`` and ``TriggerStatus`` are ``str, Enum`` subclasses; each
    member equals its string value AND ``isinstance(member, str) is True``.
    Members: 4 TriggerKind (cron/interval/manual/once), 6 TriggerStatus
    (pending/running/succeeded/failed/cancelled/disabled)."""
    # TriggerKind
    assert TriggerKind.CRON == "cron"
    assert TriggerKind.INTERVAL == "interval"
    assert TriggerKind.MANUAL == "manual"
    assert TriggerKind.ONCE == "once"
    assert isinstance(TriggerKind.CRON, str)
    kind_values = [k.value for k in TriggerKind]
    assert kind_values == ["cron", "interval", "manual", "once"]
    assert len(set(kind_values)) == 4

    # TriggerStatus
    assert TriggerStatus.PENDING == "pending"
    assert TriggerStatus.RUNNING == "running"
    assert TriggerStatus.SUCCEEDED == "succeeded"
    assert TriggerStatus.FAILED == "failed"
    assert TriggerStatus.CANCELLED == "cancelled"
    assert TriggerStatus.DISABLED == "disabled"
    assert isinstance(TriggerStatus.PENDING, str)
    status_values = [s.value for s in TriggerStatus]
    assert status_values == [
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "disabled",
    ]
    assert len(set(status_values)) == 6


# ===========================================================================
# Section B: Scheduler - basic CRUD (4 tests)
# ===========================================================================


def test_register_creates_task_with_pending_status(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``register`` returns a :class:`ScheduledTask` whose ``last_status``
    is PENDING, ``fire_count == 0``, ``last_fired_at is None``, and
    ``enabled is True`` by default. The task is retrievable via ``get``."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=60)
    task = scheduler.register(name="register-pending", schedule=spec, handler=handler)

    assert isinstance(task, ScheduledTask)
    assert task.last_status == TriggerStatus.PENDING
    assert task.fire_count == 0
    assert task.last_fired_at is None
    assert task.last_error is None
    assert task.enabled is True
    assert task.name == "register-pending"

    # The scheduler holds the same instance -- ``register`` returns a
    # reference to the value that lives in the in-memory index.
    fetched = scheduler.get(task.task_id)
    assert fetched is task


def test_register_emits_selfwake_envelope_with_kind(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
    captured_audit: list[_CapturedAudit],
) -> None:
    """``register`` emits one :class:`AuditDecision` whose ``kind ==
    'selfwake'`` and whose ``metadata['selfwake_action'] == 'register'``.

    Note: W2-3.1a widened the closed ``AuditKind`` Literal to include
    ``"selfwake"`` and the selfwake module populates the action via
    ``metadata['selfwake_action']`` to keep audit.py free of an upward
    import edge to selfwake. This test pins both behaviours.
    """
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=60)
    task = scheduler.register(name="emit-audit", schedule=spec, handler=handler)

    # Exactly one audit envelope for the register call.
    assert len(captured_audit) == 1
    decision = captured_audit[0].decision
    assert decision.kind == "selfwake"
    assert decision.metadata.get("selfwake_action") == "register"
    assert decision.metadata.get("task_id") == task.task_id
    assert decision.metadata.get("actor") == "user"
    # The envelope_id slot is also present.
    assert decision.metadata.get("envelope_id") == 1


def test_register_unknown_handler_raises(
    scheduler: OIagentCoworkerSelfWakeScheduler,
) -> None:
    """Calling ``register`` with a ``ScheduleHandler(handler_id='unknown')``
    BEFORE registering the handler via :meth:`set_handler` raises
    :class:`OIagentCoworkerSelfWakeUnknownHandlerError`, which inherits
    from :class:`KeyError`."""
    spec, handler = _make_interval_task(handler_id="never-registered")
    with pytest.raises(OIagentCoworkerSelfWakeUnknownHandlerError) as excinfo:
        scheduler.register(name="will-fail", schedule=spec, handler=handler)
    # Subclass of KeyError -- ``isinstance(e, KeyError) is True``.
    assert isinstance(excinfo.value, KeyError)
    assert excinfo.value.handler_id == "never-registered"

    # Register the handler first, then the same call succeeds.
    scheduler.set_handler("never-registered", lambda payload: None)
    task = scheduler.register(name="now-ok", schedule=spec, handler=handler)
    assert task.handler.handler_id == "never-registered"


def test_register_and_get_round_trip(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``register`` then ``get`` round-trips the same task with matching
    name / schedule / handler / metadata. ``get`` on an unknown id
    returns ``None`` (does not raise)."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=120, initial_delay_seconds=5)
    task = scheduler.register(
        name="round-trip",
        schedule=spec,
        handler=handler,
        metadata={"k1": "v1", "k2": 2},
    )

    fetched = scheduler.get(task.task_id)
    assert fetched is not None
    assert fetched.task_id == task.task_id
    assert fetched.name == "round-trip"
    assert fetched.schedule == spec
    assert fetched.handler == handler
    assert fetched.metadata == {"k1": "v1", "k2": 2}

    # get on an unknown id returns None without raising.
    assert scheduler.get("not-a-real-uuid") is None
    assert scheduler.get("") is None


# ===========================================================================
# Section C: tick() synchronous API + trigger evaluation (4 tests)
# ===========================================================================


def test_tick_interval_fires_handler(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
    captured_audit: list[_CapturedAudit],
) -> None:
    """``tick`` invokes the registered handler for an INTERVAL task once
    ``initial_delay_seconds`` has elapsed. Handler exceptions are
    caught by the scheduler and surface as a ``fail`` envelope; they
    DO NOT propagate to the caller of ``tick``.

    Wire a counter handler that raises ``ValueError('test_error')`` so
    the error path is exercised in the same test.
    """
    call_count = {"n": 0}

    def _handler(payload: dict[str, Any]) -> None:
        call_count["n"] += 1
        handler_calls.append(_CapturedCall(payload))
        raise ValueError("test_error")

    scheduler.set_handler("boom.handler", _handler)
    spec, handler = _make_interval_task(
        handler_id="boom.handler", interval_seconds=60, initial_delay_seconds=0
    )
    task = scheduler.register(name="interval-boom", schedule=spec, handler=handler)

    # tick uses the injected clock (2026-08-02 12:00:00 UTC) -- the
    # initial_delay is 0 and interval is 60s, so the first fire
    # satisfies (now - created_at) >= initial_delay AND the first
    # fire's gate is ``elapsed_since_created >= initial_delay_seconds``.
    results = scheduler.tick()
    assert call_count["n"] == 1
    # Handler was invoked with a copy of the registered payload.
    assert handler_calls[0].payload == {"k": "v"}
    # The fail path returns (task, error) -- the error is the
    # repr'd exception which contains the message.
    assert len(results) == 1
    fired_task, error_text = results[0]
    assert fired_task.task_id == task.task_id
    assert error_text is not None
    assert "test_error" in error_text
    # The task's last_status is FAILED with last_error set.
    assert fired_task.last_status == TriggerStatus.FAILED
    assert "test_error" in (fired_task.last_error or "")

    # The exception did NOT propagate -- tick returned cleanly.
    # Audit fan-out: register (1) + tick_fire (1) + fail (1) = 3
    # envelopes, all with kind='selfwake'.
    actions = [
        d.decision.metadata.get("selfwake_action") for d in captured_audit
    ]
    assert actions == ["register", "tick_fire", "fail"]
    assert all(d.decision.kind == "selfwake" for d in captured_audit)


def test_tick_cron_any_field_match_fires(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
    handler_calls: list[_CapturedCall],
) -> None:
    """CRON trigger uses ANY-match semantics: ``tick`` fires the handler
    iff ``now.minute in cron_minute``. The other four fields default
    to ``None`` (wildcard "any value matches").

    Inject a clock factory so the scheduler sees a minute=15 instant
    (fires) and a minute=14 instant (does not fire).
    """
    fixed_now_15 = datetime(2026, 8, 2, 12, 15, 0, tzinfo=UTC)
    clock_15 = lambda: fixed_now_15
    scheduler = OIagentCoworkerSelfWakeScheduler(
        storage_path=tmp_path / "selfwake.jsonl",
        audit_sink=audit_sink,
        clock=clock_15,
    )
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_cron_task(
        minute=frozenset({0, 15, 30, 45}), handler_id="noop.handler"
    )
    task = scheduler.register(name="cron-fires-at-15", schedule=spec, handler=handler)

    # First tick at minute=15 -- should fire.
    results = scheduler.tick()
    assert len(results) == 1
    assert results[0][0].task_id == task.task_id
    assert results[0][0].last_status == TriggerStatus.SUCCEEDED
    assert len(handler_calls) == 1

    # Now move the clock to minute=14 -- should NOT fire. We rebuild a
    # fresh scheduler instance because ``tick()`` without ``now`` uses
    # the injected clock factory; passing ``now=`` directly is the
    # deterministic path.
    no_fire = scheduler.tick(now=datetime(2026, 8, 2, 12, 14, 0, tzinfo=UTC))
    assert no_fire == []
    # Handler is still only invoked once (the minute=15 fire).
    assert len(handler_calls) == 1


def test_tick_manual_kind_never_auto_fires(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``TriggerKind.MANUAL`` triggers never fire via ``tick`` -- the
    contract is that MANUAL tasks need an explicit dispatcher action
    (not yet exposed in the W2-3.1 public API; the public surface is
    :meth:`tick` only). Multiple ``tick`` calls leave the MANUAL task
    in PENDING with ``fire_count == 0``."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    _spec, handler = _make_interval_task(handler_id="noop.handler")
    # Re-target the spec.kind to MANUAL while keeping the (unused)
    # interval_seconds field as None.
    manual_spec = ScheduleSpec(kind=TriggerKind.MANUAL)
    task = scheduler.register(name="manual-never", schedule=manual_spec, handler=handler)

    for _ in range(3):
        results = scheduler.tick()
        assert results == []

    fetched = scheduler.get(task.task_id)
    assert fetched is not None
    assert fetched.last_status == TriggerStatus.PENDING
    assert fetched.fire_count == 0
    assert fetched.last_fired_at is None
    # Handler never invoked.
    assert handler_calls == []


def test_tick_once_fires_then_dormant(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
    handler_calls: list[_CapturedCall],
) -> None:
    """``TriggerKind.ONCE`` triggers fire exactly once when ``fire_at <=
    now``; subsequent ticks leave the task dormant. The scheduler
    records ``last_status == SUCCEEDED`` after the first fire, and
    the task is no longer in the fire-eligible set."""
    fire_at = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    # The clock is pinned to fire_at so the first tick fires.
    clock = lambda: fire_at
    scheduler = OIagentCoworkerSelfWakeScheduler(
        storage_path=tmp_path / "selfwake.jsonl",
        audit_sink=audit_sink,
        clock=clock,
    )
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    once_spec = ScheduleSpec(kind=TriggerKind.ONCE, fire_at=fire_at)
    handler = ScheduleHandler(handler_id="noop.handler", payload={"k": "v"})
    task = scheduler.register(name="once-fire", schedule=once_spec, handler=handler)

    # First tick at fire_at -- should fire.
    results = scheduler.tick()
    assert len(results) == 1
    assert results[0][0].last_status == TriggerStatus.SUCCEEDED
    assert len(handler_calls) == 1

    # Second tick at fire_at + 1 minute -- the task has last_fired_at
    # set, so the ONCE gate short-circuits to False. No additional
    # fire. We pass ``now=`` directly to advance the clock without
    # rebuilding the scheduler.
    later = scheduler.tick(now=fire_at + timedelta(minutes=1))
    assert later == []
    # Handler invoked exactly once.
    assert len(handler_calls) == 1

    # The task still exists in the index but is dormant.
    fetched = scheduler.get(task.task_id)
    assert fetched is not None
    assert fetched.last_status == TriggerStatus.SUCCEEDED
    assert fetched.fire_count == 1
    assert fetched.last_fired_at is not None


# ===========================================================================
# Section D: cancel / disable / enable lifecycle (3 tests)
# ===========================================================================


def test_cancel_marks_task_cancelled_and_emits_envelope(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
    captured_audit: list[_CapturedAudit],
) -> None:
    """``cancel(task_id)`` flips ``last_status`` to CANCELLED and emits a
    ``selfwake`` audit envelope with ``metadata['selfwake_action'] ==
    'cancel'``. The task remains in the in-memory index so a future
    query can still surface it (with CANCELLED status)."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=60)
    task = scheduler.register(name="to-cancel", schedule=spec, handler=handler)

    # Audit baseline: 1 'register' envelope.
    assert len(captured_audit) == 1
    assert captured_audit[0].decision.metadata.get("selfwake_action") == "register"

    # Cancel succeeds and emits the cancel envelope.
    assert scheduler.cancel(task.task_id) is True
    fetched = scheduler.get(task.task_id)
    assert fetched is not None
    assert fetched.last_status == TriggerStatus.CANCELLED

    actions = [
        d.decision.metadata.get("selfwake_action") for d in captured_audit
    ]
    assert actions == ["register", "cancel"]
    cancel_envelope = captured_audit[1].decision
    assert cancel_envelope.kind == "selfwake"
    assert cancel_envelope.metadata.get("task_id") == task.task_id
    assert cancel_envelope.metadata.get("actor") == "user"

    # Cancelled tasks are no longer eligible for tick.
    assert scheduler.tick() == []


def test_disable_then_enable_restores_task(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``disable(task_id)`` sets ``enabled=False``; the task disappears
    from ``query(enabled_only=True)`` but remains in the index.
    ``enable(task_id)`` flips ``enabled`` back to ``True`` and the
    task reappears under ``enabled_only=True``."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=60)
    task = scheduler.register(name="toggle", schedule=spec, handler=handler)

    # Initially enabled and visible.
    visible = scheduler.query(TaskQuery(enabled_only=True))
    assert any(t.task_id == task.task_id for t in visible)
    full = scheduler.query()
    assert any(t.task_id == task.task_id for t in full)

    # Disable -> the task drops out of enabled_only.
    assert scheduler.disable(task.task_id) is True
    assert scheduler.query(TaskQuery(enabled_only=True)) == []
    # But the full query still surfaces it (last_status=PENDING or
    # unchanged -- disable does NOT mutate last_status).
    all_tasks = scheduler.query()
    assert any(t.task_id == task.task_id for t in all_tasks)
    fetched_disabled = scheduler.get(task.task_id)
    assert fetched_disabled is not None
    assert fetched_disabled.enabled is False

    # Enable -> the task reappears under enabled_only.
    assert scheduler.enable(task.task_id) is True
    visible_after = scheduler.query(TaskQuery(enabled_only=True))
    assert any(t.task_id == task.task_id for t in visible_after)
    fetched_enabled = scheduler.get(task.task_id)
    assert fetched_enabled is not None
    assert fetched_enabled.enabled is True


def test_cancel_then_enable_returns_false(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``cancel`` is a one-way valve: a CANCELLED task cannot be
    re-enabled, and cancelling an already-cancelled task is a no-op
    ``False`` (idempotent semantics on the second call). The first
    ``cancel`` returns ``True``; the second ``cancel`` AND ``enable``
    on the cancelled task both return ``False``."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=60)
    task = scheduler.register(name="one-way", schedule=spec, handler=handler)

    # First cancel succeeds.
    assert scheduler.cancel(task.task_id) is True
    fetched = scheduler.get(task.task_id)
    assert fetched is not None
    assert fetched.last_status == TriggerStatus.CANCELLED

    # Idempotent: second cancel is False.
    assert scheduler.cancel(task.task_id) is False

    # CANCELLED is terminal: enable on a cancelled task returns False
    # and does NOT mutate the task.
    assert scheduler.enable(task.task_id) is False
    fetched_after = scheduler.get(task.task_id)
    assert fetched_after is not None
    assert fetched_after.last_status == TriggerStatus.CANCELLED

    # cancel / enable on an unknown id also returns False.
    assert scheduler.cancel("never-existed") is False
    assert scheduler.enable("never-existed") is False


# ===========================================================================
# Section E: query + count (3 tests)
# ===========================================================================


def test_query_with_handler_ids_filter(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``query(TaskQuery(handler_ids=frozenset({'h1'})))`` returns exactly
    the tasks whose ``ScheduleHandler.handler_id`` is in the filter set.
    An empty ``handler_ids`` set means "no constraint on this
    dimension"."""
    # Two distinct handler_ids, one of them registered twice.
    scheduler.set_handler("h1", lambda payload: handler_calls.append(_CapturedCall(payload)))
    scheduler.set_handler("h2", lambda payload: handler_calls.append(_CapturedCall(payload)))

    for name, h_id in [("t1", "h1"), ("t2", "h2"), ("t3", "h1")]:
        scheduler.register(
            name=name,
            schedule=ScheduleSpec(kind=TriggerKind.MANUAL),
            handler=ScheduleHandler(handler_id=h_id),
        )

    # Filter on h1 -> 2 tasks.
    h1_only = scheduler.query(TaskQuery(handler_ids=frozenset({"h1"})))
    assert len(h1_only) == 2
    assert {t.name for t in h1_only} == {"t1", "t3"}

    # Filter on h2 -> 1 task.
    h2_only = scheduler.query(TaskQuery(handler_ids=frozenset({"h2"})))
    assert len(h2_only) == 1
    assert h2_only[0].name == "t2"

    # No filter -> 3 tasks.
    assert len(scheduler.query()) == 3


def test_query_with_statuses_filter(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``query(TaskQuery(statuses=frozenset({PENDING})))`` returns only
    tasks with ``last_status == PENDING``. Cancelling one task moves it
    out of the PENDING set, and the post-cancel query excludes it.

    Note: ``TaskQuery.statuses`` is a ``frozenset[TriggerStatus]``;
    passing a bare enum value is a type error -- the test uses
    the frozenset form throughout.
    """
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=60)
    for i in range(3):
        scheduler.register(name=f"task-{i}", schedule=spec, handler=handler)

    # All 3 are PENDING.
    pending = scheduler.query(TaskQuery(statuses=frozenset({TriggerStatus.PENDING})))
    assert len(pending) == 3
    assert all(t.last_status == TriggerStatus.PENDING for t in pending)

    # Cancel one task; it leaves the PENDING set.
    target = pending[0]
    assert scheduler.cancel(target.task_id) is True

    # Now only 2 PENDING.
    pending_after = scheduler.query(
        TaskQuery(statuses=frozenset({TriggerStatus.PENDING}))
    )
    assert len(pending_after) == 2
    assert target.task_id not in {t.task_id for t in pending_after}
    # And the cancelled task surfaces under status filter.
    cancelled = scheduler.query(
        TaskQuery(statuses=frozenset({TriggerStatus.CANCELLED}))
    )
    assert len(cancelled) == 1
    assert cancelled[0].task_id == target.task_id

    # Empty filter set means "no constraint" -- all 3 tasks visible.
    assert len(scheduler.query(TaskQuery())) == 3


def test_count_matches_query_length(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
) -> None:
    """``count()`` returns the number of tasks the corresponding
    ``query()`` would return (without hydrating ``ScheduledTask``).
    5 registered tasks; default count is 5; with ``enabled_only=False``
    (the default), count is also 5; the disabled task counts under
    the default but is excluded by ``enabled_only=True``."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(interval_seconds=60)
    tasks = []
    for i in range(5):
        tasks.append(
            scheduler.register(name=f"count-{i}", schedule=spec, handler=handler)
        )

    # Default count == len(query()).
    assert scheduler.count() == 5
    assert scheduler.count() == len(scheduler.query())

    # Disable 2 of them.
    scheduler.disable(tasks[0].task_id)
    scheduler.disable(tasks[1].task_id)

    # Default count still includes disabled.
    assert scheduler.count() == 5
    assert scheduler.count() == len(scheduler.query())
    # enabled_only=True excludes the 2 disabled.
    assert scheduler.count(TaskQuery(enabled_only=True)) == 3
    assert scheduler.count(TaskQuery(enabled_only=True)) == len(
        scheduler.query(TaskQuery(enabled_only=True))
    )

    # count() does not apply the after_id cursor (intentional -- it
    # is a coarse count, not a resume query). The contract is
    # "mirrors query without hydration"; the field is exposed for
    # future use and is not currently wired to the count() filter.
    # We assert the documented behaviour: count ignores after_id.
    assert scheduler.count(TaskQuery(after_id=999)) == 5


# ===========================================================================
# Section F: Persistence + restart replay (3 tests)
# ===========================================================================


def test_persistence_append_and_replay_round_trip(
    tmp_path: Path,
) -> None:
    """Construct a :class:`OIagentCoworkerSelfWakePersistence` directly;
    append 3 envelopes for the same task; ``replay()`` yields all 3
    in insertion order. ``last_envelope_id()`` returns the highest
    id seen on disk (3 here)."""
    path = tmp_path / "selfwake.jsonl"
    store = OIagentCoworkerSelfWakePersistence(path)
    spec, handler = _make_interval_task(interval_seconds=60)
    created_at = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    task = ScheduledTask(
        task_id="t1",
        name="persist-1",
        schedule=spec,
        handler=handler,
        created_at=created_at,
    )

    # Build 3 envelopes; envelope_id is 1, 2, 3.
    envelopes = []
    for i in range(1, 4):
        env = TaskFireEnvelope(
            envelope_id=i,
            timestamp=created_at + timedelta(seconds=i),
            task_id="t1",
            action="register" if i == 1 else "tick_fire",
            task=task if i == 1 else None,
            error=None,
            actor="user" if i == 1 else "tick",
            metadata={"step": i},
        )
        envelopes.append(env)
        store.append(env)

    # last_envelope_id returns the highest id on disk.
    assert store.last_envelope_id() == 3

    # replay() yields all 3 in insertion order.
    replayed = list(store.replay())
    assert len(replayed) == 3
    assert [e.envelope_id for e in replayed] == [1, 2, 3]
    assert [e.action for e in replayed] == ["register", "tick_fire", "tick_fire"]
    assert [e.task_id for e in replayed] == ["t1", "t1", "t1"]
    # The first envelope carries the full task payload; later ones
    # carry None (the scheduler rebuilds state from the index).
    assert replayed[0].task is not None
    assert replayed[0].task.task_id == "t1"
    assert replayed[1].task is None
    assert replayed[2].task is None


def test_persistence_atomic_rename_no_partial_files(
    tmp_path: Path,
) -> None:
    """After ``append`` on a fresh path, the on-disk file is the final
    ``selfwake.jsonl`` (no ``tmp*.jsonl`` left behind -- the writer
    uses a single ``open(mode='a') + write + fsync``, no atomic
    rename). This is the W2-3.2 surface contract: there is NO
    partial file observable, and the file is immediately readable
    by a separate process / handle."""
    path = tmp_path / "selfwake.jsonl"
    store = OIagentCoworkerSelfWakePersistence(path)
    spec, handler = _make_interval_task(interval_seconds=60)
    task = ScheduledTask(
        task_id="t1",
        name="atomic",
        schedule=spec,
        handler=handler,
        created_at=datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC),
    )
    env = TaskFireEnvelope(
        envelope_id=1,
        timestamp=datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC),
        task_id="t1",
        action="register",
        task=task,
        error=None,
        actor="user",
    )
    store.append(env)

    # The destination file exists and is non-empty.
    assert path.exists()
    assert path.stat().st_size > 0
    # No tmp / partial file is left behind. The directory listing
    # contains exactly ``selfwake.jsonl`` (and pytest's tmp_path
    # bookkeeping); the only file matching ``selfwake*.jsonl`` is the
    # final one.
    matches = sorted(p.name for p in path.parent.iterdir() if p.name.startswith("selfwake"))
    assert matches == ["selfwake.jsonl"]

    # The file is immediately readable as a single complete JSON line.
    raw = path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["envelope_id"] == 1
    assert payload["task_id"] == "t1"
    assert payload["action"] == "register"


def test_scheduler_restart_replays_envelopes(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
    handler_calls: list[_CapturedCall],
) -> None:
    """End-to-end restart: scheduler1 registers + ticks a task; the
    JSONL log captures ``register`` + ``tick_fire`` + ``succeed``
    envelopes. scheduler2 is built on the same storage_path WITHOUT
    re-registering the handler. The task is rebuilt from the log;
    ``last_status`` / ``fire_count`` / ``last_fired_at`` are restored.

    Per spec §3.3 W1 cron facade contract: handlers are in-process
    callables that the daemon owner wires explicitly via ``set_handler``
    on every startup. A task whose handler_id is no longer registered
    after restart will fail at the next ``tick`` with a clear
    ``last_error`` -- the handler is NOT replayed from disk. This
    test verifies the rebuild shape AND that the new scheduler's
    handler registry is empty after restart (no hidden re-wiring).
    """
    path = tmp_path / "selfwake.jsonl"
    # Build scheduler1 with a fixed clock so INTERVAL gate is satisfied.
    clock = lambda: datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)
    scheduler1 = OIagentCoworkerSelfWakeScheduler(
        storage_path=path,
        audit_sink=audit_sink,
        clock=clock,
    )
    scheduler1.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(handler_id="noop.handler", interval_seconds=60)
    task1 = scheduler1.register(name="restart-me", schedule=spec, handler=handler)

    # Fire once.
    results = scheduler1.tick()
    assert len(results) == 1
    assert results[0][0].last_status == TriggerStatus.SUCCEEDED
    assert results[0][0].fire_count == 1

    # Drop scheduler1 and build scheduler2 on the same path. Do NOT
    # re-register the handler -- the contract is the owner wires
    # handlers explicitly on every startup.
    del scheduler1
    scheduler2 = OIagentCoworkerSelfWakeScheduler(
        storage_path=path,
        audit_sink=audit_sink,
        clock=clock,
    )
    # Handler registry is empty after restart (no auto-replay).
    assert scheduler2._handlers == {}

    # The task is rebuilt from the JSONL with consistent state.
    fetched = scheduler2.get(task1.task_id)
    assert fetched is not None
    assert fetched.task_id == task1.task_id
    assert fetched.name == "restart-me"
    assert fetched.fire_count == 1
    assert fetched.last_status == TriggerStatus.SUCCEEDED
    assert fetched.last_fired_at is not None

    # Tick again -- because the handler is NOT registered, the next
    # fire records a fail envelope with a clear last_error referencing
    # the missing handler. The 60s interval has elapsed (now ==
    # last_fired_at, but the gate is ``(now - last_fired_at) >=
    # interval_seconds``; 0s < 60s so the gate is FALSE on the
    # second tick at the same clock). We advance the clock to make
    # the gate fire and observe the missing-handler error path.
    later_clock = lambda: datetime(2026, 8, 2, 12, 5, 0, tzinfo=UTC)
    fail_results = scheduler2.tick(now=later_clock())
    assert len(fail_results) == 1
    _, error_text = fail_results[0]
    assert error_text is not None
    assert "noop.handler" in error_text
    assert "not registered" in error_text.lower() or "not registered" in error_text


# ===========================================================================
# Section G: End-to-end (2 tests)
# ===========================================================================


def test_e2e_register_tick_audit_envelope_chain(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
    captured_audit: list[_CapturedAudit],
) -> None:
    """register -> tick (fires) -> audit pipeline receives 3 envelopes:
    ``register`` + ``tick_fire`` + ``succeed``. All envelopes carry
    ``kind='selfwake'`` and the matching ``metadata['selfwake_action']``."""
    scheduler.set_handler(
        "noop.handler", lambda payload: handler_calls.append(_CapturedCall(payload))
    )
    spec, handler = _make_interval_task(handler_id="noop.handler", interval_seconds=60)
    task = scheduler.register(name="e2e-success", schedule=spec, handler=handler)
    results = scheduler.tick()
    assert len(results) == 1
    assert results[0][0].last_status == TriggerStatus.SUCCEEDED

    # Audit fan-out: 1 register + 1 tick_fire + 1 succeed = 3 envelopes.
    actions = [d.decision.metadata.get("selfwake_action") for d in captured_audit]
    assert actions == ["register", "tick_fire", "succeed"]
    assert all(d.decision.kind == "selfwake" for d in captured_audit)
    # All envelopes reference the same task_id.
    assert all(
        d.decision.metadata.get("task_id") == task.task_id for d in captured_audit
    )
    # The task index reflects the succeeded fire.
    fetched = scheduler.get(task.task_id)
    assert fetched is not None
    assert fetched.fire_count == 1
    assert fetched.last_fired_at is not None
    # The handler was invoked exactly once.
    assert len(handler_calls) == 1
    assert handler_calls[0].payload == {"k": "v"}


def test_e2e_register_tick_fail_envelope_has_error(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_calls: list[_CapturedCall],
    captured_audit: list[_CapturedAudit],
) -> None:
    """register a handler that raises ``RuntimeError('boom')``; tick
    catches the exception and emits a ``fail`` envelope. The
    :class:`AuditDecision.error` field carries the repr'd exception
    text (which contains the message). The task's ``last_status`` is
    FAILED and ``last_error`` mirrors the audit envelope's error."""
    def _boom(payload: dict[str, Any]) -> None:
        handler_calls.append(_CapturedCall(payload))
        raise RuntimeError("boom")

    scheduler.set_handler("boom.handler", _boom)
    spec, handler = _make_interval_task(handler_id="boom.handler", interval_seconds=60)
    task = scheduler.register(name="e2e-fail", schedule=spec, handler=handler)

    results = scheduler.tick()
    assert len(results) == 1
    _, error_text = results[0]
    assert error_text is not None
    assert "boom" in error_text

    # Audit fan-out: 1 register + 1 tick_fire + 1 fail = 3 envelopes.
    actions = [d.decision.metadata.get("selfwake_action") for d in captured_audit]
    assert actions == ["register", "tick_fire", "fail"]
    # The fail envelope's AuditDecision.error carries the repr'd
    # exception -- which embeds the original message ('boom') per
    # Python's repr() for exceptions.
    fail_decision = captured_audit[-1].decision
    assert fail_decision.error is not None
    assert "boom" in fail_decision.error

    # The task's last_status is FAILED with last_error carrying the
    # same text.
    fetched = scheduler.get(task.task_id)
    assert fetched is not None
    assert fetched.last_status == TriggerStatus.FAILED
    assert fetched.last_error is not None
    assert "boom" in fetched.last_error
    # The exception did NOT propagate to the caller of tick().
    # The handler was invoked exactly once before raising.
    assert len(handler_calls) == 1
