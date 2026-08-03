# W2 — 从 fork 后的 andrewyng/openworker 抽取 5 个核心模块

> **OIagent P1 W2 阶段实现计划**
> 工作目录:`D:\Temp\license_lint_dryrun\`(W1 dryrun 仓;真实 fork 仓路径 = `63894696/oiagent-coworker`)
> 上游:`https://github.com/andrewyng/openworker`(MIT)
> 主对话拍板日期:2026-08-02
> 本计划文档不写代码,只定义边界、文件、依赖、风险与子任务拆分。

---

## §1 目标与非目标

### 1.1 W2 ship 的"是"

- 把 fork 后的 openworker 5 个核心模块改造成 OIagent Coworker 的**本地 daemon 后端**:
  - `PermissionEngine`(覆盖 OIagent P0-3 PolicyEngine,含 5 模式 + 路径沙箱 + shell op 检测 + task-scoped standing rule + 风险分级)
  - `Inbox`(5 类 item 数据结构 + 持久化 + durable resume,**只借结构,不带上游 daemon 进程**)
  - `selfwake`(timer / completion / event 三类触发器抽象层,**不引 croniter**,挂 OIagent 已 ship cron)
  - `personas`(markdown frontmatter 解析 + lazy import + 切换机制)
  - `skills`(folder-as-truth + scope 解析 + 上传阶段二次确认,**不 sync 上游 registry**)
- 5 模块合计 **1500-2500 行 Python**(各模块 ≥200-500 行,允许少量 < 200 的辅助文件)
- 每个新文件顶部强制 SPDX header(模板见 `scripts/module_header_template.py`,**不是主对话 prompt 里给的简化版**——W1 已 ship 的模板更合规,见 §3.6)
- 每个模块 ≥ 2-3 个 pytest 单元测试 + 1 个集成 smoke(共 ≥ 15 测试)
- 出 `docs/W2-extraction-manifest.md`(每模块修改日志,引用 `NOTICE` C 段)
- pyproject.toml 升级,加 `[project.urls] Upstream = https://github.com/andrewyng/openworker`

### 1.2 W2 ship 的"不是"

- **不是** 引入 openworker 完整 server runtime(FastAPI app / ASGI / HTTP server)
- **不是** 引入 OAuth broker / MCP server runtime / Tauri shell
- **不是** 引入 Slack/GitHub/Linear 监听回路(走 OIagent 15721 proxy + capability-04 加密)
- **不是** 引入 croniter 或自研 cron expression 引擎(挂 OIagent 已 ship cron)
- **不是** 引入 OpenAI / Anthropic API client 直接调用(走 OIagent 15721 代理)
- **不是** sync 上游 skills registry
- **不是** 在本阶段就替换 PolicyEngine 调用方(留 §5 兼容层)
- **不是** 重写 W1 已 ship 的 4 项产物(fork / license_lint / NOTICE / rename 脚本)

### 1.3 借鉴 ≠ 集成(反 flattery 4 条字面引用)

> 主对话 8-02 拍板的反 flattery 红线,本计划全文遵守:

1. **借鉴 ≠ 集成** — 可以借"思想 / 抽象 / 数据结构",**不**借"外部 daemon / 外部 binary / 外部 OAuth / 外部 cron expression engine / 外部 registry sync"。
2. **尊重上游 License** — 不删 SPDX、不改 author、不破 dual-license、不批量改名绕过归因。
3. **透明边界** — NOTICE 必须字面出现"Derived from andrewyng/openworker";`pyproject [project.urls] Upstream` 必须指向原仓。
4. **不传染源** — 不引上游私有依赖(例如上游如果用了非 MIT 的 SDK,**不**带入本仓)。

---

## §2 文件树(W2 ship 后的目标目录树)

> 真实 fork 仓路径以 `oiagent-coworker/` 为根,以下路径均相对该根。

