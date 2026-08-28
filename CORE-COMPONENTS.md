# Core Components (OIE-PCS-1.0 §1, §3)

This document is incorporated into the OI Enhancements Personal and
Commercial Source License (OIE-PCS-1.0). Modifications to the files and
directories listed below, when **Distributed** or made available as a
**Network Service** (as defined in LICENSE §1 and §3), must be made
available under the terms of OIE-PCS-1.0 per LICENSE §3.

This list is the **single source of truth** for which paths are
"Core Components" for this repository. Paths NOT listed here are not
Core Components and are not subject to the source-availability
obligation of LICENSE §3 when used outside Commercial Use.


## Core Components (modifications must be made available under OIE-PCS-1.0)

### 包入口与核心模块(oiagent_coworker/)
- `oiagent_coworker/inbox/__init__.py`
- `oiagent_coworker/inbox/models.py`
- `oiagent_coworker/inbox/persistence.py`
- `oiagent_coworker/inbox/service.py`
- `oiagent_coworker/permissions/__init__.py`
- `oiagent_coworker/permissions/audit.py`
- `oiagent_coworker/permissions/audit_stream.py`
- `oiagent_coworker/permissions/audit_tee.py`
- `oiagent_coworker/permissions/consistency.py`
- `oiagent_coworker/permissions/engine.py`
- `oiagent_coworker/permissions/path_sandbox.py`
- `oiagent_coworker/permissions/persistence.py`
- `oiagent_coworker/permissions/policy_gate.py`
- `oiagent_coworker/permissions/shell_classifier.py`
- `oiagent_coworker/persona/__init__.py`
- `oiagent_coworker/persona/models.py`
- `oiagent_coworker/persona/persistence.py`
- `oiagent_coworker/persona/service.py`
- `oiagent_coworker/selfwake/__init__.py`
- `oiagent_coworker/selfwake/models.py`
- `oiagent_coworker/selfwake/persistence.py`
- `oiagent_coworker/selfwake/scheduler.py`
- `oiagent_coworker/skills/__init__.py`
- `oiagent_coworker/skills/loader.py`
- `oiagent_coworker/skills/manifest.py`
- `oiagent_coworker/skills/models.py`
- `oiagent_coworker/skills/persistence.py`
- `oiagent_coworker/skills/service.py`
- `oiagent_coworker/skills/stage_confirm.py`

### Tauri 桌面壳
- `src-tauri/Cargo.toml`

### 项目元数据
- `.UPSTREAM_COMMIT`
- `license_lint.py`
- `package.json`
- `pyproject.toml`


## NOT Core Components (modifications do NOT trigger LICENSE §3 obligations)

The following are NOT Core Components. Modifications to these paths
do NOT, on their own, trigger the source-availability obligation of
LICENSE §3, provided such modifications are not Distributed as part
of Commercial Use without a commercial license.

### 文档与法律文件
- `README.md`, `*.md` at any depth
- `LICENSE`, `LICENSE-APACHE`, `LICENSE-POLICY.md`
- `CORE-COMPONENTS.md`, `TRADEMARKS.md`, `COMMERCIAL-LICENSE.md`
- `CONTRIBUTING.md`, `SECURITY.md`, `THIRD-PARTY-NOTICES`
- `CHANGELOG.md`, `NOTICE`, `PRIVACY.md`

### 文档目录
- `docs/`

### 测试 / 评估
- `tests/`, `test/`, `*_test/`, `*_eval/`
- `tests/fixtures/`

### CI / 工作流 / 工具脚本(非核心业务)
- `.github/`

### 资源文件
- `icons/`, `extension/icons/`
- `assets/` (图标/UI 资源;Brand 元素使用受 TRADEMARKS.md 约束)

### 个人开发 / 实验 / 备份 / 临时
- `_*.py`, `_*.png`, `_*.log`, `_*.db`
- `backup-*/`, `*.bak`, `*.tmp`

### 构建产物与本地运行时
- `__pycache__/`, `.venv/`, `node_modules/`, `dist/`, `build/`
- `target/` (Rust), `release/` (Android APK)
- 本地配置文件: `.env`, `*.db`, `*.db-shm`, `*.db-wal`


## How to interpret this list

- Paths are matched as **prefixes** (directory) or **exact files**.
- A modification that **transitively** affects a Core Component
  (e.g. by changing its public API used by another module) is
  itself considered a modification of the Core Component for the
  purposes of LICENSE §3.
- If a Core Component path is renamed or moved, this document is
  authoritative: the path listed here continues to be a Core Component
  regardless of the actual file system location, and the contributor
  of the rename must update this document in the same commit.
- If You are uncertain whether a path is a Core Component, treat it
  as a Core Component, or contact the Project Copyright Holder before
  Distribution.


## Changes to this document

This document may be amended by the Project Copyright Holder at any
time. The version of this document in effect at the time of Your
Distribution governs the source-availability obligation for that
Distribution.

Last updated: 2026-08-28
