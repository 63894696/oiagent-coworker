# SPDX-License-Identifier: MIT
"""Tests for workspace path containment."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.path_sandbox import (
    OIagentCoworkerPathSandbox,
    PathSandboxConfig,
    SandboxReason,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> OIagentCoworkerPathSandbox:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return OIagentCoworkerPathSandbox(PathSandboxConfig(workspace_root=workspace))


def test_within_workspace_allowed(sandbox: OIagentCoworkerPathSandbox) -> None:
    target = sandbox.config.workspace_root / "foo" / "bar.py"
    assert sandbox.sandbox_path(target).reason is SandboxReason.ALLOWED


def test_outside_workspace_blocked(sandbox: OIagentCoworkerPathSandbox) -> None:
    decision = sandbox.sandbox_path(sandbox.config.workspace_root / ".." / "outside")
    assert decision.reason is SandboxReason.OUTSIDE_WORKSPACE


def test_relative_dotdot_escape(sandbox: OIagentCoworkerPathSandbox) -> None:
    decision = sandbox.sandbox_path("foo/../../etc/passwd")
    assert decision.reason is SandboxReason.OUTSIDE_WORKSPACE


def test_tilde_escape_blocked(
    sandbox: OIagentCoworkerPathSandbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(sandbox.config.workspace_root))
    decision = sandbox.sandbox_path("~/../etc/passwd")
    assert decision.reason is SandboxReason.TILDE_EXPANSION_ESCAPE


def test_absolute_path_bypass_blocked(sandbox: OIagentCoworkerPathSandbox) -> None:
    outside = Path("C:/evil.exe") if os.name == "nt" else Path("/etc/shadow")
    assert sandbox.sandbox_path(outside).reason is SandboxReason.ABSOLUTE_BYPASS


def test_symlink_escape_blocked(
    sandbox: OIagentCoworkerPathSandbox,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = sandbox.config.workspace_root / "link_to_outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert sandbox.sandbox_path(link).reason is SandboxReason.SYMLINK_ESCAPE


def test_relative_path_within_workspace(sandbox: OIagentCoworkerPathSandbox) -> None:
    assert sandbox.sandbox_path("src/main.py").reason is SandboxReason.ALLOWED


def test_relative_dotdot_stays_inside(sandbox: OIagentCoworkerPathSandbox) -> None:
    assert sandbox.sandbox_path("foo/../bar.py").reason is SandboxReason.ALLOWED


def test_workspace_root_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        OIagentCoworkerPathSandbox(PathSandboxConfig(workspace_root=Path("rel")))


def test_case_insensitive_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "Workspace"
    workspace.mkdir()
    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=workspace, case_insensitive=True)
    )
    alternate_case = Path(str(workspace).swapcase()) / "Foo"
    assert sandbox.is_within_workspace(alternate_case)


def test_drive_letter_bypass_blocked(sandbox: OIagentCoworkerPathSandbox) -> None:
    decision = sandbox.sandbox_path(r"D:\evil.exe")
    assert decision.reason is SandboxReason.ABSOLUTE_BYPASS


def test_audit_sink_called_on_decision(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sink = Mock()
    sandbox = OIagentCoworkerPathSandbox(
        PathSandboxConfig(workspace_root=workspace), audit_sink=sink
    )
    sandbox.sandbox_path("src/main.py")
    assert sink.call_count >= 1


def test_check_relative_rejects_absolute(sandbox: OIagentCoworkerPathSandbox) -> None:
    target = Path("C:/evil") if os.name == "nt" else Path("/etc/passwd")
    assert sandbox.check_relative(target).reason is SandboxReason.ABSOLUTE_BYPASS


def test_tilde_within_workspace_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`~/inner.py` with HOME=workspace must classify as allowed.

    Regression for W2-1.2 re-review Note 3: Windows ``os.path.expanduser``
    honours ``USERPROFILE`` over ``HOME``, so a tempfile-based ``HOME``
    fixture does not actually redirect expansion. Mock ``expanduser`` to
    pin the expansion to ``tmp_path`` (== workspace + one level) and verify
    the sandbox accepts the resolved path.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sb = OIagentCoworkerPathSandbox(PathSandboxConfig(workspace_root=workspace))
    target_rel = "ws/inner.py"
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda _p: str(tmp_path / target_rel),
    )
    decision = sb.sandbox_path("~/inner.py")
    assert decision.allow is True
    assert decision.reason is SandboxReason.ALLOWED
