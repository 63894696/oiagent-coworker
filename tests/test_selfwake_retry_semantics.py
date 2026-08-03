# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W2 plan §7.4 boundary ⑤ is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Boundary ⑤ (W2 plan §7.4):
#     selfwake caller-failure retry semantics. Adjudicated semantics
#     (D4, user-ratified -- pin current behaviour, no retry feature):
#     the scheduler documents "no retry, no backoff"
#     (scheduler.py:395). Runtime no-retry and durable no-event-loss
#     are TWO INDEPENDENT assertions, pinned separately: (a) at
#     runtime, an ONCE task whose handler fails is burned
#     (``last_fired_at`` set -> never re-fires) and an INTERVAL task
#     re-fires only on its interval cadence, never immediately;
#     (b) the failure envelope is durable -- a fresh scheduler instance
#     over the same JSONL log replays FAILED status + ``last_error``
#     + ``fire_count`` faithfully.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Boundary ⑤ (W2 plan §7.4) -- selfwake caller-failure retry semantics.

Adjudicated semantics (contract D4 -- pin current "no retry" design;
runtime no-retry and persistence no-loss are independent assertions):

  1. ONCE failure does not retry: a failed ONCE task has
     ``last_fired_at`` set, so subsequent ticks never re-fire it
     (the fire event is burned; ``fire_count`` stays 1).
  2. INTERVAL failure does not retry immediately: after a failure the
     next fire waits for ``interval_seconds`` to elapse from
     ``last_fired_at`` -- the failed event is NOT re-dispatched
     out-of-cadence; when the cadence arrives and the handler now
     succeeds, the task fires again (``fire_count`` 2, SUCCEEDED).
  3. Failure is durable (independent of runtime no-retry): a fresh
     scheduler over the same storage replays the ``fail`` envelope --
     FAILED status, ``last_error``, ``fire_count`` and
     ``last_fired_at`` all survive restart.
  4. No backoff accumulation: repeated ticks inside the interval window
     never produce extra fires after a failure.

All clocks are injected (``tick(now=...)``); no real sleeps.

Anti-flattery boundary (see plan §3.3):
    - No ``import openworker`` anywhere in this file.
    - Deterministic assertions only; no thresholds.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.selfwake import (
    OIagentCoworkerSelfWakeScheduler,
    ScheduleHandler,
    ScheduleSpec,
    TriggerKind,
    TriggerStatus,
)

_T0 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def _fixed_clock() -> datetime:
    return _T0


def _register_once_task(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_id: str,
    fire_at: datetime,
) -> str:
    spec = ScheduleSpec(kind=TriggerKind.ONCE, fire_at=fire_at)
    task = scheduler.register(
        name="once-task",
        schedule=spec,
        handler=ScheduleHandler(handler_id=handler_id, payload={}),
    )
    return task.task_id


def _register_interval_task(
    scheduler: OIagentCoworkerSelfWakeScheduler,
    handler_id: str,
    interval_seconds: int = 60,
) -> str:
    spec = ScheduleSpec(
        kind=TriggerKind.INTERVAL,
        interval_seconds=interval_seconds,
        initial_delay_seconds=0,
    )
    task = scheduler.register(
        name="interval-task",
        schedule=spec,
        handler=ScheduleHandler(handler_id=handler_id, payload={}),
    )
    return task.task_id


class _FlakyHandler:
    """Handler that raises on its first ``failures`` calls, then
    succeeds. Call count is observable for no-retry assertions."""

    def __init__(self, failures: int = 1) -> None:
        self.calls = 0
        self._failures = failures

    def __call__(self, payload: dict[str, Any]) -> None:
        self.calls += 1
        if self.calls <= self._failures:
            raise RuntimeError(f"boom #{self.calls}")


# ===========================================================================
# Boundary ⑤ tests
# ===========================================================================


def test_01_once_failure_is_burned_no_retry(tmp_path: Path) -> None:
    """Runtime no-retry (ONCE): a failed ONCE fire sets ``last_fired_at``;
    every later tick returns without re-firing and ``fire_count`` stays 1."""
    scheduler = OIagentCoworkerSelfWakeScheduler(
        storage_path=tmp_path / "selfwake.jsonl", clock=_fixed_clock
    )
    handler = _FlakyHandler(failures=1_000)  # always fails
    scheduler.set_handler("h.once", handler)
    task_id = _register_once_task(scheduler, "h.once", fire_at=_T0)

    fired = scheduler.tick(now=_T0)
    assert len(fired) == 1
    task, error = fired[0]
    assert task.task_id == task_id
    assert error is not None
    assert task.last_status == TriggerStatus.FAILED
    assert task.fire_count == 1
    assert task.last_error

    # Later ticks: the ONCE task never fires again (burned).
    for delta in (1, 60, 3600, 86_400):
        fired_again = scheduler.tick(now=_T0 + timedelta(seconds=delta))
        assert task_id not in {t.task_id for t, _ in fired_again}

    final = scheduler.get(task_id)
    assert final is not None
    assert final.fire_count == 1
    assert handler.calls == 1  # handler invoked exactly once, never retried


