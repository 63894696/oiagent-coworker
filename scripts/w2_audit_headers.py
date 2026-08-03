#!/usr/bin/env python3
"""w2_audit_headers.py — 校验 W2 新增 .py 文件 100% 含 SPDX header.

扫描 ``oiagent_coworker/`` 下所有 .py 文件,要求:
  1. 顶部 ≥ 15 行 (SPDX header 模板行数)
  2. 首行 = ``# SPDX-License-Identifier: MIT``
  3. 含 ``Derived from OpenWorker`` 字样
  4. 含 ``Upstream commit`` 字样 (或实际 SHA)
  5. 含 ``Copyright (c) 2026 OIagent Project Contributors``

用法::

    python scripts/w2_audit_headers.py [repo_root]

退出码:
  0 — 全部合规
  1 — 发现缺失 header 或格式问题的文件
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REQUIRED_LINES = 15
HEADER_PATTERNS = [
    re.compile(r"^#\s*SPDX-License-Identifier:\s*MIT\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"Derived from OpenWorker", re.IGNORECASE),
    re.compile(r"Upstream commit", re.IGNORECASE),
    re.compile(r"Copyright \(c\)\s*2026\s+OIagent Project Contributors", re.IGNORECASE),
]


def check_file(path: Path) -> list[str]:
    """Return list of issues for a single .py file. Empty = compliant."""
    issues: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"read error: {exc}"]
    lines = text.splitlines()
    if len(lines) < REQUIRED_LINES:
        issues.append(f"too few lines ({len(lines)} < {REQUIRED_LINES})")
    first_line = lines[0] if lines else ""
    if not HEADER_PATTERNS[0].match(first_line):
        issues.append(f"missing SPDX header on line 1: {first_line!r}")
    for pat in HEADER_PATTERNS:
        if not re.search(pat, text):
            issues.append(f"missing pattern: {pat.pattern}")
    return issues


def main() -> int:
    repo_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    # Scan all .py under oiagent_coworker/ (skip __pycache__)
    py_files = sorted(
        p
        for p in (repo_root.glob("oiagent_coworker/**/*.py"))
        if "__pycache__" not in str(p)
    )
    errors: list[tuple[Path, list[str]]] = []
    for path in py_files:
        issues = check_file(path)
        if issues:
            errors.append((path, issues))
    if errors:
        for path, issues in errors:
            print(f"FAIL {path}:")
            for issue in issues:
                print(f"  - {issue}")
        print(f"\n{len(errors)}/{len(py_files)} files failed")
        return 1
    print(f"OK: all {len(py_files)} files have valid SPDX headers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
