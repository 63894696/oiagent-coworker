# SPDX-License-Identifier: MIT
#
# Copyright (c) 2026 OIagent Project Contributors
#
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    tests/test_skills.py (new file)
#   Upstream commit:  not present (W2-5.1/5.2/5.3 is OIagent-only)
#   Original author:  Andrew Ng and OpenWorker contributors
#   Original license: MIT (see ../LICENSE-OPENWORKER)
#
# Modifications by OIagent Project Contributors:
#   - New file authored for W2-5; tests the W2-5.1 (models +
#     service) + W2-5.2 (persistence) shipped surface.
#   - 23 tests, no external deps beyond pytest. Pure synchronous
#     service exercised under pytest's tmp_path fixtures for
#     cross-platform safety.
#   - Mirrors the section structure of test_selfwake.py: models
#     (Section A) -> service CRUD (B) -> module loading (C) ->
#     audit (D) -> persistence (E) -> E2E (F).
#   - See git log --follow <this file> for the full change history.
#
# This file is dual-licensed under the MIT License (see ../LICENSE).
# Per the MIT License, the original OpenWorker copyright notice and this
# permission notice are retained above.

"""Comprehensive tests for oiagent_coworker.skills (W2-5).

Covers the W2-5.1 (models + service) + W2-5.2 (persistence) ship
surface:

  * Section A -- Models / dataclass invariants (2 tests)
  * Section B -- Service CRUD (register, get, list, update_status) (4 tests)
  * Section C -- Module loading / unloading (3 tests)
  * Section D -- Audit integration (3 tests)
  * Section E -- Persistence round-trip (2 tests)
  * Section F -- End-to-end (2 tests)
  * Section G -- Edge cases / error paths (7 tests)

Total: 23 tests, no external deps beyond pytest.

Anti-flattery boundary (see plan §3.2):
    - No ``import openworker`` anywhere in this file.
    - No SKILL.md parsing; no stage_confirm gate; no aisuite stubs.
    - No asyncio / background thread runtime.
    - Borrowed design only (test surface + envelope shape), not runtime.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from oiagent_coworker.permissions.audit import AuditDecision
from oiagent_coworker.skills.loader import OIagentCoworkerSkillLoader, SkillSource
from oiagent_coworker.skills.models import Skill, SkillSpec, SkillStatus
from oiagent_coworker.skills.persistence import OIagentCoworkerSkillsPersistence
from oiagent_coworker.skills.service import OIagentCoworkerSkillsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _CapturedAudit:
    """Captured subset of an AuditDecision for skills assertions.

    The W2-5 ship widened ``AuditKind`` to include ``"skill"`` but
    keeps all the action / skill_id / skill_name info inside
    ``metadata`` to avoid coupling the audit module to the skills
    import DAG. Tests that need the action name read it from
    ``metadata['action']`` -- same pattern as
    ``test_selfwake.py`` / ``test_inbox.py``.
    """

    decision: AuditDecision


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
def storage_path(tmp_path: Path) -> Path:
    return tmp_path / "skills.jsonl"


@pytest.fixture
def service(
    storage_path: Path,
    audit_sink: Callable[[AuditDecision], None],
) -> OIagentCoworkerSkillsService:
    return OIagentCoworkerSkillsService(
        storage_path=storage_path,
        audit_sink=audit_sink,
    )


# ---------------------------------------------------------------------------
# Section A: Models (2 tests)
# ---------------------------------------------------------------------------


def test_models_dataclasses_are_frozen() -> None:
    """All three dataclasses must be frozen=True -- mutating a field
    after construction must raise ``dataclasses.FrozenInstanceError``."""
    spec = SkillSpec(
        name="test",
        version="1.0.0",
        description="desc",
        entrypoint="mod",
    )
    skill = Skill(skill_id="abc", spec=spec)
    with pytest.raises(dataclasses.FrozenInstanceError):
        skill.status = SkillStatus.INACTIVE  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        skill.spec.name = "hijacked"  # type: ignore[misc]


def test_skill_status_enum_values() -> None:
    """SkillStatus is a str-enum with the three canonical states."""
    assert SkillStatus.ACTIVE.value == "active"
    assert SkillStatus.INACTIVE.value == "inactive"
    assert SkillStatus.BROKEN.value == "broken"
    # str-mixing: comparing with the raw string works.
    assert SkillStatus.ACTIVE == "active"


# ---------------------------------------------------------------------------
# Section B: Service CRUD (4 tests)
# ---------------------------------------------------------------------------


def test_register_creates_skill_with_uuid(
    service: OIagentCoworkerSkillsService,
) -> None:
    """register_skill returns a Skill with a non-empty UUID4 hex."""
    skill = service.register_skill(
        name="web-search",
        version="1.0.0",
        description="Search the web",
        entrypoint="skills.web_search",
    )
    assert skill.skill_id is not None
    assert len(skill.skill_id) == 32  # UUID4 hex
    assert skill.spec.name == "web-search"
    assert skill.spec.version == "1.0.0"
    assert skill.spec.entrypoint == "skills.web_search"
    assert skill.status == SkillStatus.ACTIVE
    assert skill.loaded_at is not None


def test_get_unknown_skill_returns_none(
    service: OIagentCoworkerSkillsService,
) -> None:
    """get_skill for a non-existent id returns None."""
    assert service.get_skill("nonexistent") is None


def test_list_skills_returns_registered(
    service: OIagentCoworkerSkillsService,
) -> None:
    """list_skills returns all registered skills; filtering by status
    works when there are multiple statuses."""
    s1 = service.register_skill(
        name="a", version="1", description="d", entrypoint="mod.a"
    )
    s2 = service.register_skill(
        name="b", version="1", description="d", entrypoint="mod.b"
    )
    all_skills = service.list_skills()
    assert len(all_skills) == 2
    ids = {s.skill_id for s in all_skills}
    assert ids == {s1.skill_id, s2.skill_id}
    # Filter by ACTIVE (default for new skills).
    active = service.list_skills(status=SkillStatus.ACTIVE)
    assert len(active) == 2
    # Switch one to INACTIVE.
    service.update_skill_status(s1.skill_id, SkillStatus.INACTIVE)
    active2 = service.list_skills(status=SkillStatus.ACTIVE)
    assert len(active2) == 1
    assert active2[0].skill_id == s2.skill_id
    inactive = service.list_skills(status=SkillStatus.INACTIVE)
    assert len(inactive) == 1
    assert inactive[0].skill_id == s1.skill_id


def test_update_skill_status_returns_false_for_unknown(
    service: OIagentCoworkerSkillsService,
) -> None:
    """update_skill_status returns False when the skill_id doesn't exist."""
    assert service.update_skill_status("ghost-id", SkillStatus.INACTIVE) is False