def test_02_interval_failure_retries_only_on_cadence(tmp_path: Path) -> None:
    """Runtime no-retry (INTERVAL): after a failure the task does NOT
    re-fire inside the interval window; it fires again only once
    ``interval_seconds`` has elapsed -- cadence retry, not immediate."""
    scheduler = OIagentCoworkerSelfWakeScheduler(
        storage_path=tmp_path / "selfwake.jsonl", clock=_fixed_clock
    )
    handler = _FlakyHandler(failures=1)  # first call raises, second succeeds
    scheduler.set_handler("h.interval", handler)
    task_id = _register_interval_task(scheduler, "h.interval", interval_seconds=60)

    # t0: first fire fails.
    fired = scheduler.tick(now=_T0)
    assert len(fired) == 1
    assert fired[0][0].last_status == TriggerStatus.FAILED
    assert handler.calls == 1

    # t0+30s (< interval): no re-fire -- failure is not retried immediately.
    assert scheduler.tick(now=_T0 + timedelta(seconds=30)) == []
    assert handler.calls == 1

    # t0+61s (> interval): cadence arrived; handler now succeeds.
    fired = scheduler.tick(now=_T0 + timedelta(seconds=61))
    assert len(fired) == 1
    task, error = fired[0]
    assert task.task_id == task_id
    assert error is None
    assert task.last_status == TriggerStatus.SUCCEEDED
    assert task.fire_count == 2
    assert handler.calls == 2


def test_03_failure_survives_restart_via_replay(tmp_path: Path) -> None:
    """Persistence no-loss (independent of runtime no-retry): the ``fail``
    envelope is durable -- a fresh scheduler over the same JSONL log
    replays FAILED status, ``last_error``, ``fire_count`` and
    ``last_fired_at`` faithfully."""
    storage = tmp_path / "selfwake.jsonl"
    scheduler = OIagentCoworkerSelfWakeScheduler(
        storage_path=storage, clock=_fixed_clock
    )
    handler = _FlakyHandler(failures=1_000)
    scheduler.set_handler("h.once", handler)
    task_id = _register_once_task(scheduler, "h.once", fire_at=_T0)
    scheduler.tick(now=_T0)

    before = scheduler.get(task_id)
    assert before is not None
    assert before.last_status == TriggerStatus.FAILED

    # Simulate restart: brand-new instance, same storage path.
    resumed = OIagentCoworkerSelfWakeScheduler(
        storage_path=storage, clock=_fixed_clock
    )
    after = resumed.get(task_id)
    assert after is not None
    assert after.last_status == TriggerStatus.FAILED
    assert after.last_error == before.last_error
    assert after.last_error  # error text preserved, not None/empty
    assert after.fire_count == before.fire_count == 1
    assert after.last_fired_at == before.last_fired_at


def test_04_no_backoff_extra_fires_within_interval(tmp_path: Path) -> None:
    """No-backoff pin: after a failure, many ticks inside the interval
    window produce zero additional fires -- no hidden backoff schedule,
    no silent retry burst."""
    scheduler = OIagentCoworkerSelfWakeScheduler(
        storage_path=tmp_path / "selfwake.jsonl", clock=_fixed_clock
    )
    handler = _FlakyHandler(failures=1_000)  # always fails
    scheduler.set_handler("h.interval", handler)
    task_id = _register_interval_task(scheduler, "h.interval", interval_seconds=60)

    scheduler.tick(now=_T0)  # first fire fails
    assert handler.calls == 1

    # Ten ticks well inside the 60s window: none fire.
    for step in range(1, 11):
        fired = scheduler.tick(now=_T0 + timedelta(seconds=step * 5))
        assert task_id not in {t.task_id for t, _ in fired}
    assert handler.calls == 1

    task = scheduler.get(task_id)
    assert task is not None
    assert task.fire_count == 1
    assert task.last_status == TriggerStatus.FAILED
