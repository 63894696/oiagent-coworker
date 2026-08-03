# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/scheduler/__init__.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package openworker -> oiagent_coworker; replaced upstream
#     broad re-export surface with a curated public API for the
#     trigger / task / envelope dataclasses + scheduler + persistence.
#   - The upstream scheduler daemon process + handler-loader side
#     effects are dropped entirely; this __init__ only exposes the
#     three OIagent Coworker building blocks: models, persistence,
#     scheduler.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Self-wake package for OIagent Coworker (W2-3).

Public API:

    * :class:`TriggerKind`, :class:`TriggerStatus` -- enums used by
      :class:`ScheduledTask` / :class:`ScheduleSpec` and by
      :class:`TaskQuery`.
    * :class:`ScheduleSpec` -- immutable trigger specification
      (cron / interval / manual / once).
    * :class:`ScheduleHandler` -- dispatch key + JSON-safe payload.
    * :class:`ScheduledTask` -- a registered unit (frozen dataclass).
    * :class:`TaskFireEnvelope` -- one line in the append-only log.
    * :class:`TaskQuery` -- explicit read-side filter.
    * :class:`OIagentCoworkerSelfWakeScheduler` -- business core;
      owns the in-memory task index, the handler registry, and the
      audit-sink wiring.
    * :class:`OIagentCoworkerSelfWakePersistence` -- append-only
      JSONL store for envelopes.
    * :class:`OIagentCoworkerSelfWakeUnknownHandlerError` -- raised
      by :meth:`OIagentCoworkerSelfWakeScheduler.register` when the
      supplied ``handler_id`` has not been registered via
      :meth:`OIagentCoworkerSelfWakeScheduler.set_handler`.

Anti-flattery boundary (see plan §3.3):
    - No ``import openworker`` anywhere in this package.
    - No Slack / GitHub / Linear / Notion / Calendar connectors.
    - No ``croniter`` / APScheduler / asyncio daemon.
    - Borrowed design (envelope + handler registry + cron field
      ANY-match semantics), not runtime integration.
"""

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
from oiagent_coworker.selfwake.scheduler import (
    OIagentCoworkerSelfWakeScheduler,
    OIagentCoworkerSelfWakeUnknownHandlerError,
)

__all__ = [
    "OIagentCoworkerSelfWakePersistence",
    "OIagentCoworkerSelfWakeScheduler",
    "OIagentCoworkerSelfWakeUnknownHandlerError",
    "ScheduleHandler",
    "ScheduleSpec",
    "ScheduledTask",
    "TaskFireEnvelope",
    "TaskQuery",
    "TriggerKind",
    "TriggerStatus",
]
