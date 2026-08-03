# W2 Extraction Manifest

> 本 manifest 记录 W2 阶段从 `andrewyng/openworker` (MIT) 抽取的每个模块的
> 借鉴边界、改动点、以及上游 commit 参考。每个模块对应一份 SPDX Modifications
> 说明,供 compliance reviewer 审阅。
>
> 上游仓: `https://github.com/andrewyng/openworker`
> 上游 commit: `01b6f83b3927e02912dda84bb392942c13ca70d1`
> 提取日期: 2026-08-02
> 工作目录: `D:\Temp\license_lint_dryrun\`

---

## W2-1.1 `permissions/engine.py`

**借鉴来源**: `openworker/agent/permissions.py` (PermissionMode 五态机)
**不借**: OAuth broker / Tauri shell / MCP server runtime

**改动点**:
- 重命名包 `openworker.agent.permissions` → `oiagent_coworker.permissions.engine`
- 移除上游 `PermissionMode.async` 与 `compaction` 模式(OIagent 无异步审批路径)
- 新增 `StandingRule` 任务级持久授权,替代上游基于 user-level 的静态授权
- 5 模式决策表重写:去掉 upstream 的 `risk_level` 字符串匹配,改为结构化 `Verdict` dataclass

**Last reviewed upstream**: 2026-08-02

---

## W2-1.2 `permissions/path_sandbox.py` + `permissions/shell_classifier.py`

**借鉴来源**: `openworker/agent/tools/bash.py` 中 `classify_command` 函数 + `path_sandbox` 模块
**不借**: upstream `bash.py` 中的 `run_command()` 执行路径(完全不引入)

**改动点**:
- `classify_command()` 重写为 `ShellClassifier`,支持 PowerShell / cmd.exe / fish / bash 四 shell
- `is_within_workspace()` 重写为 `PathSandbox.is_safe()`,增加 symlink 逃逸检测
- 移除上游基于 `/workspace/` 硬编码白名单的逻辑,改为基于 `workspace_root` 参数动态校验

**Last reviewed upstream**: 2026-08-02

---

## W2-1.3 `permissions/standing_rule.py`

**借鉴来源**: upstream `standing_rule` 思路(task-scoped 持久授权)
**不借**: upstream `standing_rule` 的具体实现(完全重写)

**改动点**:
- TTL 从上游的 1h 缩短为 15min(符合 OIagent P2-10 风险 profile)
- 存储从 JSON 文件改为 append-only JSONL(与 inbox/selfwake 一致)
- 移除 upstream `StandingRule.accumulate()` 幂等逻辑(OIagent 不需要累积授权)

**Last reviewed upstream**: 2026-08-02

---

## W2-1.4 `permissions/audit.py`

**借鉴来源**: upstream `openworker/agent/audit.py` 的 `(args, kwargs)` duck type
**不借**: upstream audit 的 HTTP sink / file sink 具体实现

**改动点**:
- 将 loose duck type 替换为 typed Protocol (`AuditSink`) + tagged-union envelope (`AuditDecision`)
- 新增 `OIagentCoworkerAuditFacade` 作为唯一集成入口
- 适配适配器 `for_path_sandbox_with_original()` / `for_shell_classifier_with_target()`
- 每个 W2 模块(subsystem)新增 envelope kind: `inbox`, `selfwake`, `persona`, `skill`

**Last reviewed upstream**: 2026-08-02

---

## W2-2 Inbox 模块

**借鉴来源**: `openworker/inbox/items.py` (5 类 item 结构) + `openworker/inbox/store.py` (SQLite backend)
**不借**: upstream SQLite schema / OAuth connector routing

**改动点**:
- 后端从 SQLite 替换为 append-only JSONL (`OIagentCoworkerInboxPersistence`)
- 移除 upstream `InboxItem.source` 的 Slack/GitHub/Linear/Notion/Calendar 枚举
- 新增 `idempotency_key` 基于 blake2b-256(上游为 sha1)
- `resume.py` 的 cursor 逻辑重写,适配 OIagent 的 envelope_id 序列

**Last reviewed upstream**: 2026-08-02

---

## W2-3 SelfWake 模块

**借鉴来源**: `openworker/agent/triggers.py` (Trigger 三态:Timer/Completion/Event) + `openworker/agent/scheduler.py` (next-fire-time 计算思路)
**不借**: upstream scheduler 的 `croniter` 依赖 / asyncio 循环集成

**改动点**:
- 用简化版 CRON 匹配替换 `croniter`(符合无新依赖约束)
- 移除 upstream `Scheduler.tick()` 的 asyncio dispatch;改为同步 `tick()`
- `caller.py` 移除 upstream Tauri shell 调用,改为通过 OIagent 15721 代理

**Last reviewed upstream**: 2026-08-02

---

## W2-4 Personas 模块

**借鉴来源**: `openworker/agent/personas/registry.py` (lazy import + registry) + `openworker/agent/personas/loader.py` (markdown frontmatter 解析)
**不借**: upstream `PersonaProvider.openai.Anthropic` / `PersonaProvider.openai.OpenAI` 类

**改动点**:
- 重命名包 `openworker.agent.personas` → `oiagent_coworker.persona`(单数)
- 移除所有 OpenAI/Anthropic provider 类
- `registry.switch(name)` 重写为同步方法
- YAML frontmatter 解析改为 `yaml.safe_load`(上游为 `yaml.load` + unsafe)

**Last reviewed upstream**: 2026-08-02

---

## W2-5 Skills 模块

**借鉴来源**: `openworker/agent/skills/service.py` (registry + lazy import) + `openworker/agent/skills/loader.py` (folder-as-truth + scope 思路)
**不借**: upstream stage_confirm gate / aisuite 集成

**改动点**:
- 重命名包 `openworker.agent.skills` → `oiagent_coworker.skills`
- W2-5.1 (2026-08-02): 重新引入 `loader.py` — SKILL.md folder-as-truth 发现 + global/project/user scope 优先级解析;YAML frontmatter 解析复用 persona.persistence 的 regex + yaml.safe_load 模式
- W2-5.2 (2026-08-03): `stage_confirm.py` 已 ship — duck-typed 注入 PolicyGate(P0-3 §5 兼容层),取代上游 Tauri 确认对话框;`invoke_skill_with_confirm` 为唯一受制裁调用路径,fail-closed,直接调用被拒;11 测试全绿
- W2-5.3 (2026-08-03): `manifest.py` 已 ship — SKILL.md markdown-body digest + `e2e_overlay` declaration 检查;`OIagentCoworkerSkillManifest(skills_root)` 注入 `skills_root`,不解析 `${OIAGENT_VAULT}`(对齐 PolicyGate 注 `flags_path` 惯例);复用 loader 私有 `_FRONT_MATTER_PATTERN`(单一 regex 源);零 audit;12 测试全绿;fixture `capability-04-e2e/SKILL.md` 落地,`e2e_overlay: true` 置于 `metadata:` 之下(loader 丢弃未知顶层键)
- 新增 `AuditDecision(kind='skill')` 审计集成
- 用 `uuid.uuid4().hex` 替代 upstream 的 UUID 字符串格式

**Last reviewed upstream**: 2026-08-02

---

## W2-6 集成 / 验证

**W2-6.1** `tests/test_w2_integration.py` — 七步 mock E2E (Slack mention → audit 落盘)
**W2-6.2** `scripts/w2_audit_headers.py` + `scripts/w2_lineage_check.py` — SPDX header + import residual 校验
**W2-6.3** 本 manifest + `NOTICE` C 段更新 — 每个模块的"借什么 / 不借什么 / 改动点 / Last reviewed upstream"

**Last reviewed upstream**: 2026-08-02
