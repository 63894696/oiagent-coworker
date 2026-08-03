#!/usr/bin/env python3
"""
rename_dirs.py — W1-1.4 路径改名配套工具

rename_openworker.py 改文件内容(32 substitutions),这个脚本改目录名。

功能:
  openworker/  →  oiagent_coworker/

纯路径 rename,只改 *顶层* 的 openworker 目录名;子目录里的 openworker
不做递归(因为 import 路径已被 rename_openworker.py 改成 oiagent_coworker.x,
os.walk 自底向上 rename 才安全)。

用法:
  python scripts/rename_dirs.py --root <out-dir>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 顶层目录改名映射
DIR_RENAMES = {
    "openworker": "oiagent_coworker",
}


def rename_dirs(root: Path) -> dict:
    """自底向上 rename 目录,返回 {old: new} dict。"""
    renamed = {}
    # 自底向上:先改最深的目录,再改父目录
    for dirpath, dirnames, _filenames in root.walk(top_down=False):
        for dn in dirnames:
            if dn in DIR_RENAMES:
                old = Path(dirpath) / dn
                new = Path(dirpath) / DIR_RENAMES[dn]
                if not new.exists():
                    old.rename(new)
                    renamed[str(old)] = str(new)
                else:
                    print(f"[rename_dirs] 目标已存在,跳过: {old} → {new}",
                          file=sys.stderr)
    return renamed


def main():
    ap = argparse.ArgumentParser(description="W1-1.4 路径 rename 配套")
    ap.add_argument("--root", required=True, help="rename_openworker.py 的 --dst 目录")
    ap.add_argument("--apply", action="store_true", help="默认 dry-run;--apply 才改")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"[rename_dirs] root 不存在: {root}", file=sys.stderr)
        return 2

    if args.apply:
        renamed = rename_dirs(root)
        print(f"[rename_dirs] apply=True,改了 {len(renamed)} 处:")
        for old, new in renamed.items():
            print(f"  {old}  →  {new}")
    else:
        # dry-run:列出将要改的目录
        plan = []
        for dirpath, dirnames, _ in root.walk(top_down=False):
            for dn in dirnames:
                if dn in DIR_RENAMES:
                    plan.append((Path(dirpath) / dn, Path(dirpath) / DIR_RENAMES[dn]))
        print(f"[rename_dirs] dry-run;要改 {len(plan)} 个目录:")
        for old, new in plan:
            print(f"  WOULD: {old}  →  {new}")
        print(f"\n[rename_dirs] --apply 才改")
    return 0


if __name__ == "__main__":
    sys.exit(main())
