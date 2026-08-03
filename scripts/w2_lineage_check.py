#!/usr/bin/env python3
"""w2_lineage_check.py — 校验 W2 模块 import 图无 openworker.* 残留.

扫描 ``oiagent_coworker/`` 下所有 .py 文件,检查:
  1. ``from openworker`` / ``import openworker`` → 必须 **0 hit**
  2. ``import openai`` / ``import anthropic`` / ``import aisuite`` → 必须 **0 hit**
  3. ``import fitz`` / ``import pymupdf`` → 必须 **0 hit**
  4. ``croniter`` → 必须 **0 hit**

SPDX 头部注释中的 ``openworker`` 字样豁免(只检查代码 import 行).

用法::

    python scripts/w2_lineage_check.py [repo_root]

退出码:
  0 — 无残留
  1 — 发现违规 import
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Patterns to flag (checked only on code lines, not in comments).
FORBIDDEN_IMPORT_PATTERNS = [
    re.compile(r"^(from|import)\s+openworker\b"),
    re.compile(r"^(from|import)\s+openai\b"),
    re.compile(r"^(from|import)\s+anthropic\b"),
    re.compile(r"^(from|import)\s+aisuite\b"),
    re.compile(r"^(from|import)\s+fitz\b"),
    re.compile(r"^(from|import)\s+pymupdf\b"),
    re.compile(r"^(from|import)\s+croniter\b"),
]


def check_file(path: Path) -> list[str]:
    """Return list of violations for a single .py file."""
    violations: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [f"read error: {exc}"]
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        # Skip comments and blank lines.
        if not stripped or stripped.startswith("#"):
            continue
        for pat in FORBIDDEN_IMPORT_PATTERNS:
            if pat.search(stripped):
                violations.append(f"line {lineno}: {stripped}")
    return violations


def main() -> int:
    repo_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    py_files = sorted(
        p
        for p in (repo_root.glob("oiagent_coworker/**/*.py"))
        if "__pycache__" not in str(p)
    )
    errors: list[tuple[Path, list[str]]] = []
    for path in py_files:
        violations = check_file(path)
        if violations:
            errors.append((path, violations))
    if errors:
        for path, violations in errors:
            print(f"FAIL {path}:")
            for v in violations:
                print(f"  - {v}")
        total = sum(len(v) for _, v in errors)
        print(f"\n{len(errors)} files, {total} violations found")
        return 1
    print(f"OK: {len(py_files)} files, no openworker/forbidden import residuals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
