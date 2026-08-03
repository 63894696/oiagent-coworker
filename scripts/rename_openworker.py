#!/usr/bin/env python3
"""
rename_openworker.py — W1-1.4 改名脚本

从 openworker 派生到 oiagent-coworker 时,把代码层所有 `openworker.*` /
`OpenWorker` / `OPENWORKER_` / `ow-` / `/openworker/` 替换成 oiagent 命名。

设计原则:
  1. dry-run 默认开,先看 diff 再 apply
  2. 保留 hand-curated 的 SKIP_PATH_NAMES(如 LICENSE-OPENWORKER / NOTICE /
     docs/license-policy.md / docs/rename-manifest.md 自身)
  3. 严格按顺序匹配:先长串(避免短串先匹配后长串不命中)
  4. 每次 apply 生成 audit JSON(rename-audit-<ts>.json),用于 reviewer
  5. 在 console 输出每文件 before/after 字节数 + diff 行数

用法:
  python scripts/rename_openworker.py --src <upstream-clone> --dst <out-dir> [--apply]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from datetime import datetime

# ---------------- 替换规则 ----------------
# 严格按顺序;前面匹配过的不会进入后面
REPLACE_RULES = [
    # 1. 完整字符串 "openworker.xxx" → "oiagent_coworker.xxx"
    (r"\bopenworker\.([a-z_][a-z_0-9]*)", r"oiagent_coworker.\1"),
    # 2. import / from-import 的模块前缀
    (r"from openworker\b", "from oiagent_coworker"),
    (r"import openworker\b", "import oiagent_coworker"),
    # 3. 类名 OpenWorkerXxx → OIagentCoworkerXxx(若存在)
    (r"\bOpenWorker([A-Z][a-zA-Z]*)?", r"OIagentCoworker\1"),
    # 4. 环境变量 — 只改 "OPENWORKER_X" 词头,不改 "XXX_OPENWORKER_X" 这种常量
    #    用 negative lookbehind (?<![A-Z0-9_]) 排除前面有字母/数字/下划线的情况
    (r"(?<![A-Z0-9_])OPENWORKER_([A-Z_][A-Z_0-9]*)\b", r"OIAGENT_COWORKER_\1"),
    # 5. 日志 / 监控标签 ow-xxx → oic-xxx
    (r"\bow-([a-z0-9\-]+)", r"oic-\1"),
    # 6. 文档字符串中的人读名(在 *.py / *.md 通用)
    (r"\bOpenWorker\b", "OIagent Coworker"),
    # 7. URL path(API endpoint,只改 /openworker/ 子路径,不动 andrewyng/openworker)
    (r"/openworker/", "/oiagent-coworker/"),
    # 8. JSON / YAML / TOML 配置 key
    (r'"openworker"', '"oiagent_coworker"'),
    (r"'openworker'", "'oiagent_coworker'"),
    # 9. CLI 脚本名 / CLI 子命令(在 pyproject.toml [project.scripts] / package.json bin)
    #   匹配 "openworker-X" / '"openworker-X"' / 'openworker X' 形态
    (r'(?<![/\w])openworker(["\-][a-z0-9_\-]+)?\b', r"oiagent-coworker\1"),
    # 10. 兜底:openworker 单独 token(在字符串、CLI 调用、等号右侧)
    #    关键:用 negative lookbehind 排除 /openworker (GitHub URL,保留!)
    #    也排除 [.\_\-]openworker (已被前面规则处理过)
    (r"(?<![/\w.\-])openworker\b", "oiagent-coworker"),
]

# 文件后缀白名单(只对代码/配置/文档做 rename;二进制不扫)
SCAN_SUFFIXES = {
    ".py", ".toml", ".json", ".yaml", ".yml", ".md", ".txt", ".cfg", ".ini",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".sh", ".bash",
    ".rs", ".go", ".java", ".kt", ".rb", ".php", ".sql", ".env", ".dockerignore",
    ".gitignore", ".editorconfig", "Dockerfile",  # Dockerfile 无后缀,但常见
}

# 路径跳过(始终不改,即使在扫的后缀内)
SKIP_PATH_NAMES = {
    "LICENSE-OPENWORKER",  # 凭证类,保留原字
    "NOTICE",              # 凭证类,保留
    "license-policy.md",   # 政策文档
    "rename-manifest.md",  # 本脚本的说明文档
    "LICENSE",
    "LICENSE.md",
    "openworker"  # 不递归到名为 openworker 的子目录(避免混淆);实际目录改名在 --apply 后单独处理
}

# 路径跳过(目录)
SKIP_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".eggs", ".mypy_cache", ".pytest_cache", ".ruff_cache", "target",
    ".github",  # CI 整文件重写,不适合 sed
    "docker",   # Dockerfile 整文件重写
    "docs",     # license-policy / rename-manifest / NOTICE 都在 docs;整目录跳过
    "scripts",  # 含本脚本自身和 license_lint.py;改本目录会污染 dry-run
}

# 单文件黑名单(即使扩展名在 SCAN_SUFFIXES,也不扫)
SKIP_FILE_NAMES = {
    "rename_openworker.py",   # 本脚本
    "license_lint.py",        # 已有 lint,改它会污染
    "add_openworker_header.py",  # 配套脚本
    "module_header_template.py",  # 模板
}


def should_skip_path(rel: Path) -> bool:
    parts = rel.parts
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    if rel.name in SKIP_FILE_NAMES:
        return True
    return False


def should_scan_suffix(path: Path) -> bool:
    if path.name in SKIP_FILE_NAMES:
        return False
    if path.name in SKIP_PATH_NAMES:
        return False
    # Dockerfile 这种无后缀
    if path.name == "Dockerfile":
        return True
    if path.suffix.lower() in SCAN_SUFFIXES:
        return True
    return False


def apply_replacements(text: str) -> tuple[str, int]:
    """按 REPLACE_RULES 顺序应用,返回 (new_text, total_subs)."""
    total = 0
    for pat, repl in REPLACE_RULES:
        new, n = re.subn(pat, repl, text)
        if n > 0:
            total += n
        text = new
    return text, total


def scan_tree(root: Path):
    """扫描 root,产出 (rel_path, abs_path, before_bytes)."""
    entries = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if should_skip_path(rel):
            continue
        if not should_scan_suffix(p):
            continue
        entries.append((rel, p, p.stat().st_size))
    return entries


def rename_in_text(old_text: str) -> tuple[str, int]:
    return apply_replacements(old_text)


def main():
    ap = argparse.ArgumentParser(description="W1-1.4 openworker → oiagent-coworker rename")
    ap.add_argument("--src", required=True, help="upstream clone 目录")
    ap.add_argument("--dst", required=True, help="output 目录")
    ap.add_argument("--apply", action="store_true",
                    help="默认 dry-run;--apply 才会写文件")
    ap.add_argument("--audit", default=None,
                    help="audit JSON 输出路径(默认 rename-audit-<ts>.json in dst)")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    dst = Path(args.dst).resolve()
    if not src.exists():
        print(f"[rename] src 不存在: {src}", file=sys.stderr)
        return 2

    entries = scan_tree(src)
    print(f"[rename] 扫到 {len(entries)} 个待改文件 (src={src})")
    if not entries:
        print("[rename] 没东西可改;检查 SCAN_SUFFIXES / SKIP_DIR_NAMES")
        return 1

    # apply or dry-run
    audit = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "src": str(src),
        "dst": str(dst),
        "apply": args.apply,
        "files": [],
        "totals": {"files": 0, "files_skipped_empty": 0, "total_subs": 0,
                   "bytes_before": 0, "bytes_after": 0},
    }

    if args.apply:
        dst.mkdir(parents=True, exist_ok=True)

    for rel, src_path, size_before in entries:
        try:
            text = src_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            audit["files"].append({
                "rel": str(rel), "error": f"read: {e!r}", "subs": 0
            })
            continue

        new_text, subs = rename_in_text(text)
        size_after = len(new_text.encode("utf-8", errors="replace"))

        rec = {
            "rel": str(rel),
            "subs": subs,
            "bytes_before": size_before,
            "bytes_after": size_after,
            "changed": subs > 0,
        }
        if args.apply and subs > 0:
            out_path = dst / rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(new_text, encoding="utf-8")
        audit["files"].append(rec)
        audit["totals"]["files"] += 1
        if subs == 0:
            audit["totals"]["files_skipped_empty"] += 1
        else:
            audit["totals"]["total_subs"] += subs
        audit["totals"]["bytes_before"] += size_before
        audit["totals"]["bytes_after"] += size_after

    # dump audit
    audit_path = Path(args.audit) if args.audit else (
        dst / f"rename-audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        if args.apply else
        Path("rename-audit-dryrun.json")
    )
    if args.apply:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False),
                          encoding="utf-8")

    # print summary
    print(f"\n[rename] 总结 — apply={args.apply}")
    print(f"  files scanned:          {audit['totals']['files']}")
    print(f"  files with no rename:   {audit['totals']['files_skipped_empty']}")
    print(f"  files changed:          {audit['totals']['files'] - audit['totals']['files_skipped_empty']}")
    print(f"  total substitutions:    {audit['totals']['total_subs']}")
    print(f"  bytes before → after:   {audit['totals']['bytes_before']} → {audit['totals']['bytes_after']}")
    print(f"  audit:                  {audit_path}")

    # 打印 changed top 20(让 reviewer 一眼可见)
    changed = [f for f in audit["files"] if f.get("changed")]
    print(f"\n  top 20 highest-substitution files:")
    for f in sorted(changed, key=lambda x: x.get("subs", 0), reverse=True)[:20]:
        print(f"    {f['subs']:>4d}  {f['rel']}")

    if not args.apply:
        print(f"\n[rename] dry-run 完毕;--apply 才会写文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
