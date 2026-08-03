# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    openworker/agent/standing_rule.py
#   Upstream commit:  01b6f83b3927e02912dda84bb392942c13ca70d1
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - Renamed package; replaced upstream SQLite backend with an
#     append-only JSONL store for crash-safe single-file writes and
#     trivial per-host rotation.
#   - Added TTL semantics driven by engine.DEFAULT_STANDING_RULE_TTL_S
#     (15min, tightened from upstream 1h per OIagent P2-10 risk profile).
#   - Added tombstone (revoke) records so revocation is also append-only
#     and survives concurrent readers.
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Append-only JSONL store for standing rules (W2-1.3).

The standing-rule data model is a small persisted dictionary from
``rule_id`` to ``StandingRule``. Upstream used SQLite; OIagent Coworker
moves to an append-only JSONL file because:

  * Crash-safety: a partial write leaves a parseable prefix instead of a
    locked DB.
  * Concurrent writers: each ``add`` is a single ``write + fsync`` --
    no row-level locking needed.
  * Trivial rotation: the on-disk file is small (<1MB for typical
    rule counts) and can be copied / archived without ceremony.

Persistence semantics:
  * Each line is a JSON object. Valid lines carry a ``StandingRule``;
    revoke records are tombstones with ``{"action": "revoke", ...}``.
  * ``add`` is ``O(1)`` amortized; ``get`` is ``O(n)`` because the
    JSONL is a forward log and the latest entry wins. With a
    single-host, <10k rules file this is well under 1ms p99.
  * ``purge_expired`` rewrites the file atomically via ``os.replace``
    to keep the on-disk size bounded.

Anti-flattery boundary (see plan §3.1):
    - No ``import openworker`` anywhere in this file.
    - No OAuth broker, MCP server runtime, or Tauri shell calls.
    - No vendor LLM SDK imports.
    - Borrowed design (data shape + revoke semantics), not runtime.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from oiagent_coworker.permissions.engine import PermissionMode

__all__ = [
    "OIagentCoworkerStandingRuleStore",
    "StandingRule",
    "StandingRuleExpired",
]

_LOGGER = logging.getLogger(__name__)


# A revoke record is identifiable by the absence of all StandingRule
# fields and the presence of an "action" key set to "revoke".
_REVOKE_ACTION = "revoke"


class StandingRuleExpired(Exception):
    """Raised when a standing rule lookup hits an expired entry.

    Distinct from ``KeyError`` so callers can treat expiry as a soft
    miss (re-prompt the user for a new rule) without catching every
    unknown-id lookup as a hard error.
    """


@dataclass(frozen=True)
class StandingRule:
    """A persisted, time-bounded permission grant.

    Attributes:
        rule_id: Globally-unique identifier (uuid4 hex).
        pattern: Glob / regex / kind matched against an action's
            ``kind`` or ``target``.
        mode: The PermissionMode the rule applies under.
        created_at: UTC datetime the rule was created.
        expires_at: UTC datetime the rule stops being honored.
        granted_by: Identity that created the rule ("user",
            "oiagent.admin", "policy.default", etc.).
        note: Free-form annotation surfaced in audit.
    """

    rule_id: str
    pattern: str
    mode: PermissionMode
    created_at: datetime
    expires_at: datetime
    granted_by: str
    note: str = ""

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return True if the rule's expires_at is strictly before now."""
        current = now if now is not None else datetime.now(UTC)
        return self.expires_at <= current


def _serialize_rule(rule: StandingRule) -> dict[str, Any]:
    """Serialize a StandingRule to a JSON-safe dict."""
    return {
        "rule_id": rule.rule_id,
        "pattern": rule.pattern,
        "mode": rule.mode.value,
        "created_at": rule.created_at.isoformat(),
        "expires_at": rule.expires_at.isoformat(),
        "granted_by": rule.granted_by,
        "note": rule.note,
    }


