# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    (none -- new file)
#   Upstream commit:  not present (W2 plan §7.4 boundary ② is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file; no upstream counterpart. Boundary ② (W2 plan §7.4):
#     PermissionEngine 5-mode rapid switching must NOT implicitly clear
#     standing rules. Adjudicated semantics (D2, user-ratified): when the
#     only activity is mode-switched ``engine.check()`` calls -- no
#     explicit ``revoke`` and no TTL expiry -- every active standing rule
#     MUST remain retrievable from the store. The only legal removal
#     paths are explicit ``revoke(rule_id)``, ``purge_expired()``, or
#     natural TTL expiry; this suite pins all of them deterministically.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Boundary ② (W2 plan §7.4) -- mode-switch standing-rule stability.

Adjudicated semantics (contract D2): the permission engine's ``check()``
does not touch the standing-rule store; a rule's ``mode`` field is the
mode under which it was granted, and mode switching of the engine is
NOT a revocation event. "Erroneous clear" = an active rule disappearing
from the store purely because the engine cycled through the five
``PermissionMode`` values.

Test intent:

  1. Seed one active rule per mode (5 rules, 1h TTL) into a
     ``OIagentCoworkerStandingRuleStore``.
  2. Drive a single ``OIagentCoworkerPermissionEngine`` through 200
     alternating ``check()`` calls across all 5 modes.
  3. Assert all 5 rules are still active (``list_active``) and still
     individually retrievable (``get`` does not raise, mode preserved).
  4. Reverse pin: the ONLY ways a rule leaves the store are explicit
     ``revoke`` (``get`` -> KeyError) and expiry (``get`` ->
     ``StandingRuleExpired``; ``purge_expired`` removes it).

Anti-flattery boundary (see plan §3.1):
    - No ``import openworker`` anywhere in this file.
    - Deterministic assertions only; no thresholds, no sleeps.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.engine import (
    Action,
    OIagentCoworkerPermissionEngine,
    PermissionContext,
    PermissionMode,
)
from oiagent_coworker.permissions.persistence import (
    OIagentCoworkerStandingRuleStore,
    StandingRuleExpired,
    make_default_rule,
)

_ALL_MODES: tuple[PermissionMode, ...] = (
    PermissionMode.ASYNC,
    PermissionMode.SYNC,
    PermissionMode.PLAN,
    PermissionMode.INTERRUPT,
    PermissionMode.COMPACTION,
)

_SWITCH_ROUNDS = 200
_TTL_SECONDS = 3600  # 1h -- comfortably outlives the test run


@pytest.fixture
def audit_sink():
    """No-op audit sink (engine + store require a callable; we do not
    assert on audit envelopes in this boundary)."""
    return lambda decision: None


@pytest.fixture
def engine(tmp_path: Path, audit_sink) -> OIagentCoworkerPermissionEngine:
    return OIagentCoworkerPermissionEngine(
        workspace_root=tmp_path, audit_sink=audit_sink
    )


@pytest.fixture
def store(tmp_path: Path, audit_sink) -> OIagentCoworkerStandingRuleStore:
    return OIagentCoworkerStandingRuleStore(
        store_path=tmp_path / "standing_rules.jsonl", audit_sink=audit_sink
    )


def _seed_one_rule_per_mode(
    store: OIagentCoworkerStandingRuleStore,
) -> dict[PermissionMode, str]:
    """Add one active standing rule per PermissionMode; return
    ``{mode: rule_id}``."""
    now = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
    rule_ids: dict[PermissionMode, str] = {}
    for mode in _ALL_MODES:
        rule = make_default_rule(
            pattern=f"read_file:{mode.value}:*",
            mode=mode,
            granted_by="boundary-test",
            ttl_seconds=_TTL_SECONDS,
            now=now,
        )
        store.add(rule)
        rule_ids[mode] = rule.rule_id
    return rule_ids


# ===========================================================================
# Boundary ② tests
# ===========================================================================


def test_01_rapid_mode_switching_does_not_clear_standing_rules(
    engine: OIagentCoworkerPermissionEngine,
    store: OIagentCoworkerStandingRuleStore,
) -> None:
    """200 alternating mode-switched check() calls with no revoke/expire
    must leave all 5 seeded rules active and retrievable."""
    rule_ids = _seed_one_rule_per_mode(store)
    assert len(store.list_active()) == len(_ALL_MODES)

    action = Action(kind="read_file", target="README.md")
    for i in range(_SWITCH_ROUNDS):
        mode = _ALL_MODES[i % len(_ALL_MODES)]
        engine.check(action, PermissionContext(mode=mode, task_id="t-boundary"))

    # No rule was implicitly cleared by the mode switching.
    active = store.list_active()
    assert len(active) == len(_ALL_MODES)
    active_ids = {r.rule_id for r in active}
    assert active_ids == set(rule_ids.values())

    # Each rule is still individually retrievable, unexpired, and keeps
    # the mode under which it was granted.
    for mode, rule_id in rule_ids.items():
        rule = store.get(rule_id)  # must not raise KeyError / Expired
        assert rule.mode is mode


def test_02_explicit_revoke_is_the_legal_removal_path(
    store: OIagentCoworkerStandingRuleStore,
) -> None:
    """Reverse pin (a): ``revoke(rule_id)`` -- and only an explicit call
    like it -- removes a rule; afterwards ``get`` raises KeyError and
    ``list_active`` no longer contains the rule."""
    rule_ids = _seed_one_rule_per_mode(store)
    victim = rule_ids[PermissionMode.ASYNC]

    store.revoke(victim)

    with pytest.raises(KeyError):
        store.get(victim)
    remaining_ids = {r.rule_id for r in store.list_active()}
    assert victim not in remaining_ids
    assert len(remaining_ids) == len(_ALL_MODES) - 1


def test_03_ttl_expiry_is_the_other_legal_removal_path(
    store: OIagentCoworkerStandingRuleStore,
) -> None:
    """Reverse pin (b): a rule past its ``expires_at`` raises
    ``StandingRuleExpired`` from ``get`` and disappears from
    ``list_active`` / ``purge_expired`` -- expiry, not mode switching,
    is the only implicit removal."""
    past = datetime.now(UTC) - timedelta(seconds=10)
    expired_rule = make_default_rule(
        pattern="read_file:expired:*",
        mode=PermissionMode.SYNC,
        granted_by="boundary-test",
        ttl_seconds=5,
        now=past - timedelta(seconds=5),
    )
    assert expired_rule.expires_at < datetime.now(UTC)
    store.add(expired_rule)

    with pytest.raises(StandingRuleExpired):
        store.get(expired_rule.rule_id)
    assert expired_rule.rule_id not in {
        r.rule_id for r in store.list_active()
    }

    removed = store.purge_expired()
    assert removed >= 1
