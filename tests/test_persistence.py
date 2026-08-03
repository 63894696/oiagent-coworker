"""Tests for oiagent_coworker.permissions.persistence -- W2-1.3 standing rule store.

Per W2-extraction-plan §3.1 / §3.6 + §8.1.1, this file covers the
append-only JSONL store for StandingRule records:

  * Round-trip add / get / revoke
  * TTL expiry (StandingRuleExpired)
  * list_active filtering
  * Atomic purge (os.replace)
  * Corrupt-JSONL tolerance
  * Lazy parent-directory creation
  * Audit-sink fan-out

Total: 11 tests, no external deps beyond pytest.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.audit import AuditDecision
from oiagent_coworker.permissions.engine import PermissionMode
from oiagent_coworker.permissions.persistence import (
    OIagentCoworkerStandingRuleStore,
    StandingRule,
    StandingRuleExpired,
    make_default_rule,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "rules.jsonl"


@pytest.fixture
def audit_calls() -> list[AuditDecision]:
    return []


@pytest.fixture
def audit_sink(audit_calls: list[AuditDecision]):
    def _sink(decision: AuditDecision) -> None:
        audit_calls.append(decision)
    return _sink


@pytest.fixture
def store(store_path: Path, audit_sink) -> OIagentCoworkerStandingRuleStore:
    return OIagentCoworkerStandingRuleStore(
        store_path=store_path, audit_sink=audit_sink
    )


def _make_rule(
    pattern: str = "read_*",
    ttl_seconds: int = 600,
    granted_by: str = "user",
    now: datetime | None = None,
) -> StandingRule:
    return make_default_rule(
        pattern=pattern,
        mode=PermissionMode.ASYNC,
        granted_by=granted_by,
        ttl_seconds=ttl_seconds,
        now=now,
    )


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------


def test_add_and_get_round_trip(
    store: OIagentCoworkerStandingRuleStore,
    store_path: Path,
) -> None:
    """add -> get returns equal fields."""
    rule = _make_rule(pattern="read_*.py", ttl_seconds=900)
    store.add(rule)
    assert store_path.exists(), "store_path should be created on first add"

    fetched = store.get(rule.rule_id)
    assert fetched.rule_id == rule.rule_id
    assert fetched.pattern == rule.pattern
    assert fetched.mode is rule.mode
    assert fetched.created_at == rule.created_at
    assert fetched.expires_at == rule.expires_at
    assert fetched.granted_by == rule.granted_by
    assert fetched.note == rule.note


# ---------------------------------------------------------------------------
# 2. Unknown rule_id -> KeyError
# ---------------------------------------------------------------------------


def test_get_unknown_rule_raises_keyerror(
    store: OIagentCoworkerStandingRuleStore,
) -> None:
    with pytest.raises(KeyError, match="no standing rule"):
        store.get("nonexistent-rule-id")


# ---------------------------------------------------------------------------
# 3. Expired rule -> StandingRuleExpired
# ---------------------------------------------------------------------------


def test_get_expired_rule_raises_expired(
    store: OIagentCoworkerStandingRuleStore,
) -> None:
    """A rule whose expires_at is in the past raises StandingRuleExpired
    (distinct from KeyError so callers can treat expiry as a soft miss)."""
    past = datetime(2020, 1, 1, tzinfo=UTC)
    rule = _make_rule(ttl_seconds=10, now=past)
    store.add(rule)
    with pytest.raises(StandingRuleExpired):
        store.get(rule.rule_id)


# ---------------------------------------------------------------------------
# 4. Revoke unknown rule_id is silent
# ---------------------------------------------------------------------------


def test_revoke_unknown_rule_is_silent(
    store: OIagentCoworkerStandingRuleStore,
    store_path: Path,
) -> None:
    """revoke(nonexistent) is a no-op: a tombstone is appended but no
    error is raised. This is the concurrent-writer-friendly contract.
    After revocation, get() raises KeyError (the rule is logically
    gone, whether it was ever there or not)."""
    store.revoke("nonexistent-rule-id")
    assert store_path.exists()
    with pytest.raises(KeyError):
        store.get("nonexistent-rule-id")


# ---------------------------------------------------------------------------
# 5. Revoke existing rule -> get raises KeyError
# ---------------------------------------------------------------------------


def test_revoke_existing_rule_raises_keyerror(
    store: OIagentCoworkerStandingRuleStore,
) -> None:
    rule = _make_rule()
    store.add(rule)
    fetched = store.get(rule.rule_id)  # works
    assert fetched.rule_id == rule.rule_id
    store.revoke(rule.rule_id)
    with pytest.raises(KeyError, match="revoked"):
        store.get(rule.rule_id)


# ---------------------------------------------------------------------------
# 6. list_active excludes expired
# ---------------------------------------------------------------------------


def test_list_active_excludes_expired(
    store: OIagentCoworkerStandingRuleStore,
) -> None:
    now = datetime.now(UTC)
    active_rule = _make_rule(pattern="active_*", now=now)
    expired_rule = _make_rule(pattern="expired_*", now=now - timedelta(hours=2))
    store.add(active_rule)
    store.add(expired_rule)

    active = store.list_active(now=now)
    rule_ids = {r.rule_id for r in active}
    assert active_rule.rule_id in rule_ids
    assert expired_rule.rule_id not in rule_ids
    # Sorted by created_at: the expired rule has earlier created_at but
    # is excluded; only the active rule is present.
    assert len(active) == 1
    assert active[0].rule_id == active_rule.rule_id


# ---------------------------------------------------------------------------
# 7. purge_expired removes entries
# ---------------------------------------------------------------------------


def test_purge_expired_removes_entries(
    store: OIagentCoworkerStandingRuleStore,
    store_path: Path,
) -> None:
    now = datetime.now(UTC)
    active_rule = _make_rule(pattern="a", now=now)
    expired_rule = _make_rule(pattern="b", now=now - timedelta(hours=2))
    store.add(active_rule)
    store.add(expired_rule)

    removed = store.purge_expired(now=now)
    # 1 expired rule + 0 tombstones removed (no revoke was issued).
    assert removed == 1

    # The file should still exist with the active rule.
    assert store_path.exists()
    active = store.list_active(now=now)
    assert len(active) == 1
    assert active[0].rule_id == active_rule.rule_id

    # And the expired rule_id is no longer fetchable (KeyError, not
    # StandingRuleExpired -- the entry has been wiped, not just stale).
    with pytest.raises(KeyError):
        store.get(expired_rule.rule_id)


# ---------------------------------------------------------------------------
# 8. Corrupt JSONL line is tolerated
# ---------------------------------------------------------------------------


def test_jsonl_corrupt_line_tolerated(
    store: OIagentCoworkerStandingRuleStore,
    store_path: Path,
) -> None:
    """Manually inject a corrupt line; the parser logs + skips it but
    still returns the surrounding valid rules."""
    valid_rule = _make_rule(pattern="valid_*")
    store.add(valid_rule)

    # Append a corrupt line by hand; do NOT use the store API (it
    # serializes valid JSON only).
    with open(store_path, "a", encoding="utf-8") as fp:
        fp.write("this is not json{\n")

    # And another valid rule (after the corrupt one) to confirm we
    # don't bail on the first parse failure.
    second_rule = _make_rule(pattern="second_*")
    store.add(second_rule)

    # Read-back should find both valid rules; the corrupt line is
    # silently dropped.
    fetched_a = store.get(valid_rule.rule_id)
    fetched_b = store.get(second_rule.rule_id)
    assert fetched_a.pattern == "valid_*"
    assert fetched_b.pattern == "second_*"


# ---------------------------------------------------------------------------
# 9. Atomic rename survives purge crash
# ---------------------------------------------------------------------------


def test_atomic_rename_survives_purge(
    store: OIagentCoworkerStandingRuleStore,
    store_path: Path,
) -> None:
    """purge_expired uses os.replace() (atomic on POSIX + Win32) so
    the store_path is never observed as a half-written file. We
    simulate a crash mid-purge by deleting the tmp file before the
    next add; the store must self-heal on the next add().
    """
    now = datetime.now(UTC)
    store.add(_make_rule(pattern="pre-purge", now=now))
    store.add(_make_rule(pattern="expired", now=now - timedelta(hours=2)))

    # Simulate crash mid-purge: write a stale .tmp file that would
    # normally be os.replace()'d.
    tmp = store_path.with_suffix(store_path.suffix + ".tmp")
    tmp.write_text("stale\n", encoding="utf-8")

    # The next purge overwrites the .tmp atomically -- the stale
    # file is replaced with the new content.
    store.purge_expired(now=now)
    assert not tmp.exists(), "os.replace() should have consumed the .tmp"
    assert store_path.exists()

    # And the store is still usable after the simulated crash.
    fresh = _make_rule(pattern="post-purge", now=now)
    store.add(fresh)
    assert store.get(fresh.rule_id).pattern == "post-purge"

    # Cleanup: store_path.tmp should not exist after os.replace
    # succeeded.
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# 10. audit_sink called on add
# ---------------------------------------------------------------------------


def test_audit_sink_called_on_add(
    store: OIagentCoworkerStandingRuleStore,
    audit_calls: list[AuditDecision],
) -> None:
    rule = _make_rule()
    store.add(rule)
    assert len(audit_calls) >= 1
    d = audit_calls[0]
    assert d.kind == "standing_rule"
    assert d.standing_rule_action == "add"
    assert d.standing_rule is rule

    # Revoke emits a second decision.
    store.revoke(rule.rule_id)
    assert len(audit_calls) >= 2
    assert audit_calls[1].standing_rule_action == "revoke"


# ---------------------------------------------------------------------------
# 11. Lazy parent-directory creation
# ---------------------------------------------------------------------------


def test_store_path_lazy_create(
    tmp_path: Path,
    audit_sink,
) -> None:
    """__init__ does not create the store_path. The parent directory
    is created on the first add() call.
    """
    nested = tmp_path / "deep" / "nested" / "rules.jsonl"
    assert not nested.parent.exists()

    store = OIagentCoworkerStandingRuleStore(
        store_path=nested, audit_sink=audit_sink
    )
    # __init__ does not touch the filesystem.
    assert not nested.parent.exists()

    rule = _make_rule()
    store.add(rule)
    # After first add, the parent directory must exist and the file
    # must contain exactly one valid line.
    assert nested.parent.exists()
    assert nested.exists()
    with open(nested, "r", encoding="utf-8") as fp:
        lines = [ln for ln in fp.read().splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["rule_id"] == rule.rule_id