def _deserialize_rule(payload: dict[str, Any]) -> StandingRule:
    """Inverse of ``_serialize_rule`` -- deserializes a rule dict."""
    from oiagent_coworker.permissions.engine import PermissionMode

    return StandingRule(
        rule_id=str(payload["rule_id"]),
        pattern=str(payload["pattern"]),
        mode=PermissionMode(str(payload["mode"])),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        granted_by=str(payload.get("granted_by", "")),
        note=str(payload.get("note", "")),
    )


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _new_rule_id() -> str:
    return uuid.uuid4().hex


class OIagentCoworkerStandingRuleStore:
    """Append-only JSONL store for standing rules with TTL expiry.

    The store is designed for single-process access. Multiple processes
    writing to the same file is safe at the OS level (each ``add`` is
    one ``write + fsync`` and the JSONL parser is tolerant of torn
    writes), but the in-memory cache is process-local.

    Lazy file creation: ``__init__`` does NOT touch the filesystem.
    The store file (and its parent directory) is created on the first
    ``add`` call. This keeps construction side-effect free for tests
    and for callers that want to inspect the store path before commit.
    """

    def __init__(
        self,
        store_path: Path,
        audit_sink: object | None = None,
    ) -> None:
        # Local import to avoid an import cycle with engine / audit
        # when consumers only need the Protocol (which is duck-typed).
        from oiagent_coworker.permissions.audit import AuditDecision

        self.store_path: Path = Path(store_path)
        self._audit_sink = audit_sink
        self._AuditDecision = AuditDecision
        # In-memory cache of "last seen" rule_id -> serialized payload.
        # Rebuilt on demand by _rebuild_cache; survives across calls in
        # the same process.
        self._cache: dict[str, dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, rule: StandingRule) -> None:
        """Append a StandingRule to the JSONL log with atomic fsync."""
        self._ensure_parent()
        payload = _serialize_rule(rule)
        with open(self.store_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        self._invalidate_cache()
        self._audit("add", rule)

    def get(self, rule_id: str) -> StandingRule:
        """Return the StandingRule with the given id.

        Raises:
            KeyError: If no active rule with the given id exists.
            StandingRuleExpired: If the rule is past its expires_at.
        """
        entries = self._load_entries()
        if rule_id not in entries:
            raise KeyError(f"no standing rule with id={rule_id!r}")
        payload = entries[rule_id]
        if payload.get("action") == _REVOKE_ACTION:
            raise KeyError(f"standing rule {rule_id!r} has been revoked")
        rule = _deserialize_rule(payload)
        if rule.is_expired():
            raise StandingRuleExpired(
                f"standing rule {rule_id!r} expired at {rule.expires_at.isoformat()}"
            )
        return rule

    def revoke(self, rule_id: str) -> None:
        """Append a tombstone record revoking the rule with the given id.

        Unknown rule_id is silent (tombstone appended regardless) so
        concurrent writers don't fight over "was it revoked already?".
        """
        self._ensure_parent()
        tombstone = {
            "action": _REVOKE_ACTION,
            "rule_id": rule_id,
            "ts": _now_utc().isoformat(),
        }
        with open(self.store_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(tombstone, ensure_ascii=False, sort_keys=True))
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        self._invalidate_cache()
        self._audit("revoke", None)

    def list_active(self, *, now: datetime | None = None) -> list[StandingRule]:
        """Return all non-revoked, non-expired rules sorted by created_at."""
        current = now if now is not None else _now_utc()
        entries = self._load_entries()
        active: list[StandingRule] = []
        for payload in entries.values():
            if payload.get("action") == _REVOKE_ACTION:
                continue
            try:
                rule = _deserialize_rule(payload)
            except (KeyError, ValueError) as exc:
                _LOGGER.warning("skipping malformed standing rule: %s", exc)
                continue
            if rule.is_expired(current):
                continue
            active.append(rule)
        active.sort(key=lambda r: r.created_at)
        return active

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Atomically rewrite the store keeping only active rules.

        Returns:
            The number of entries removed (expired + tombstoned).
        """
        current = now if now is not None else _now_utc()
        active_rules = self.list_active(now=current)
        active_payloads = [_serialize_rule(r) for r in active_rules]
        removed = self._count_all_entries() - len(active_payloads)

        tmp_path = self.store_path.with_suffix(self.store_path.suffix + ".tmp")
        # If the store file does not exist yet, just create the .tmp
        # and atomic-rename it. If parent doesn't exist, this is a noop
        # (the file will be created on next add).
        self._ensure_parent()
        with open(tmp_path, "w", encoding="utf-8") as fp:
            for payload in active_payloads:
                fp.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, self.store_path)
        self._invalidate_cache()
        return removed

    def set_audit_sink(self, sink: object | None) -> None:
        """Replace the audit sink (or pass None to disable).

        Public mutator so the OIagentCoworkerAuditFacade can wire a
        standing-rule store back through the unified audit pipeline without
        poking the private ``_audit_sink`` attribute. Idempotent;
        replaces whatever sink the store was constructed with.

        See ``oiagent_coworker.permissions.audit`` for the facade contract.
        """
        self._audit_sink = sink

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ensure_parent(self) -> None:
        """Create the parent directory of store_path if needed."""
        parent = self.store_path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)

    def _invalidate_cache(self) -> None:
        self._cache = None

    def _load_entries(self) -> dict[str, dict[str, Any]]:
        """Return a dict of rule_id -> last seen payload (tolerant of corrupt lines)."""
        if self._cache is not None:
            return self._cache
        result: dict[str, dict[str, Any]] = {}
        if not self.store_path.exists():
            self._cache = result
            return result
        with open(self.store_path, "r", encoding="utf-8") as fp:
            for lineno, raw in enumerate(fp, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    _LOGGER.warning(
                        "standing_rule: skipping corrupt line %d: %s", lineno, exc
                    )
                    continue
                if not isinstance(payload, dict):
                    _LOGGER.warning(
                        "standing_rule: line %d is not a JSON object; skipping",
                        lineno,
                    )
                    continue
                rule_id = payload.get("rule_id")
                if not isinstance(rule_id, str) or not rule_id:
                    # Tombstones always carry rule_id; a missing one is
                    # malformed -- skip with a warning.
                    _LOGGER.warning(
                        "standing_rule: line %d missing rule_id; skipping", lineno
                    )
                    continue
                result[rule_id] = payload
        self._cache = result
        return result

    def _count_all_entries(self) -> int:
        """Count all lines (rules + tombstones + skipped corrupts)."""
        if not self.store_path.exists():
            return 0
        count = 0
        with open(self.store_path, "r", encoding="utf-8") as fp:
            for raw in fp:
                if raw.strip():
                    count += 1
        return count

    def _audit(self, action: str, rule: StandingRule | None) -> None:
        """Emit an AuditDecision if an audit_sink was provided."""
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(
                self._AuditDecision(
                    kind="standing_rule",
                    timestamp=_now_utc(),
                    standing_rule_action=action,  # type: ignore[arg-type]
                    standing_rule=rule,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- audit must not break persistence
            _LOGGER.warning("standing_rule audit_sink raised %s; ignored", exc)


def make_default_rule(
    pattern: str,
    mode: PermissionMode,
    *,
    granted_by: str = "user",
    ttl_seconds: int = 15 * 60,
    now: datetime | None = None,
    note: str = "",
) -> StandingRule:
    """Convenience builder for the common "user grants a 15min rule" case.

    Mirrors ``engine.DEFAULT_STANDING_RULE_TTL_S = 900`` so callers that
    don't pass ``ttl_seconds`` get the project-wide default.
    """
    from oiagent_coworker.permissions.engine import DEFAULT_STANDING_RULE_TTL_S

    current = now if now is not None else _now_utc()
    return StandingRule(
        rule_id=_new_rule_id(),
        pattern=pattern,
        mode=mode,
        created_at=current,
        expires_at=current + timedelta(seconds=ttl_seconds or DEFAULT_STANDING_RULE_TTL_S),
        granted_by=granted_by,
        note=note,
    )