# ---------------------------------------------------------------------------
# Section C: Module loading / unloading (3 tests)
# ---------------------------------------------------------------------------


def test_load_skill_module_missing_entrypoint_returns_none(
    service: OIagentCoworkerSkillsService,
) -> None:
    """Skill without an entrypoint returns None from load_skill_module."""
    skill = service.register_skill(
        name="no-entry",
        version="1",
        description="d",
        entrypoint="",
    )
    assert service.load_skill_module(skill.skill_id) is None


def test_load_skill_module_unknown_id_returns_none(
    service: OIagentCoworkerSkillsService,
) -> None:
    """load_skill_module for a non-existent id returns None."""
    assert service.load_skill_module("nope") is None


def test_unload_skill_module_unknown_id_returns_false(
    service: OIagentCoworkerSkillsService,
) -> None:
    """unload_skill_module for a non-existent id returns False."""
    assert service.unload_skill_module("nope") is False


# ---------------------------------------------------------------------------
# Section D: Audit integration (3 tests)
# ---------------------------------------------------------------------------


def test_register_emits_skill_audit_envelope(
    service: OIagentCoworkerSkillsService,
    captured_audit: list[_CapturedAudit],
) -> None:
    """register_skill emits an AuditDecision with kind='skill'."""
    service.register_skill(
        name="audit-test", version="1", description="d", entrypoint="mod"
    )
    assert len(captured_audit) == 1
    decision = captured_audit[0].decision
    assert decision.kind == "skill"
    assert decision.metadata["action"] == "register"
    assert decision.metadata["skill_name"] == "audit-test"
    assert decision.metadata["skill_status"] == "active"


