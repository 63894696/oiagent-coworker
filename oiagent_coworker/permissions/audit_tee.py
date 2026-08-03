# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W3-3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Implements the W3-3 two-leg audit
#     tee (FanoutAuditSink) with per-leg failure isolation.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""OIagent Coworker -- W3-3 audit tee (two-leg fan-out) + wiring recipe.

This module provides :class:`FanoutAuditSink`, a two-leg audit fan-out
with per-leg failure isolation. It composes the W3-2 durable local
``AuditStreamSink`` (the primary leg, invoked FIRST) with the external
``oiagent.audit.P2_10`` audit leg (the secondary leg, invoked SECOND)
behind the sealed single-sink facade
(:class:`~oiagent_coworker.permissions.audit.OIagentCoworkerAuditFacade`).

Adjudicated design (W3-3 contract)
----------------------------------
W1 -- two-leg tee ONLY. ``FanoutAuditSink(primary, secondary)`` takes
    exactly two legs. This is NOT a generic N-leg fan-out and NOT a
    wiring-only closure: the tee is a first-class ``AuditSink``
    implementation in this library module.
W2 -- leg order + failure isolation. The primary (durable local stream)
    leg is invoked FIRST; the secondary (external P2_10) leg is invoked
    SECOND. Each leg's exception is caught independently and logged at
    WARNING; it is NEVER re-raised and NEVER breaks ``check()``. A
    failing primary still allows the secondary to run, and vice versa.
W3 -- ``stream_path`` is injected as a ``Path`` by the DEPLOYER; the
    recipe default convention is
    ``<deploy_root>/logs/audit/permission_stream.jsonl``. This library
    NEVER resolves the path itself (no vault resolution, no env vars).
W4 -- deliverable is the tee class + this wiring recipe + the wiring
    test (``tests/test_audit_tee_wiring.py``). This is NOT live
    production wiring; the deployer performs the wiring.
W5 -- window-start marker: the wiring test proves the chain
    tee -> file -> reader -> analyzer yields ``total_compared >= 1``.
W6 -- the deployment probe entry-point lives in
    ``scripts/daily_flip_probe.py`` (the only place a real clock is
    read); this library module never reads a clock.

Wiring recipe (deployment-facing; NOT executed by this module)
--------------------------------------------------------------
::

    from pathlib import Path
    from oiagent_coworker.permissions.audit import OIagentCoworkerAuditFacade
    from oiagent_coworker.permissions.audit_stream import AuditStreamSink
    from oiagent_coworker.permissions.audit_tee import FanoutAuditSink
    # from oiagent.audit import P2_10_audit_sink   # external; deployer imports (NOT in this repo)

    stream_path = Path("<deploy_root>/logs/audit/permission_stream.jsonl")
    stream_leg  = AuditStreamSink(stream_path)      # durable local leg (first)
    p2_leg      = P2_10_audit_sink(...)             # external leg (second)
    tee         = FanoutAuditSink(primary=stream_leg, secondary=p2_leg)
    facade      = OIagentCoworkerAuditFacade(sink=tee)
    # engine = OIagentCoworkerPermissionEngine(..., audit_sink=facade.for_engine())

Anti-flattery boundary (see plan §3.1 / §8.1.1):
    - No ``import openworker`` anywhere in this file.
    - No ``${OIAGENT_VAULT}`` resolution, no env-var reads, no
      ``oiagent.vault.path`` import; ``stream_path`` is injected as a
      ``Path`` by the deployer.
    - No ``datetime.now()`` -- this module never reads a clock.
    - No P2_10 import: the external leg is always an injected callable.
    - No ``min_sample`` / window / traffic assumption baked in.
    - The tee NEVER breaks ``check()``: leg exceptions are caught and
      logged, never raised.
"""

from __future__ import annotations

import logging

from oiagent_coworker.permissions.audit import AuditDecision, AuditSink

__all__ = [
    "FanoutAuditSink",
]

_LOGGER = logging.getLogger(__name__)


class FanoutAuditSink:
    """Two-leg audit fan-out with per-leg failure isolation (W3-3, W1/W2).

    Conforms to the ``AuditSink`` Protocol (``@runtime_checkable``):
    ``isinstance(tee, AuditSink)`` holds, and the tee passes the
    ``OIagentCoworkerAuditFacade(sink=tee)`` ``callable`` gate.

    The primary leg (durable local ``AuditStreamSink``) is invoked FIRST;
    the secondary leg (external P2_10) is invoked SECOND. Both legs
    receive the SAME ``AuditDecision`` envelope (identity, not a copy).

    Failure isolation (W2): each leg's exception is caught independently
    and logged at WARNING. It is NEVER re-raised and NEVER breaks
    ``check()``. A failing primary still allows the secondary to run;
    a failing secondary does not affect the already-completed primary.
    """

    def __init__(self, primary: AuditSink, secondary: AuditSink) -> None:
        """Initialize the two-leg tee.

        Args:
            primary: Durable local leg (e.g. ``AuditStreamSink``),
                invoked FIRST.
            secondary: External leg (e.g. P2_10), invoked SECOND.

        Raises:
            TypeError: If either leg is not callable.
        """
        if not callable(primary):
            raise TypeError(
                f"FanoutAuditSink primary leg must be callable, "
                f"got {type(primary).__name__}"
            )
        if not callable(secondary):
            raise TypeError(
                f"FanoutAuditSink secondary leg must be callable, "
                f"got {type(secondary).__name__}"
            )
        self._primary: AuditSink = primary
        self._secondary: AuditSink = secondary

    @property
    def primary(self) -> AuditSink:
        """The primary (durable local) leg, invoked FIRST."""
        return self._primary

    @property
    def secondary(self) -> AuditSink:
        """The secondary (external P2_10) leg, invoked SECOND."""
        return self._secondary

    def __call__(self, decision: AuditDecision) -> None:
        """Fan out one envelope to both legs with failure isolation.

        Primary is invoked FIRST, secondary SECOND. Each leg's exception
        is caught independently, logged at WARNING, and NEVER re-raised,
        so the tee never breaks ``check()``.

        Args:
            decision: The audit envelope to fan out (same object to both
                legs; identity preserved).
        """
        try:
            self._primary(decision)
        except Exception as exc:  # noqa: BLE001 -- tee must not break check()
            _LOGGER.warning(
                "FanoutAuditSink primary leg raised %s; decision dropped "
                "for the durable local leg",
                exc,
            )
        try:
            self._secondary(decision)
        except Exception as exc:  # noqa: BLE001 -- tee must not break check()
            _LOGGER.warning(
                "FanoutAuditSink secondary leg raised %s; decision dropped "
                "for the external P2_10 leg",
                exc,
            )
