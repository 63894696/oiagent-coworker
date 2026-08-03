#!/usr/bin/env python3
"""
license_lint.py — OIagent-coworker 依赖树 License 风险扫描 (W1-1.2 v2)

dry-run 暴露 + 修复后的版本:
  Fix 1: SKIP_TEXT_PATHS 排除脚本自身 + License 全文 + 政策文档
  Fix 2: 包名匹配用边界正则 / 精确等值,避免 pyreadline 误伤 readline
  Fix 3: 文档协议 SPDX(CC-BY-* / OFL-*)降级 GREEN,不传染
  Fix 4: LGPL 动态链接豁免表(psycopg 等 daemon-side 边界包显式标 GREEN)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------- License 三色分类 ----------------
GREEN_LICENSES = {
    "MIT", "MIT-0",
    "BSD-2-Clause", "BSD-3-Clause", "0BSD",
    "Apache-2.0", "Apache-2.0 WITH LLVM-exception",
    "ISC",
    "MPL-2.0",
    "Unlicense",
    "CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0",
    "Python-2.0",
    "Zlib", "Libpng", "BSL-1.0",
    "OpenSSL",
}
# 文档协议:不传染代码仓,默认 GREEN(独立分类,见 DOCUMENT_LICENSES)
DOCUMENT_LICENSES = {
    "CC-BY-NC-4.0", "CC-BY-NC-SA-4.0", "CC-BY-NC-ND-4.0",
    "CC-BY-4.0", "CC-BY-SA-4.0",
    "OFL-1.1",
}
YELLOW_LICENSES = {
    "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "EPL-1.0", "EPL-2.0",
    "MPL-2.0",
    "CDDL-1.0", "CDDL-1.1",
    "PSF-2.0",
}
RED_LICENSES = {
    "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
    "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "SSPL-1.0",
    "Commons-Clause",
    "BUSL-1.1",
    "Elastic-2.0", "Elastic-1.0",
    "QPL-1.0", "OSL-3.0",
}

# ---------------- Fix 4: LGPL 动态链接豁免 ----------------
LGPL_DYNAMIC_OK = {
    # 包名:理由(给 reviewer 看)
    "psycopg": "PostgreSQL client; dynamic .so only; OIagent daemon 是独立 process boundary",
    "libpq": "PostgreSQL C client; dynamic linking only",
}

# ---------------- Fix 1: text scan 跳过 ----------------
SKIP_TEXT_PATHS = {
    "scripts/license_lint.py",
    "scripts/license_lint_test.py",
    "scripts/license_lint_v2.py",
    "LICENSE",
    "LICENSE-OPENWORKER",
    "NOTICE",
    "docs/license-policy.md",
    "LICENSE.md",
}

# ---------------- 包名黑名单 + Fix 2:边界匹配 ----------------
_NAME_RISK = {
    "PyMuPDF": ("RED", "AGPL-3.0"),
    "pymupdf": ("RED", "AGPL-3.0"),
    "Pykka": ("YELLOW", "LGPL"),
    "MongoDB": ("YELLOW", "SSPL-1.0"),
    "pymongo": ("YELLOW", "SSPL-1.0"),
    "readline": ("YELLOW", "GPL-2.0"),
    "pyreadline": ("GREEN", "MIT"),
    "@elastic/elasticsearch": ("RED", "Elastic-2.0"),
    "mysql-server": ("RED", "GPL-2.0"),
    "mariadb-server": ("RED", "GPL-2.0"),
    "ring": ("RED", "OpenSSL/SSLeay"),
}


def _name_matches(kw: str, name: str) -> bool:
    """Fix 2: 边界匹配,避免 pyreadline 命中 readline."""
    if kw.lower() == name.lower():
        return True
    # 特定关键字必须精确等值(常见同名坑)
    if kw in {"readline", "mysql-server", "mariadb-server", "@elastic/elasticsearch"}:
        return False
    # 通用边界:连字符 / 下划线 / 点 / 字符串起止
    return re.search(
        r"(?:^|[._\-])" + re.escape(kw.lower()) + r"(?:$|[._\-])",
        name.lower(),
    ) is not None


def _classify_pkg(name: str) -> tuple[str, str, list[str]]:
    """返回 (color, license, tags)."""
    tags = []
    for kw, (color, lic) in _NAME_RISK.items():
        if _name_matches(kw, name):
            # LGPL 豁免
            if color == "YELLOW" and lic.startswith("LGPL") and name.lower() in LGPL_DYNAMIC_OK:
                tags.append("lgtm-dynamic")
                return "GREEN", lic + " (dynamic linking)", tags
            return color, lic, tags
    return "GREEN", "MIT(default)", tags


# ---------------- scan_text_files SPDX ----------------
_SPDX_EXPR_RE = re.compile(r"[A-Za-z0-9.\-+]+")


def classify(spdx: str) -> tuple[str, str]:
    s = spdx.strip()
    # 文档协议优先(永远不传染)
    for doc in DOCUMENT_LICENSES:
        if doc.lower() in s.lower():
            return "GREEN", doc
    for bad in RED_LICENSES:
        if bad.lower() in s.lower():
            return "RED", bad
    for warn in YELLOW_LICENSES:
        if warn.lower() in s.lower():
            return "YELLOW", warn
    for good in GREEN_LICENSES:
        if good.lower() in s.lower():
            return "GREEN", good
    return "UNKNOWN", s


# ---------------- 各文件扫描 ----------------
def scan_pyproject(path: Path):
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        import tomllib
        data = tomllib.loads(text)
    except ImportError:
        print("[license_lint] 缺 tomllib", file=sys.stderr)
        return []
    out = []
    for section in ("dependencies", "optional-dependencies", "dev-dependencies"):
        deps = data.get("project", {}).get(section, {})
        if isinstance(deps, dict):
            items = deps.items()
        else:
            items = ((d.split(" ")[0].split(">=")[0].split("==")[0].strip(), d) for d in deps)
        for name, raw in items:
            color, norm, tags = _classify_pkg(name)
            tag_str = " " + " ".join(f"[{t}]" for t in tags) if tags else ""
            out.append((f"pyproject:{name}", norm + tag_str, color))
    return out


def scan_requirements(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9_.\-\[\]]+)", line)
        if not m:
            continue
        name = m.group(1).split("[")[0]
        color, norm, tags = _classify_pkg(name)
        tag_str = " " + " ".join(f"[{t}]" for t in tags) if tags else ""
        out.append((f"requirements:{name}", norm + tag_str, color))
    return out


def scan_package_json(path: Path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    out = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name in (data.get(section) or {}):
            color, norm, tags = _classify_pkg(name)
            tag_str = " " + " ".join(f"[{t}]" for t in tags) if tags else ""
            out.append((f"package.json:{name}", norm + tag_str, color))
    return out


def scan_cargo_toml(path: Path):
    if not path.exists():
        return []
    try:
        import tomllib
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except ImportError:
        return []
    out = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name in data.get(section, {}):
            color, norm, tags = _classify_pkg(name)
            tag_str = " " + " ".join(f"[{t}]" for t in tags) if tags else ""
            out.append((f"cargo:{name}", norm + tag_str, color))
    return out


def scan_text_files(root: Path):
    out = []
    # Fix 5 (final): Windows 文件系统 case-insensitive,glob "LICENSE*" 会匹配
    #   license_lint.py。改用精确文件 existence 检查替代 glob。
    SKIP_BINARY = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
                   ".zip", ".tar", ".gz", ".whl", ".egg"}
    SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv"}
    LICENSE_FILE_NAMES = {"LICENSE", "LICENSE.md", "LICENSE-OPENWORKER",
                          "LICENSE-OPENWORKER.md", "NOTICE", "NOTICE.md"}
    # 用顶层 + 递归显式枚举,而不是 glob
    seen_files = set()
    targets = []
    # README / LICENSE / NOTICE:顶层 + 顶层 docs/
    for name in ("README.md", "LICENSE", "LICENSE.md", "NOTICE", "NOTICE.md"):
        p = root / name
        if p.exists() and p.is_file():
            targets.append(p)
            seen_files.add(p)
    for sub in ("docs", "doc", "."):
        for name in ("README.md", "LICENSE", "LICENSE.md",
                     "LICENSE-OPENWORKER", "LICENSE-OPENWORKER.md",
                     "NOTICE", "NOTICE.md"):
            p = root / sub / name
            if p.exists() and p.is_file() and p not in seen_files:
                targets.append(p)
                seen_files.add(p)
    # 任意子目录下的 *.md
    for p in root.rglob("*.md"):
        if p in seen_files:
            continue
        if p.is_file():
            targets.append(p)
            seen_files.add(p)
    # 任意子目录下的 LICENSE / NOTICE (精确文件名,不用 glob)
    for name in ("LICENSE", "NOTICE"):
        for p in root.rglob(name):
            if p in seen_files:
                continue
            if p.is_file():
                targets.append(p)
                seen_files.add(p)
    seen = set()
    for t in targets:
        try:
            rel = t.relative_to(root).as_posix()
        except ValueError:
            continue
        # 跳过目录 / 二进制 / 缓存
        if t.is_dir():
            continue
        if t.suffix.lower() in SKIP_BINARY:
            continue
        if any(p in rel.split("/") for p in SKIP_DIRS):
            continue
        # Fix 1: 跳过脚本自身 / License 全文 / 政策文档
        if rel in SKIP_TEXT_PATHS:
            continue
        try:
            text = t.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _SPDX_EXPR_RE.finditer(text):
            tok = m.group(0)
            if tok in seen:
                continue
            color, norm = classify(tok)
            # 文档协议 + GREEN 都不报,只看 YELLOW/RED
            if color in ("RED", "YELLOW"):
                seen.add(tok)
                out.append((f"text:{rel}", norm, color))
    return out


# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser(description="OIagent-coworker License 风险扫描")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ci", action="store_true", help="RED 即非 0 退出")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    findings = []
    findings.extend(scan_pyproject(root / "pyproject.toml"))
    findings.extend(scan_requirements(root / "requirements.txt"))
    findings.extend(scan_package_json(root / "package.json"))
    findings.extend(scan_cargo_toml(root / "src-tauri" / "Cargo.toml"))
    findings.extend(scan_text_files(root))

    if args.json:
        print(json.dumps([{"where": w, "license": l, "color": c} for w, l, c in findings], indent=2))
    else:
        red = sum(1 for _, _, c in findings if c == "RED")
        yellow = sum(1 for _, _, c in findings if c == "YELLOW")
        green = sum(1 for _, _, c in findings if c == "GREEN")
        print(f"[license_lint] 扫到 {len(findings)} 项 — GREEN {green} / YELLOW {yellow} / RED {red}\n")
        for where, lic, color in findings:
            sym = {"RED": "✗", "YELLOW": "⚠", "GREEN": "✓"}[color]
            print(f"  {sym} [{color:6s}] {where:40s} {lic}")

    if args.ci and any(c == "RED" for _, _, c in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())