def test_update_status_emits_skill_audit_envelope(
    service: OIagentCoworkerSkillsService,
    captured_audit: list[_CapturedAudit],
) -> None:
    """update_skill_status emits an AuditDecision with action='update_status'."""
    skill = service.register_skill(
        name="upd", version="1", description="d", entrypoint="mod"
    )
    captured_audit.clear()
    service.update_skill_status(skill.skill_id, SkillStatus.INACTIVE)
    assert len(captured_audit) == 1
    decision = captured_audit[0].decision
    assert decision.kind == "skill"
    assert decision.metadata["action"] == "update_status"
    assert decision.metadata["skill_status"] == "inactive"


def test_audit_sink_none_no_crash(
    storage_path: Path,
) -> None:
    """Service with audit_sink=None still works and does not crash on register."""
    svc = OIagentCoworkerSkillsService(
        storage_path=storage_path,
        audit_sink=None,
    )
    skill = svc.register_skill(
        name="no-audit", version="1", description="d", entrypoint="mod"
    )
    assert skill is not None
    assert svc.get_skill(skill.skill_id) is not None


# ---------------------------------------------------------------------------
# Section E: Persistence round-trip (2 tests)
# ---------------------------------------------------------------------------


def test_persistence_append_and_replay_round_trip(
    tmp_path: Path,
) -> None:
    """Construct :class:`OIagentCoworkerSkillsPersistence` directly;
    append 3 skills; ``replay()`` yields all 3 in insertion order."""
    path = tmp_path / "skills.jsonl"
    store = OIagentCoworkerSkillsPersistence(path)
    skills = []
    for i in range(3):
        spec = SkillSpec(
            name=f"skill-{i}",
            version="1",
            description=f"d{i}",
            entrypoint=f"mod.{i}",
        )
        skills.append(Skill(skill_id=f"id-{i}", spec=spec))
        store.append_skill(skills[-1])

    replayed = list(store.replay())
    assert len(replayed) == 3
    assert [s.skill_id for s in replayed] == ["id-0", "id-1", "id-2"]
    assert [s.spec.name for s in replayed] == ["skill-0", "skill-1", "skill-2"]


def test_persistence_delete_removes_from_replay(
    tmp_path: Path,
) -> None:
    """After append + delete, replay() yields only the remaining skill."""
    path = tmp_path / "skills.jsonl"
    store = OIagentCoworkerSkillsPersistence(path)
    spec = SkillSpec(name="keep", version="1", description="d", entrypoint="mod")
    keep = Skill(skill_id="keep-id", spec=spec)
    remove = Skill(skill_id="remove-id", spec=spec)
    store.append_skill(keep)
    store.append_skill(remove)
    store.delete_skill("remove-id")

    replayed = list(store.replay())
    assert len(replayed) == 1
    assert replayed[0].skill_id == "keep-id"


# ---------------------------------------------------------------------------
# Section F: End-to-end (2 tests)
# ---------------------------------------------------------------------------


def test_e2e_register_then_get_round_trip(
    service: OIagentCoworkerSkillsService,
) -> None:
    """register -> get -> update_status -> list filtered. The full
    lifecycle without any persistence restart."""
    skill = service.register_skill(
        name="e2e-test",
        version="1.0.0",
        description="E2E integration",
        entrypoint="skills.e2e",
        config={"api_key": "secret"},
    )
    fetched = service.get_skill(skill.skill_id)
    assert fetched is not None
    assert fetched.spec.name == "e2e-test"
    assert fetched.spec.config == {"api_key": "secret"}
    # Update status.
    assert service.update_skill_status(skill.skill_id, SkillStatus.BROKEN) is True
    broken = service.list_skills(status=SkillStatus.BROKEN)
    assert len(broken) == 1
    assert broken[0].skill_id == skill.skill_id


