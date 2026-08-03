# SPDX-License-Identifier: MIT
"""Tests for three-tier shell command classification."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from oiagent_coworker.permissions.shell_classifier import (
    OIagentCoworkerShellClassifier,
    ShellRiskLevel,
)


@pytest.fixture
def classifier() -> OIagentCoworkerShellClassifier:
    return OIagentCoworkerShellClassifier()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("echo hello", ShellRiskLevel.SAFE),
        ("ls -la /workspace", ShellRiskLevel.SAFE),
        ("cat README.md", ShellRiskLevel.SAFE),
        ("git status", ShellRiskLevel.SAFE),
        ("pytest tests/", ShellRiskLevel.SAFE),
    ],
)
def test_safe_commands(
    classifier: OIagentCoworkerShellClassifier,
    command: str,
    expected: ShellRiskLevel,
) -> None:
    result = classifier.classify(command)
    assert result.risk_level is expected
    assert not result.requires_approval


@pytest.mark.parametrize(
    "command",
    [
        "curl http://example.com",
        "chmod 777 /workspace/file",
        "sudo ls /workspace",
        "git push --force",
        "apt install vim",
        "rm file.txt",
        "git diff --no-index /etc/passwd /workspace/x",
    ],
)
def test_risky_commands(
    classifier: OIagentCoworkerShellClassifier,
    command: str,
) -> None:
    result = classifier.classify(command)
    assert result.risk_level is ShellRiskLevel.RISKY
    assert result.requires_approval


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -rf ~",
        "format C:",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
        "rm -rf .",
        "mkfs.ext4 /dev/sda1",
        "shutdown now",
    ],
)
def test_destructive_commands(
    classifier: OIagentCoworkerShellClassifier,
    command: str,
) -> None:
    result = classifier.classify(command)
    assert result.risk_level is ShellRiskLevel.DESTRUCTIVE
    assert result.requires_approval


def test_force_with_lease_is_not_force_push(
    classifier: OIagentCoworkerShellClassifier,
) -> None:
    assert classifier.classify("git push --force-with-lease").risk_level is ShellRiskLevel.SAFE


@pytest.mark.parametrize(
    ("command", "hint", "expected"),
    [
        ("pwsh -c Get-ChildItem", "unknown", "pwsh"),
        ("cmd /c dir", "unknown", "cmd"),
        ("#!/bin/zsh\necho hello", "unknown", "zsh"),
        ("echo hello", "fish", "fish"),
        ("echo hello", "unknown", "bash"),
    ],
)
def test_shell_kind_detection(
    classifier: OIagentCoworkerShellClassifier,
    command: str,
    hint: str,
    expected: str,
) -> None:
    assert classifier.classify(command, hint).shell_kind == expected


def test_target_normalized(classifier: OIagentCoworkerShellClassifier) -> None:
    result = classifier.classify("  ECHO Hello  ")
    assert result.target_normalized == "echo hello"


def test_predicate_helpers(classifier: OIagentCoworkerShellClassifier) -> None:
    assert classifier.is_safe("echo hello")
    assert classifier.is_destructive("rm -rf /")