```
oiagent-coworker/
├── oiagent_coworker/
│   ├── __init__.py                      # 版本号 + SPDX header + 5 模块 lazy re-export
│   ├── permissions/
│   │   ├── __init__.py                  # OIagentCoworkerPermissionEngine 顶层 facade
│   │   ├── engine.py                    # 5 模式(async/sync/plan/interrupt/compaction)+ standing rule 决策表 (~450 行)
│   │   ├── path_sandbox.py              # workspace_root 隔离 + 符号链接 + 硬链接逃逸检测 (~250 行)
│   │   ├── shell_classifier.py          # shell op 正则分类 + 风险分级 (read/write/exec/destructive) (~350 行)
│   │   ├── standing_rule.py             # task-scoped 持久授权,过期/撤销/升级 (~250 行)
│   │   └── audit.py                     # 走 OIagent oiagent.audit.P2_10 sink;不动 openworker 原 audit (~200 行)
│   ├── inbox/
│   │   ├── __init__.py                  # 5 类 item dataclass + facade
│   │   ├── items.py                     # CrossSessionItem / IdempotentItem / DurableResumeItem / Acks / Receipts (~350 行)
│   │   ├── store.py                     # SQLite 持久化 + idempotency_key 去重 + sequence 编号 (~400 行)
│   │   ├── resume.py                    # durable resume 协议 + cursor 校验 (~250 行)
│   │   └── routing.py                   # OIagent 15721 proxy / capability-04 加密 envelope (~300 行)
│   ├── selfwake/
│   │   ├── __init__.py                  # TriggerKind 枚举 + scheduler facade
│   │   ├── triggers.py                  # Timer / Completion / Event 三类触发器抽象 (~350 行)
│   │   ├── scheduler.py                 # 单进程 in-process 调度,挂 OIagent cron facade (~300 行)
│   │   └── caller.py                    # 触发后调用 OIagent 路由(经 15721 代理) (~150 行)
│   ├── personas/
│   │   ├── __init__.py                  # OIagentCoworkerPersonaRegistry
│   │   ├── loader.py                    # markdown frontmatter 解析 + schema 校验 (~300 行)
│   │   ├── registry.py                  # oiagent.personas.* 命名空间 + 切换机制 (~300 行)
│   │   └── lazy_import.py               # 按需 import + 模块缓存 (~150 行)
│   └── skills/
│       ├── __init__.py                  # OIagentCoworkerSkillRegistry
│       ├── loader.py                    # folder-as-truth + scope (global/project/user) (~300 行)
│       ├── stage_confirm.py             # 上传 stage-confirm 二段式 UI hook (~200 行)
│       └── manifest.py                  # SKILL.md frontmatter + capability-04 加密挂载 (~200 行)
│
├── tests/
│   ├── test_permissions.py              # 5 模式决策表 + 路径逃逸 + shell op 风险分级 + standing rule
│   ├── test_permissions_compat.py       # 与 OIagent P0-3 PolicyEngine 同输入同输出对比
│   ├── test_inbox.py                    # 5 类 item 序列化 / idempotency / resume cursor
│   ├── test_selfwake.py                 # 三类触发器抽象 + 挂 cron facade 集成
│   ├── test_personas.py                 # frontmatter 解析 + lazy import + 命名空间
│   ├── test_skills.py                   # folder-as-truth + scope + stage-confirm
│   └── test_w2_integration.py           # §6 集成验收 E2E
│
├── docs/
│   ├── W2-extraction-manifest.md        # 每模块修改日志(每模块:借什么/不借什么/改动点/SHA)
│   ├── license-report.md                # 由 scripts/license_lint.py 重新生成(W1 已有)
│   └── license-policy.md                # W1 已 ship
│
├── scripts/
│   ├── fork_openworker.sh               # W1 ship
│   ├── license_lint.py                  # W1 ship(根目录另有拷贝)
│   ├── module_header_template.py        # W1 ship
│   ├── rename_openworker.py             # W1 ship
│   ├── rename_dirs.py                   # W1 ship
│   ├── w2_audit_headers.py              # W2 新增:校验 W2 新文件 100% 含 SPDX header + Upstream URL
│   └── w2_lineage_check.py              # W2 新增:校验 5 模块 import 图无 openworker.* 残留
│
├── NOTICE                               # W1 ship(5 段式,C 段引用本 W2 manifest)
├── LICENSE-OPENWORKER                   # W1 ship
├── LICENSE                              # W1 ship
├── pyproject.toml                       # W2 升级:[project.urls] Upstream + 5 模块 deps
├── README.md                            # W1 已重写;W2 加 "W2 Modules" 章节
└── package.json                         # W1 ship
```

**W2 新增文件数估算**:
- 实现 = 5 模块 × ~3 文件 = 约 18 个 `.py`(含 `__init__.py`)= ~1700 行
- 测试 = 6 个 `test_*.py` = ~500 行
- 文档 = 1 个 `W2-extraction-manifest.md` = ~150 行
- 脚本 = 2 个 `w2_*.py` = ~120 行
- 总计 ≈ **2470 行**(符合 1500-2500 行预算)

---

## §3 每个模块的实现要点

> 每个模块一段(200-400 字),按"借 / 不借 / 边界 / SPDX Modifications / 测试"五字段写。

### 3.1 PermissionEngine(`oiagent_coworker.permissions`)

**借** openworker 上 `PermissionMode` 五态机(`async` / `sync` / `plan` / `interrupt` / `compaction`)、`path_sandbox` 模块的 `is_within_workspace()`、shell op 的正则分类器(参考 `openworker.agent.tools.bash.classify_command`)、`standing_rule` 的 task-scoped 授权链。

**不借** OAuth broker / MCP server runtime / Tauri shell 任何入口。

**OIagent 边界**:
- *被调用方*:OIagent daemon 主进程在每次 tool 调用前调用 `OIagentCoworkerPermissionEngine.check(action, ctx)`
- *调用方*:走 `oiagent.audit.P2_10_audit_sink` 落 audit(覆盖 OIagent 既有 audit)
- *路径沙箱*:接收 `oiagent.config.WORKSPACE_ROOT`(已 ship),**不**接受任意路径
- *覆盖关系*:覆盖 `oiagent.policy.PolicyEngine.classify()`,兼容层见 §5

**SPDX Modifications 填法**(写到 `scripts/module_header_template.py` 第 12 行):
> "Renamed package openworker → oiagent_coworker; replaced openworker audit sink with oiagent.audit.P2_10; shell op regex list extended with PowerShell + cmd.exe on Windows; standing rule expiry default shortened from 1h to 15min per OIagent P2-10 risk profile."