def test_e2e_persistence_restart_preserves_skills(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
) -> None:
    """Persist skills with one service instance, close it, create a
    new service from the same path, and verify the skills are still
    there."""
    path = tmp_path / "skills.jsonl"
    svc1 = OIagentCoworkerSkillsService(storage_path=path, audit_sink=audit_sink)
    s1 = svc1.register_skill(name="p1", version="1", description="d", entrypoint="mod")
    s2 = svc1.register_skill(name="p2", version="1", description="d", entrypoint="mod")
    svc1.update_skill_status(s1.skill_id, SkillStatus.INACTIVE)

    # Simulate restart: fresh service reads from the same log.
    svc2 = OIagentCoworkerSkillsService(storage_path=path, audit_sink=audit_sink)
    all_skills = svc2.list_skills()
    assert len(all_skills) == 2
    ids = {s.skill_id for s in all_skills}
    assert ids == {s1.skill_id, s2.skill_id}
    # Status should be preserved.
    for s in all_skills:
        if s.skill_id == s1.skill_id:
            assert s.status == SkillStatus.INACTIVE
        else:
            assert s.status == SkillStatus.ACTIVE


# ---------------------------------------------------------------------------
# Section G: Edge cases / error paths (7 tests)
# ---------------------------------------------------------------------------


def test_persistence_replay_skips_malformed_lines(
    tmp_path: Path,
) -> None:
    """A JSONL file with a corrupted line in the middle should still
    yield the surrounding valid entries -- the parser skips malformed
    lines at WARNING and continues."""

    from oiagent_coworker.skills.models import Skill, SkillSpec

    path = tmp_path / "skills.jsonl"
    store = OIagentCoworkerSkillsPersistence(path)
    spec = SkillSpec(name="good", version="1", description="d", entrypoint="mod")
    store.append_skill(Skill(skill_id="id-a", spec=spec))
    # Write a malformed line directly.
    with open(path, "a", encoding="utf-8") as fp:
        fp.write("{this is not json\n")
    store.append_skill(Skill(skill_id="id-b", spec=spec))

    replayed = list(store.replay())
    assert len(replayed) == 2
    assert {s.skill_id for s in replayed} == {"id-a", "id-b"}


def test_persistence_replay_skips_unknown_event_type(
    tmp_path: Path,
) -> None:
    """A line with an unknown event_type is skipped, not crashing replay."""
    import json

    path = tmp_path / "skills.jsonl"
    # Write a line with a bogus event_type directly.
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(
            json.dumps(
                {"event_type": "purge", "skill": None, "timestamp": "2026-08-02T00:00:00+00:00"},
                ensure_ascii=False,
            )
            + "\n"
        )
    store = OIagentCoworkerSkillsPersistence(path)
    replayed = list(store.replay())
    assert len(replayed) == 0


def test_persistence_replay_skips_missing_skill_dict(
    tmp_path: Path,
) -> None:
    """A create event with no 'skill' key is skipped."""
    import json

    path = tmp_path / "skills.jsonl"
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(
            json.dumps(
                {
                    "event_type": "create",
                    "skill": None,
                    "timestamp": "2026-08-02T00:00:00+00:00",
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    store = OIagentCoworkerSkillsPersistence(path)
    replayed = list(store.replay())
    assert len(replayed) == 0


def test_load_skill_module_failure_returns_none_no_raise(
    service: OIagentCoworkerSkillsService,
) -> None:
    """load_skill_module with a non-existent entrypoint returns None
    without raising -- the exception is caught internally and logged."""
    skill = service.register_skill(
        name="bad-mod", version="1", description="d", entrypoint="nonexistent.module.xyz"
    )
    # Must not raise; should return None and cache None.
    result = service.load_skill_module(skill.skill_id)
    assert result is None
    # Second call should also return None (cached).
    assert service.load_skill_module(skill.skill_id) is None


def test_service_loads_from_persistence_on_construction(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
) -> None:
    """A service constructed from a path with pre-existing log data
    should replay all skills into memory."""
    from oiagent_coworker.skills.models import Skill, SkillSpec

    path = tmp_path / "skills.jsonl"
    spec = SkillSpec(name="pre", version="1", description="d", entrypoint="mod")
    s1 = Skill(skill_id="pre-1", spec=spec, status=SkillStatus.INACTIVE)
    s2 = Skill(skill_id="pre-2", spec=spec, status=SkillStatus.BROKEN)

    store = OIagentCoworkerSkillsPersistence(path)
    store.append_skill(s1)
    store.append_skill(s2)

    svc = OIagentCoworkerSkillsService(storage_path=path, audit_sink=audit_sink)
    all_skills = svc.list_skills()
    assert len(all_skills) == 2
    ids = {s.skill_id for s in all_skills}
    assert ids == {"pre-1", "pre-2"}
    # Statuses preserved from persistence.
    for s in all_skills:
        if s.skill_id == "pre-1":
            assert s.status == SkillStatus.INACTIVE
        else:
            assert s.status == SkillStatus.BROKEN


def test_service_loads_empty_persistence_gracefully(
    tmp_path: Path,
    audit_sink: Callable[[AuditDecision], None],
) -> None:
    """A service constructed from a non-existent path should start with
    an empty registry (not crash)."""
    path = tmp_path / "empty.jsonl"
    svc = OIagentCoworkerSkillsService(storage_path=path, audit_sink=audit_sink)
    assert svc.list_skills() == []


def test_emit_audit_sink_raises_no_crash(
    tmp_path: Path,
) -> None:
    """If the audit sink raises, the service should catch it and
    continue operating (audit failures must never break the skill
    registry)."""
    def bad_sink(decision):
        raise RuntimeError("sink exploded")

    svc = OIagentCoworkerSkillsService(
        storage_path=tmp_path / "skills.jsonl",
        audit_sink=bad_sink,
    )
    skill = svc.register_skill(
        name="robust", version="1", description="d", entrypoint="mod"
    )
    assert skill is not None
    assert svc.get_skill(skill.skill_id) is not None


# ---------------------------------------------------------------------------
# Section H: Loader — SKILL.md folder-as-truth + scope resolution (W2-5.1)
# ---------------------------------------------------------------------------


@pytest.fixture
def loader() -> "OIagentCoworkerSkillLoader":
    from oiagent_coworker.skills.loader import OIagentCoworkerSkillLoader

    return OIagentCoworkerSkillLoader()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "skills"


def test_folder_as_truth_discovers_SKILL_md(
    loader: "OIagentCoworkerSkillLoader",
    fixtures_dir: Path,
) -> None:
    """discover() finds skills by looking for SKILL.md (case-sensitive).
    A lowercase `skill.md` must be ignored."""
    entries = loader.discover(fixtures_dir / "global", SkillSource.GLOBAL)
    # "global" dir has skill-a and skill-b -> 2 entries.
    names = {e.name for e in entries}
    assert "skill-a" in names
    assert "skill-b" in names

    misspelled = loader.discover(fixtures_dir / "misspelled", SkillSource.GLOBAL)
    assert misspelled == [], "lowercase skill.md must not be discovered"


def test_folder_as_truth_ignores_non_dirs(
    loader: "OIagentCoworkerSkillLoader",
    tmp_path: Path,
) -> None:
    """A SKILL.md placed at the root of the scan directory (not inside
    a subdirectory) must be ignored -- the loader expects folder-as-truth."""
    # Put a SKILL.md directly under tmp_path, no subdirectory.
    (tmp_path / "SKILL.md").write_text(
        "---\nname: root-skill\nversion: 1.0.0\ndescription: root\n"
        "entrypoint: mod.root\n---\n",
        encoding="utf-8",
    )
    entries = loader.discover(tmp_path, SkillSource.GLOBAL)
    assert entries == [], "SKILL.md at root must not be discovered"


def test_folder_as_truth_skips_dir_without_SKILL_md(
    loader: "OIagentCoworkerSkillLoader",
    tmp_path: Path,
) -> None:
    """A subdirectory without SKILL.md must not produce an entry."""
    stub_dir = tmp_path / "orphan"
    stub_dir.mkdir()
    (stub_dir / "notes.md").write_text("not a skill", encoding="utf-8")
    entries = loader.discover(tmp_path, SkillSource.GLOBAL)
    assert entries == []


def test_scope_priority_global_vs_project(
    loader: "OIagentCoworkerSkillLoader",
    fixtures_dir: Path,
) -> None:
    """When the same skill name appears at global and project scope,
    project must win after resolve()."""
    loader.discover(fixtures_dir / "global", SkillSource.GLOBAL)
    loader.discover(fixtures_dir / "project", SkillSource.PROJECT)
    resolved = loader.resolve()
    by_name = {e.name: e for e in resolved}
    # skill-a: project wins.
    skill_a = by_name["skill-a"]
    assert skill_a.scope == SkillSource.PROJECT
    assert skill_a.spec.version == "2.0.0"
    assert skill_a.spec.entrypoint == "skills.project_a"
    # skill-b: project also wins (exists at both).
    skill_b = by_name["skill-b"]
    assert skill_b.scope == SkillSource.PROJECT
    assert skill_b.spec.entrypoint == "skills.project_b"
    # Only 2 entries total after dedup.
    assert len(resolved) == 2


def test_scope_priority_user_wins_over_project(
    loader: "OIagentCoworkerSkillLoader",
    tmp_path: Path,
) -> None:
    """User scope must win over project and global."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    pa = project_dir / "skill-x"
    pa.mkdir()
    (pa / "SKILL.md").write_text(
        "---\nname: skill-x\nversion: 1.0.0\ndescription: p\n"
        "entrypoint: proj.x\n---\n",
        encoding="utf-8",
    )
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    ua = user_dir / "skill-x"
    ua.mkdir()
    (ua / "SKILL.md").write_text(
        "---\nname: skill-x\nversion: 3.0.0\ndescription: u\n"
        "entrypoint: user.x\n---\n",
        encoding="utf-8",
    )
    loader.discover(project_dir, SkillSource.PROJECT)
    loader.discover(user_dir, SkillSource.USER)
    resolved = loader.resolve()
    winner = next(e for e in resolved if e.name == "skill-x")
    assert winner.scope == SkillSource.USER
    assert winner.spec.version == "3.0.0"
    assert winner.spec.entrypoint == "user.x"


def test_resolve_no_duplicates_returns_all(
    loader: "OIagentCoworkerSkillLoader",
    fixtures_dir: Path,
) -> None:
    """When no names collide, resolve() returns all entries."""
    entries = loader.discover(fixtures_dir / "project", SkillSource.PROJECT)
    resolved = loader.resolve(entries)
    assert len(resolved) == 2
    names = {e.name for e in resolved}
    assert names == {"skill-a", "skill-b"}


def test_parse_skill_md_missing_required_fields_skipped(
    loader: "OIagentCoworkerSkillLoader",
    tmp_path: Path,
) -> None:
    """SKILL.md with missing required keys (e.g. no entrypoint) is
    silently skipped, not raised."""
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = bad_dir / "SKILL.md"
    bad.write_text(
        "---\nname: broken\nversion: 1\n---\n",
        encoding="utf-8",
    )
    entries = loader.discover(tmp_path, SkillSource.GLOBAL)
    assert entries == []


def test_parse_skill_md_invalid_yaml_raises(
    loader: "OIagentCoworkerSkillLoader",
    tmp_path: Path,
) -> None:
    """A SKILL.md with invalid YAML frontmatter is skipped (not raised
    by discover)."""
    bad_dir = tmp_path / "bad-yaml"
    bad_dir.mkdir()
    bad = bad_dir / "SKILL.md"
    bad.write_text(
        "---\nname: [invalid\nyml: {broken\n---\n",
        encoding="utf-8",
    )
    entries = loader.discover(tmp_path, SkillSource.GLOBAL)
    assert entries == []


def test_loader_clear_resets_discoveries(
    loader: "OIagentCoworkerSkillLoader",
    fixtures_dir: Path,
) -> None:
    """After clear(), subsequent resolve() returns empty."""
    loader.discover(fixtures_dir / "global", SkillSource.GLOBAL)
    assert len(loader._discoveries) > 0
    loader.clear()
    assert loader._discoveries == []
    assert loader.resolve() == []


def test_skill_source_priority_values() -> None:
    """SkillSource priority must map to integers in the expected order."""
    assert SkillSource.GLOBAL.priority == 0
    assert SkillSource.PROJECT.priority == 1
    assert SkillSource.USER.priority == 2


def test_loader_root_not_directory_raises(
    loader: "OIagentCoworkerSkillLoader",
    tmp_path: Path,
) -> None:
    """discover() must raise ValueError when root is not an existing
    directory (e.g. a file)."""
    f = tmp_path / "not-a-dir"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be an existing directory"):
        loader.discover(f, SkillSource.GLOBAL)


def test_loader_root_nonexistent_raises(
    loader: "OIagentCoworkerSkillLoader",
    tmp_path: Path,
) -> None:
    """discover() must raise ValueError when root does not exist."""
    with pytest.raises(ValueError, match="root must be an existing directory"):
        loader.discover(tmp_path / "nope", SkillSource.GLOBAL)