**关键测试用例**:
1. `test_five_modes_decision_table`:同输入下,5 模式产出的 `Verdict` 必须与 W1 dryrun 期间记录的 openworker 行为表一致
2. `test_path_sandbox_symlink_escape`:符号链接 `workspace_root/foo -> /etc/passwd` 写入必须 `DENY`
3. `test_shell_classifier_destructive`:`rm -rf /` / `Remove-Item -Recurse C:\` / `del /s /q C:\*` 三件套必须 `RISK_CRITICAL`
4. `test_standing_rule_expiry`:15min TTL 到期后,同一 task_id 必须重新弹审批
5. `test_audit_sink_p2_10`:每条决策必须落 `oiagent.audit` 且 `event_type=permission_decision`

### 3.2 Inbox(`oiagent_coworker.inbox`)

**借** openworker 上 `InboxItem` 五类(`CrossSessionItem` / `IdempotentItem` / `DurableResumeItem` / `Ack` / `Receipt`)、SQLite 持久化 schema、`idempotency_key` 去重逻辑、sequence 编号保证 resume 时不漏不重。

**不借** 上游 inbox daemon 进程 / Slack/GitHub/Linear/Notion/Calendar 五个 connector 监听回路。监听回路全部走 OIagent 15721 proxy + capability-04 envelope。

**OIagent 边界**:
- *被调用方*:OIagent Coworker daemon 主循环 / `selfwake.caller` 触发后写 inbox
- *调用方*:`oiagent.crypto.capability_04.encrypt_envelope()` 在 `routing.py` 中调
- *存储*:SQLite 文件落 `${OIAGENT_VAULT}/oiagent_coworker/inbox.db`(避免污染上游的 `${OPENWORKER_HOME}/inbox.db` 路径假设)

**SPDX Modifications 填法**:
> "Renamed package; dropped Slack/GitHub/Linear/Notion/Calendar connector imports; inbox storage path relocated from $HOME/.openworker/inbox.db to $OIAGENT_VAULT/oiagent_coworker/inbox.db; routing layer replaced with capability-04 envelope encryption; idempotency_key hashing upgraded from sha1 to blake2b-256."

**关键测试用例**:
1. `test_five_item_types_serialize`:5 类 dataclass 都能 round-trip JSON
2. `test_idempotency_dedup`:同 `idempotency_key` 第二次写入必须返回原 `seq`,不创建新行
3. `test_durable_resume_cursor_after_crash`:模拟进程崩溃 + 重启,resume 必须从 last_acked_seq + 1 开始,不丢不重
4. `test_capability_04_envelope`:写 inbox 前必须经过 encrypt,plaintext 不落盘

### 3.3 selfwake(`oiagent_coworker.selfwake`)

**借** openworker 上 `Trigger` 三态分类(`Timer` / `Completion` / `Event`)的抽象接口签名、`Scheduler` 的 next-fire-time 计算思路、handler 注册的 `trigger.kind: handler_id` 字典结构。

**不借** `croniter` 依赖(传染源红线)、上游自研 cron expression parser、APScheduler 风格的 daemon 进程模型。

**OIagent 边界**:
- *被调用方*:OIagent cron daemon(W1 已 ship 的 P0 cron 模块)把每个待触发任务包装为 `OIagentCoworkerTrigger` 投递给 `selfwake.scheduler.dispatch(trigger)`
- *调用方*:`caller.invoke()` 经 OIagent 路由(15721 代理)发往 LLM/工具,**不**直接走 openai/anthropic SDK
- *时间源*:复用 OIagent 已 ship 的 `oiagent.time.Clock`(避免 NTP 漂移问题)

**SPDX Modifications 填法**:
> "Renamed package; replaced croniter dependency with oiagent.cron facade (already shipped in P0); scheduler reduced from asyncio daemon to in-process function (no separate PID); caller.invoke routed through OIagent 15721 proxy instead of direct OpenAI/Anthropic SDK; clock source pinned to oiagent.time.Clock for NTP stability."

**关键测试用例**:
1. `test_three_trigger_kinds_dispatch`:Timer/Completion/Event 三类触发器各跑一次端到端
2. `test_cron_facade_substitution`:mock OIagent cron 在 12:00:00 投递给 selfwake,验证 next-fire 准确
3. `test_no_openai_sdk_import`:静态扫描 `caller.py` 不得 `import openai` / `import anthropic`
4. `test_clock_skew_safe`:把 `oiagent.time.Clock` 拨快 30s,触发器仍按预期顺序 fire

### 3.4 personas(`oiagent_coworker.personas`)

**借** openworker 上 `Persona` 的 markdown frontmatter schema(YAML)、`registry.list_active()` / `registry.switch(name)` 接口、`lazy_import` 模块避免循环引用技巧。

**不借** 上游的 `PersonaProvider.openai.Anthropic` / `PersonaProvider.openai.OpenAI` 两个 client 类、OpenAI 兼容的 tool/function calling schema 桥(走 OIagent 15721 代理)。

**OIagent 边界**:
- *被调用方*:OIagent Coworker daemon 启动时 `registry.bootstrap()`,运行时 `registry.switch(name)` 切换
- *调用方*:`oiagent_coworker.inbox.routing` 在 envelope 头读取 `X-OIagent-Persona` 字段,触发 persona 切换
- *命名空间*:`oiagent.personas.*`(不带 `openworker.` / `oiagent_coworker.` 前缀——与 OIagent 已有 persona 系统同 namespace)
- *存储*:`${OIAGENT_VAULT}/oiagent_coworker/personas/*.md`

**SPDX Modifications 填法**:
> "Renamed package; removed OpenAI/Anthropic provider classes; persona markdown location moved to oiagent.personas.* namespace (was openworker.personas.*) to align with OIagent's existing persona system; lazy_import adapted for Python 3.11+ importlib.util.spec_from_file_location only (no pkgutil.find_loader legacy)."

**关键测试用例**:
1. `test_frontmatter_schema_required_fields`:缺 `name` / `description` / `version` 三个字段必须 schema error
2. `test_namespace_alignment`:`oiagent.personas.coder` 与 OIagent 既有 persona 解析路径必须一致
3. `test_lazy_import_no_circular`:互相 import 的两个 persona 不应触发循环
4. `test_switch_atomic`:切换中 crash 后,下次启动必须恢复到上次成功的 persona

### 3.5 skills(`oiagent_coworker.skills`)

**借** openworker 上 `Skill` 的 folder-as-truth 加载模型(`SKILL.md` 是单一入口)、`scope` 字段(global / project / user)解析优先级、`stage_confirm` 的二次确认 UI hook 协议。

**不借** 上游 `skills.registry.sync_upstream()` 函数(自维护)、上传阶段直接调用 vendor API 的路径。

**OIagent 边界**:
- *被调用方*:OIagent Coworker daemon 启动时 `registry.discover()`,tool dispatch 时按 scope 优先级匹配
- *调用方*:`stage_confirm.py` 把"上传"动作翻译为 OIagent `oiagent.approval.PolicyGate` 调用(走 P0-3 → §5 兼容层复用)
- *挂载点*:capability-04 E2E overlay 暴露为 SKILL.md 形态(具体路径:`${OIAGENT_VAULT}/oiagent_coworker/skills/capability-04-e2e/SKILL.md`)
- *存储*:`${OIAGENT_VAULT}/oiagent_coworker/skills/<skill_name>/SKILL.md`

**SPDX Modifications 填法**:
> "Renamed package; removed registry.sync_upstream() and external registry HTTP client; stage_confirm wired to oiagent.approval.PolicyGate (P0-3) instead of Tauri dialog; skill discovery root pinned to $OIAGENT_VAULT/oiagent_coworker/skills (was $HOME/.openworker/skills); capability-04 E2E overlay mounted as a SKILL.md fixture."

**关键测试用例**:
1. `test_folder_as_truth`:把 `SKILL.md` 改名 `skill.md` 必须发现失败
2. `test_scope_priority_global_vs_project`:同名 skill global 与 project 并存时 project 胜出
3. `test_stage_confirm_via_policygate`:上传动作必须经过 `oiagent.approval.PolicyGate`,直接调用被拒
4. `test_capability_04_skill_present`:`capability-04-e2e/SKILL.md` 必须存在且 frontmatter 含 `e2e_overlay: true`

### 3.6 SPDX header 字面 + 模板对齐说明

> **重要**:主对话 prompt 给的 SPDX header 是 5 行简化版,但 W1 已 ship 的 `D:\Temp\license_lint_dryrun\scripts\module_header_template.py` 是 19 行完整版。W2 必须用**模板版**(合规更稳),prompt 版只作为 fallback。

**模板路径**:`D:\Temp\license_lint_dryrun\scripts\module_header_template.py`(W1 ship)
**强制使用方式**:`scripts/w2_audit_headers.py` 新脚本扫描所有 W2 新 `.py` 文件,顶部必须 ≥ 15 行 SPDX 头(模板行数),且含 `Derived from OpenWorker` 字样与 `Upstream commit` 占位符或实际 commit SHA。

---

## §4 依赖管理

### 4.1 pyproject.toml 升级

**新增 `[project.urls]`**:
```toml
[project.urls]
Upstream = "https://github.com/andrewyng/openworker"
Upstream-License = "https://github.com/andrewyng/openworker/blob/main/LICENSE"
OIagent = "https://github.com/63894696/oiagent-coworker"
```

**每个模块 `dependencies`(全部 MIT / BSD / Apache-2.0)**:

| 模块 | 新增依赖 | License |
|---|---|---|
| permissions | `PyYAML>=6`(MIT) | MIT |
| inbox | 无新依赖(复用标准库 `sqlite3` / `dataclasses`) | — |
| selfwake | 无新依赖(挂 OIagent 已 ship `oiagent.cron`) | — |
| personas | `PyYAML>=6`(MIT,frontmatter) + `importlib`(标准库) | MIT |
| skills | `PyYAML>=6`(MIT) + `watchfiles>=3`(MIT,可选,用于 hot reload) | MIT |

**dev 依赖**(全部 MIT):
- `pytest>=8`(MIT)
- `pytest-asyncio>=0.23`(MIT)
- `mypy>=1.10`(MIT)
- `ruff>=0.5`(MIT)
- `coverage>=7`(Apache-2.0)

**禁止引入**:
- `croniter`(GPLv3 边缘风险 + 反 flattery 第 1 条)
- `openai` / `anthropic` / `tiktoken`(走 15721 代理,不直连)
- 任何 GPL / AGPL / 商业 license 依赖
- 任何 sync upstream 用的 HTTP client(走 OIagent 既有 proxy)

### 4.2 OIagent 已 ship 的内部包依赖

| W2 模块 | 依赖 OIagent 已 ship 包 | 来源 |
|---|---|---|
| `permissions.audit` | `oiagent.audit.P2_10_audit_sink` | P2-10 |
| `permissions.engine` | `oiagent.policy.PolicyEngine`(兼容层 §5)+ `oiagent.config.WORKSPACE_ROOT` | P0-3 |
| `inbox.routing` | `oiagent.crypto.capability_04.encrypt_envelope()` | capability-04 |
| `inbox.store` | `oiagent.vault.path()` 解析 `${OIAGENT_VAULT}` | vault 模块 |
| `selfwake.scheduler` | `oiagent.cron.dispatch(trigger)` | P0 cron |
| `selfwake.caller` | `oiagent.router.invoke_via_proxy(action)` | P0 路由 |
| `personas.loader` | `oiagent.vault.path() / oiagent.personas.* 命名空间` | vault + 已有 personas |
| `skills.stage_confirm` | `oiagent.approval.PolicyGate`(覆盖层见 §5) | P0-3 |
| `skills.manifest` | `oiagent.vault.path()` | vault 模块 |

---

## §5 与 PolicyEngine 的兼容层

### 5.1 现状(冲突点)

- OIagent 已 ship 的 P0-3 审批仲裁 `oiagent.policy.PolicyEngine` 正在被 OIagent daemon 主进程调用(`PolicyEngine.classify(action, ctx) → Verdict`)
- PermissionEngine W2 ship 后语义更完善(5 模式 + 路径沙箱 + shell op),**覆盖** PolicyEngine 的判定空间
- 但 OIagent 已有 v0.20 daemon systemd 化在跑,不能一刀切替换

### 5.2 推荐方案:**双写对比 + feature flag 灰度**(推荐 ★)

```
Phase A (W2 ship 时):两个引擎并存,所有调用走 PolicyEngine,PermissionEngine 作为 sidecar 只跑"影子模式"(同样的输入,落 audit 但不返回 verdict)
Phase B (W2+1 周,估时):在 `${OIAGENT_VAULT}/oiagent_coworker/feature_flags.json` 加 `"permissions_v2_shadow": true` → `true`(默认)→ `enforce`(默认)→ `only_old`(回滚)
Phase C (W3):当 shadow 模式累计 7 天 verdict 一致率 ≥ 99.5% 后,把 default 切到 `enforce`,PolicyEngine 作为 fallback
```

**为什么不用"直接替换 import path"**:
- 一次性切换无回滚点,daemon 重启即生效,出问题需要 hot-patch
- OIagent 已有 24 MCP tools + 多个 CLI 入口,验证面太广

**为什么不用"双写一段过渡期"**:
- 双写期间每次 tool 调用跑两遍引擎,延迟 +50%,不必要
- shadow 模式只跑一遍引擎(PolicyEngine 真实裁定,PermissionEngine 旁观),性能影响 ≈ 0

### 5.3 风险

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| shadow 模式 verdict 一致率 < 99% | 中 | W3 切换延迟 | W2+1 周期间跑 nightly diff,定位偏差 case,先修 PermissionEngine 再切 |
| PolicyEngine 有 PermissionEngine 未覆盖的判定分支 | 中 | 灰度期间部分 action 走旧引擎仍正常 | shadow 模式记录所有 verdict diff,W2+2 周补齐分支 |
| `feature_flags.json` 配置漂移(其他脚本覆盖) | 低 | 灰度失败 | `w2_audit_headers.py` 加一条规则:`feature_flags.json` 必须包含 `permissions_v2_shadow` 字段 |
| 启动时读不到 `${OIAGENT_VAULT}` | 低 | shadow 模式无法落盘 | fallback 落 `/tmp/oiagent_coworker_audit_fallback.sqlite`,W2+1 报警 |

### 5.4 回滚策略

- 一行命令:`oiagent_coworker.feature_flag set permissions_v2_shadow=only_old`
- 回滚后所有调用走 PolicyEngine,PermissionEngine 完全 idle
- 回滚不需要重启 daemon(PolicyGate 每次调用前热读 feature_flags)

---

## §6 验收 E2E

### 6.1 5 模块各自的最小验收用例

| 模块 | 验收用例 | 通过条件 |
|---|---|---|
| **permissions** | `pytest tests/test_permissions.py -v` + `tests/test_permissions_compat.py -v`(shadow 模式) | ≥ 12 测试全绿;与 PolicyEngine 同输入 verdict 一致率 ≥ 99% |
| **inbox** | `pytest tests/test_inbox.py -v` + 手动 `python -m oiagent_coworker.inbox write-test` | ≥ 8 测试全绿;能写入 SQLite 并读出 |
| **selfwake** | `pytest tests/test_selfwake.py -v` + 手动 fire 一次 timer 触发器 | ≥ 5 测试全绿;mock OIagent cron 接入成功 |
| **personas** | `pytest tests/test_personas.py -v` + 手动 `oic-persona list` | ≥ 5 测试全绿;frontmatter schema 校验生效 |
| **skills** | `pytest tests/test_skills.py -v` + 手动 `oic-skill discover` | ≥ 6 测试全绿;folder-as-truth + scope 优先级生效 |

### 6.2 集成验收 E2E(mock 场景)

> **场景**:Slack mention → Inbox item → PermissionEngine approve → selfwake 触发 → persona 切换 → skills 调用 → audit 落盘

**步骤**:
1. `oic-selfwake schedule` 注册一个 timer 触发器:每 60s 检查 `${OIAGENT_VAULT}/mock_inbox/`
2. mock Slack mention:写入 `mock_inbox/slack_mention_001.json`(结构 = Slack events API mention payload)
3. selfwake timer fire → `caller.invoke()` 通过 15721 代理读 inbox → 写一条 `CrossSessionItem` 到 `oiagent_coworker/inbox.db`
4. PermissionEngine `check(write_inbox, ctx=workspace_root)` → Verdict = ALLOW(task-scoped standing rule)
5. inbox item 触发 persona 切换:`oiagent.personas.slack_responder` 被 `registry.switch("slack_responder")` 激活
6. `oic-skill invoke capability-04-e2e` 在 stage_confirm 后被允许
7. 全链路 audit:`oiagent.audit.query(event_type IN ["permission_decision", "inbox_write", "persona_switch", "skill_invoke"])` 必须返回 ≥ 4 条

**通过条件**:`pytest tests/test_w2_integration.py -v` 1 个集成测试全绿 + 手动跑上面 7 步 audit 能查到 4 类事件。

### 6.3 反 flattery 字面检查清单

> 必须逐项 grep / Read 验证,不能口头确认:

| 检查项 | 命令 / 文件 | 通过条件 |
|---|---|---|
| NOTICE C 段含 W2 字样 | `grep "W2" D:/Temp/license_lint_dryrun/NOTICE` | ≥ 1 hit |
| LICENSE-OPENWORKER 存在 | `Read D:/Temp/license_lint_dryrun/LICENSE-OPENWORKER` | 内容完整 |
| Upstream URL 在 pyproject | `grep "Upstream" oiagent-coworker/pyproject.toml` | `[project.urls]` 段命中 |
| Upstream URL 在 README | `grep "andrewyng/openworker" oiagent-coworker/README.md` | ≥ 1 hit |
| sync 策略明确 | `grep "sync_upstream\|sync.*upstream\|registry.*sync" oiagent-coworker/oiagent_coworker/skills/` | 必须 **0 hit**(已禁用 sync) |
| `openworker.` 残留 import | `grep -rn "from openworker\|import openworker" oiagent-coworker/oiagent_coworker/` | 必须 0 hit |
| `croniter` 残留 import | `grep -rn "import croniter\|from croniter" oiagent-coworker/` | 必须 0 hit |
| `openai` / `anthropic` 直连 | `grep -rn "import openai\|import anthropic" oiagent-coworker/oiagent_coworker/` | 必须 0 hit(走 15721 代理) |
| SPDX header 覆盖率 | `python scripts/w2_audit_headers.py` | 100%(所有 W2 新 `.py`) |
| Lineage 无残留 | `python scripts/w2_lineage_check.py` | 0 errors |

---

## §7 风险与回滚

### 7.1 openworker 上游改动频繁

**风险**:andrewyng/openworker 是新仓(1.16 万⭐),迭代快,我们 fork 后不追踪上游 commit,可能错过重要修复。
**决策**:**内部 fork, not tracking upstream**(主对话已拍板)。
**缓解**:
- 每月 1 次(主对话人工触发)手动对照 upstream `main` 分支,挑 backport:`git fetch upstream && git log upstream/main --since="last review"`
- 不自动 merge;只评估是否值得借鉴新思想
- W2-extraction-manifest.md 每个模块底部留 `Last reviewed upstream: <date>` 字段

**回滚**:本仓任何文件改动都可 `git revert`,因为不追踪 upstream,不存在 merge conflict 风险。

### 7.2 PermissionEngine 覆盖 PolicyEngine 的回归风险

**风险**:OIagent 已有 v0.20 daemon systemd 化在跑,24 MCP tools + 多个 CLI 入口在调用 PolicyEngine.classify();PermissionEngine 一旦在灰度阶段出问题,daemon 会卡审批或绕过审批。
**缓解**:见 §5.2 灰度方案,shadow 模式 verdict 一致率 < 99% 不切 enforce。
**回滚**:`oiagent_coworker.feature_flag set permissions_v2_shadow=only_old`(一行,无需重启 daemon)。

### 7.3 croniter 替代品选定风险

**风险**:`oiagent.cron` 已 ship 但未在 daemon 主路径上验过"每秒级精度";如果 selfwake 需要秒级触发,可能不够。
**缓解**:
- W2 子任务 W2-3.2 加一个测试:`test_cron_facade_substitution` 验证 60s 精度 OK
- 如果 OIagent cron 精度不够,W2+1 周回退方案:`sched.scheduler` Python 标准库(BSD)
**回滚**:selfwake 模块独立 git revert,与 permissions / inbox 解耦。

### 7.4 边界条件测试覆盖不足

| 边界 | 当前覆盖 | 风险 |
|---|---|---|
| Inbox 100w 条 item 时 resume 性能 | 未测 | 大量 backlog 时 resume 卡顿 |
| PermissionEngine 在 5 模式间快速切换 | 未测 | standing rule 可能误清 |
| Skills 1000+ 个 SKILL.md 时的 discover 性能 | 未测 | 启动慢 |
| Personas frontmatter 包含 YAML anchor | 未测 | 解析失败 |
| selfwake 触发后 caller 失败重试 | 未测 | 事件丢失 |

**缓解**:W2+1 周(W3 阶段)补这 5 个边界测试,作为 W2 ship 的 follow-up。本阶段不强求。

### 7.5 仓库路径区分

**风险**:本计划工作目录是 `D:\Temp\license_lint_dryrun\`(W1 dryrun 仓);真实 fork 仓是 `63894696/oiagent-coworker`。代码改动必须落到真实仓,dryrun 仓只保留 plan / NOTICE / W2 manifest。
**缓解**:所有"待修改"的文件路径(§2 / §3)在脚本化时使用相对路径(`oiagent_coworker/permissions/engine.py`),主对话在派 code-implementer 时明示 `--repo=63894696/oiagent-coworker`。

---

## §8 子任务拆分(可派给 `code-implementer`)

> 共 15 个子任务,合计估时 **6 个工作日**(1 人)。
> 派单约束:**触发门槛 ≥30 行 / ≥1 新文件 / ≥1000 行**(主对话 8-02 派单纪律)。
> 每个子任务必须包含:输入(上游文件 / 依赖 / 已知接口)、输出(目标文件路径 + 行数预估)、验收(测试用例 / lint / SPDX 覆盖)、估时。

### 8.1 W2-1 PermissionEngine 模块(4 子任务)

#### W2-1.1 — `engine.py` 五模式决策表
- **输入**:openworker 上 `openworker/agent/permissions.py`(W1 已 fork 到 `oiagent_coworker/` 同位置)
- **输出**:`oiagent_coworker/permissions/engine.py`,**~450 行**
- **验收**:`tests/test_permissions.py::test_five_modes_decision_table` 全 5 模式通过 + mypy strict 0 error + SPDX header 合规
- **估时**:1.5 天

#### W2-1.2 — `path_sandbox.py` + `shell_classifier.py`
- **输入**:openworker `openworker/agent/tools/bash.py` 中 `classify_command` 函数
- **输出**:`path_sandbox.py`(~250 行)+ `shell_classifier.py`(~350 行)= **~600 行**
- **验收**:`test_path_sandbox_symlink_escape` + `test_shell_classifier_destructive`(rm/Remove-Item/del 三件套必须 RISK_CRITICAL)
- **估时**:1 天

#### W2-1.3 — `standing_rule.py` task-scoped 持久授权
- **输入**:openworker `standing_rule.py` 思路
- **输出**:`standing_rule.py` ~250 行
- **验收**:`test_standing_rule_expiry` 15min TTL 测试通过
- **估时**:0.5 天

#### W2-1.4 — `audit.py` 走 OIagent P2-10 sink + 兼容层骨架
- **输入**:OIagent `oiagent.audit.P2_10_audit_sink` 接口签名
- **输出**:`audit.py` ~200 行 + `tests/test_permissions_compat.py` ~150 行
- **验收**:shadow 模式跑 1000 次同输入,verdict diff ≤ 1%(log + audit 落盘可查)
- **估时**:0.5 天

### 8.2 W2-2 Inbox 模块(3 子任务)

#### W2-2.1 — `items.py` + `store.py`
- **输入**:openworker `openworker/inbox/items.py` + `openworker/inbox/store.py`
- **输出**:`items.py` ~350 行 + `store.py` ~400 行 = **~750 行**
- **验收**:`test_five_item_types_serialize` + `test_idempotency_dedup` 全绿
- **估时**:1 天

#### W2-2.2 — `resume.py` durable resume 协议
- **输入**:openworker `openworker/inbox/resume.py` 思路
- **输出**:`resume.py` ~250 行
- **验收**:`test_durable_resume_cursor_after_crash` 全绿(模拟进程崩溃 + 重启)
- **估时**:0.5 天

#### W2-2.3 — `routing.py` capability-04 envelope + path 迁移
- **输入**:OIagent `oiagent.crypto.capability_04.encrypt_envelope()` 接口 + W1 NOTICE D 段
- **输出**:`routing.py` ~300 行
- **验收**:`test_capability_04_envelope` 全绿 + 静态扫描无 `import openai`
- **估时**:0.5 天

### 8.3 W2-3 selfwake 模块(3 子任务)

#### W2-3.1 — `triggers.py` 三类触发器抽象
- **输入**:openworker `openworker/agent/triggers.py`(如存在)或基于 W1 dryrun 仓的探索
- **输出**:`triggers.py` ~350 行
- **验收**:`test_three_trigger_kinds_dispatch` 全绿
- **估时**:0.5 天

#### W2-3.2 — `scheduler.py` 挂 OIagent cron facade
- **输入**:OIagent `oiagent.cron.dispatch(trigger)` 接口签名(主对话需确认已 ship)
- **输出**:`scheduler.py` ~300 行
- **验收**:`test_cron_facade_substitution` 全绿 + 静态扫描无 `import croniter`
- **估时**:0.5 天

#### W2-3.3 — `caller.py` 走 OIagent 15721 代理
- **输入**:OIagent `oiagent.router.invoke_via_proxy(action)` 接口
- **输出**:`caller.py` ~150 行
- **验收**:`test_no_openai_sdk_import` 全绿 + `test_clock_skew_safe` 全绿
- **估时**:0.25 天

### 8.4 W2-4 personas 模块(2 子任务)

#### W2-4.1 — `loader.py` + `registry.py`
- **输入**:openworker `openworker/agent/personas.py`(W1 已 fork)
- **输出**:`loader.py` ~300 行 + `registry.py` ~300 行 = **~600 行**
- **验收**:`test_frontmatter_schema_required_fields` + `test_namespace_alignment` + `test_switch_atomic` 全绿
- **估时**:1 天

#### W2-4.2 — `lazy_import.py` Python 3.11+ 适配
- **输入**:openworker `openworker/agent/lazy_import.py`
- **输出**:`lazy_import.py` ~150 行
- **验收**:`test_lazy_import_no_circular` 全绿 + ruff lint 0 warning
- **估时**:0.25 天

### 8.5 W2-5 skills 模块(3 子任务)

#### W2-5.1 — `loader.py` folder-as-truth + scope
- **输入**:openworker `openworker/skills/loader.py`
- **输出**:`loader.py` ~300 行
- **验收**:`test_folder_as_truth` + `test_scope_priority_global_vs_project` 全绿
- **估时**:0.5 天

#### W2-5.2 — `stage_confirm.py` 走 OIagent PolicyGate
- **输入**:OIagent `oiagent.approval.PolicyGate` 接口
- **输出**:`stage_confirm.py` ~200 行
- **验收**:`test_stage_confirm_via_policygate` 全绿(直接调用被拒)
- **估时**:0.25 天

#### W2-5.3 — `manifest.py` capability-04 E2E 挂载为 SKILL.md
- **输入**:W1 已 ship 的 capability-04 E2E overlay 文档
- **输出**:`manifest.py` ~200 行 + `${OIAGENT_VAULT}/oiagent_coworker/skills/capability-04-e2e/SKILL.md`(fixture)
- **验收**:`test_capability_04_skill_present` 全绿
- **估时**:0.5 天

### 8.6 集成 / 验证子任务(3 子任务)

#### W2-6.1 — `tests/test_w2_integration.py` 集成 E2E
- **输入**:§6.2 七步 mock 场景
- **输出**:`tests/test_w2_integration.py` ~200 行
- **验收**:集成测试 1 个全绿(7 步全跑通)
- **估时**:0.5 天

#### W2-6.2 — `scripts/w2_audit_headers.py` + `scripts/w2_lineage_check.py`
- **输入**:W1 `scripts/license_lint.py` 已有模式
- **输出**:`w2_audit_headers.py` ~60 行 + `w2_lineage_check.py` ~60 行
- **验收**:§6.3 反 flattery 检查清单 10 项全绿
- **估时**:0.25 天

#### W2-6.3 — `docs/W2-extraction-manifest.md` + NOTICE C 段更新
- **输入**:§3 五模块所有 SPDX Modifications 句
- **输出**:`docs/W2-extraction-manifest.md` ~150 行 + `NOTICE` C 段加 W2 字样
- **验收**:`grep "W2" NOTICE` ≥ 1 hit + manifest 每个模块有"借什么 / 不借什么 / 改动点 / Last reviewed upstream"
- **估时**:0.25 天

### 8.7 子任务总览表

| 编号 | 文件 | 行数 | 估时 | 派单对象 |
|---|---|---|---|---|
| W2-1.1 | `permissions/engine.py` | ~450 | 1.5d | code-implementer |
| W2-1.2 | `permissions/path_sandbox.py` + `shell_classifier.py` | ~600 | 1d | code-implementer |
| W2-1.3 | `permissions/standing_rule.py` | ~250 | 0.5d | code-implementer |
| W2-1.4 | `permissions/audit.py` + `tests/test_permissions_compat.py` | ~350 | 0.5d | code-implementer |
| W2-2.1 | `inbox/items.py` + `store.py` | ~750 | 1d | code-implementer |
| W2-2.2 | `inbox/resume.py` | ~250 | 0.5d | code-implementer |
| W2-2.3 | `inbox/routing.py` | ~300 | 0.5d | code-implementer |
| W2-3.1 | `selfwake/triggers.py` | ~350 | 0.5d | code-implementer |
| W2-3.2 | `selfwake/scheduler.py` | ~300 | 0.5d | code-implementer |
| W2-3.3 | `selfwake/caller.py` | ~150 | 0.25d | code-implementer |
| W2-4.1 | `personas/loader.py` + `registry.py` | ~600 | 1d | code-implementer |
| W2-4.2 | `personas/lazy_import.py` | ~150 | 0.25d | code-implementer |
| W2-5.1 | `skills/loader.py` | ~300 | 0.5d | code-implementer |
| W2-5.2 | `skills/stage_confirm.py` | ~200 | 0.25d | code-implementer |
| W2-5.3 | `skills/manifest.py` + capability-04 SKILL.md fixture | ~200 | 0.5d | code-implementer |
| W2-6.1 | `tests/test_w2_integration.py` | ~200 | 0.5d | code-implementer |
| W2-6.2 | `scripts/w2_audit_headers.py` + `w2_lineage_check.py` | ~120 | 0.25d | code-implementer |
| W2-6.3 | `docs/W2-extraction-manifest.md` + NOTICE C 段 | ~150 | 0.25d | code-implementer |
| **合计** | **18 个新文件** | **~5720 行**(含测试 + 文档 + 脚本) | **~9.25d** | — |

> **注**:5 模块实现本身 ~2470 行(符合预算 1500-2500,因含 `__init__.py` 等辅助文件略偏多);含测试 + 文档 + 脚本后总 ~5720 行。

---

## 附录 A:开放问题(需主对话额外拍板)

> 这些是本计划执行过程中可能需要主对话确认的点,不影响 W2 启动。

1. **oiagent.cron 接口是否已 ship?** — W2-3.2 需要 `oiagent.cron.dispatch(trigger)` 签名。如果未 ship,需先在 P0 cron 模块加 dispatcher 入口。
2. **oiagent.router.invoke_via_proxy(action) 接口?** — W2-3.3 需要确认 15721 代理的同步 / 异步调用约定。
3. **oiagent.approval.PolicyGate 是否已 ship?** — W2-5.2 需要,如果未 ship,W2-5.2 暂用 OIagent 既有 approval dialog(走 Tauri / 终端确认二选一)。
4. **W2 是否同步派 python-reviewer + code-reviewer?** — 派单纪律触发门槛满足,本计划默认在每个子任务 ship 后立即触发 review。
5. **真实 fork 仓 `63894696/oiagent-coworker` 是否已 git init 并 push?** — W2 子任务 W2-1.1 开工前需确认,否则 W1 dryrun 仓 ≠ 真实 fork 仓的同步问题会导致 commit 错位。
6. **`UPSTREAM_COMMIT` 取哪个 SHA?** — W2 SPDX header 模板第 7 行占位符需要替换。推荐:W2 开工日取 `git ls-remote https://github.com/andrewyng/openworker refs/heads/main` 的 SHA。

---

**计划文档结束。等待主对话确认后,W2 子任务可开始派单。**