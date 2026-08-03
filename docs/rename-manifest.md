# Rename Manifest — OIagent-coworker Fork from OpenWorker

> **目的**:这部分代码从 OpenWorker 派生而来,**所有 `openworker.*` 命名必须替换**为 `oiagent_coworker.*`。本清单是 W1-1.4 改名脚本的 source-of-truth,执行后会被 `scripts/rename_openworker.py` 读入并生成 NOTICE.C 章节。

## 1. 命名空间映射

| 原(openworker) | 新(oiagent_coworker) | 形式 |
|---|---|---|
| `openworker` | `oiagent_coworker` | package name (Python / pyproject) |
| `OpenWorker` | `OIagent Coworker` / `OIagent-Coworker` | brand string(用户可见) |
| `OPENWORKER` | `OIAGENT_COWORKER` | env var prefix |
| `openworker.server` | `oiagent_coworker.server` | FastAPI app / ASGI module |
| `openworker.engine` | `oiagent_coworker.engine` | async task engine |
| `openworker.mcp` | `oiagent_coworker.mcp` | MCP client bridge |
| `openworker.skills` | `oiagent_coworker.skills` | skill loader & registry |
| `openworker.connectors` | `oiagent_coworker.connectors` | Slack / GitHub / Linear / Notion / Calendar |
| `ow-` | `oic-` | log prefix / metric label |
| `OpenWorker, Inc.` (none in upstream MIT) | n/a | upstream is individual contributors, no entity |

**重要**:本表**只列**命名替换,**不列**代码行为变更。行为变更留到对应模块的 PR 描述里写。

## 2. 文件路径映射

### 2.1 顶层目录

| 原路径 | 新路径 | 备注 |
|---|---|---|
| `openworker/` | `oiagent_coworker/` | 主包目录 |
| `openworker/engine/` | `oiagent_coworker/engine/` | 原样保留内部结构 |
| `openworker/mcp/` | `oiagent_coworker/mcp/` | |
| `openworker/skills/` | `oiagent_coworker/skills/` | |
| `openworker/connectors/` | `oiagent_coworker/connectors/` | |
| `openworker/server/` | `oiagent_coworker/server/` | |
| `tests/` | `tests/` | 保留,前缀不变 |
| `scripts/` | `scripts/` | 保留(本仓新增 `scripts/rename_openworker.py` / `scripts/add_openworker_header.py` / `scripts/license_lint.py`) |
| `docs/` | `docs/` | 保留,新增 `docs/rename-manifest.md` / `docs/license-policy.md` |

### 2.2 顶层文件

| 原文件 | 新文件 | 备注 |
|---|---|---|
| `openworker/__init__.py` | `oiagent_coworker/__init__.py` | 内含版本号 + SPDX header |
| `openworker/__main__.py` | `oiagent_coworker/__main__.py` | CLI entry |
| `openworker/pyproject.toml` | `pyproject.toml` | 重命名到仓根 |
| `openworker/README.md` | `README.md` | 重写 + 加 NOTICE 指针 |
| `openworker/LICENSE` | `LICENSE-OPENWORKER` | **保留原文件名作为合规凭证**,**不替换** |
| `openworker/NOTICE` | `NOTICE` | 重写为 5 段式(见 NOTICE 模板) |

### 2.3 不复制的原文件

| 文件 | 原因 |
|---|---|
| `openworker/.github/workflows/ci.yml` | 上游 CI 配置不适配 OIagent;重写 |
| `openworker/Dockerfile` | 上游镜像 push 到 ghcr.io/openworker;本仓改用 OIagent daemon 镜像 |
| `openworker/docker-compose.yml` | 同上 |
| `openworker/THIRD_PARTY_LICENSES.txt` | **必须** 重新生成,由 `scripts/license_lint.py` 输出 `docs/license-report.md` 替代 |
| 大于 1MB 的二进制 / 模型权重 | 一律不复制;CI runtime 拉取 |

## 3. import / 字符串替换规则(rename 脚本执行)

### 3.1 Python 代码

```python
# 替换规则 — 严格按顺序(避免误伤)
REPLACE_RULES = [
    # 1. 完整字符串 "openworker.xxx" → "oiagent_coworker.xxx"
    (r"\bopenworker\.([a-z_][a-z_0-9]*)", r"oiagent_coworker.\1"),
    # 2. import / from-import 的模块前缀
    (r"from openworker\b", "from oiagent_coworker"),
    (r"import openworker\b", "import oiagent_coworker"),
    # 3. 类名(少见,但若代码内有 OpenWorker* 类需替换)
    (r"\bOpenWorker([A-Z][a-zA-Z]*)?", r"OIagentCoworker\1"),
    # 4. 环境变量
    (r"\bOPENWORKER_([A-Z_][A-Z_0-9]*)\b", r"OIAGENT_COWORKER_\1"),
    # 5. 日志 / 监控标签
    (r"ow-([a-z0-9\-]+)", r"oic-\1"),
    # 6. 文档字符串中的人读名
    (r"\bOpenWorker\b", "OIagent Coworker"),
    # 7. URL path(API endpoint)
    (r"/openworker/", "/oiagent-coworker/"),
    # 8. JSON 配置 key
    (r'"openworker"', '"oiagent_coworker"'),
]
```

**注意**:
- **不**替换 `Andrew Ng and OpenWorker contributors` 这种**历史事实描述**(在 NOTICE / LICENSE-OPENWORKER / 模块 header 里)。
- **不**替换注释里的 `derived from openworker` 历史溯源字样(由 `scripts/add_openworker_header.py` 单独写,不带 rename)。
- **不**替换 GitHub URL `https://github.com/andrewyng/openworker`(上游归属必须保留)。

### 3.2 配置 / 文档

| 文件类型 | 替换策略 |
|---|---|
| `pyproject.toml` | `name = "openworker"` → `name = "oiagent-coworker"`;`[project.urls]` 加 `Source = "https://github.com/63894696/oiagent-coworker"` 和 `Upstream = "https://github.com/andrewyng/openworker"` |
| `package.json` | `name: "openworker"` → `name: "oiagent-coworker"`;`repository.url` 改成本仓 |
| `Cargo.toml` | 同上 |
| `*.md` | 用正则重写(同上);但**保留**所有 `andrewyng/openworker` URL / `OpenWorker` 在历史溯源段落里的字样 |
| `*.yml` / `*.yaml` (GitHub Actions) | 整文件重写,不适合 sed 替换 |
| `Dockerfile` | 整文件重写 |

## 4. 行为变更 —— 不是 rename 脚本的事

下面 5 类变更**必须**在 PR 描述里独立列,rename 脚本**不会**自动做:

1. **env var prefix**:除了字符串替换,`os.environ.get("OPENWORKER_X")` 调用的语义不变,但**默认值**要根据 OIagent 配置重写(例如 `OPENWORKER_REDIS_URL` 默认 `redis://localhost:6379/0` → `OIAGENT_COWORKER_REDIS_URL` 默认 `redis://localhost:16379/0`)。
2. **CLI 入口**:`oiagent-coworker` CLI 替代 `openworker` CLI(已写进 pyproject.toml `[project.scripts]`),但**子命令**保持 `start / stop / status / skill list / connector list / mcp list` 不变。
3. **API 路径前缀**:`/openworker/api/v1/*` → `/oiagent-coworker/api/v1/*`;OAuth callback URL 同步更新。
4. **ns 命名空间**:log/metric namespace `openworker` → `oiagent_coworker`(字符串替换),但**指标名**(`task.duration`, `mcp.call.count`)不变 —— 因为指标名是协议面,改了就破监控。
5. **数据库 / migration**:openworker 用 SQLAlchemy / Alembic,迁移文件名 `migrate_xxx_openworker.py` → `migrate_xxx_oiagent_coworker.py`;**表名前缀** `openworker_` → `oiagent_coworker_`(重要:不是改 schema,只改前缀)。

## 5. 必加的 SPDX header (rename 脚本写完 rename 后,batch 插入)

rename 脚本**只**做字符串替换;**SPDX header 由 `scripts/add_openworker_header.py` 单独插入**。

每文件第一行(若已是 #! shebang 之外的位置)插入:

```python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OIagent Project Contributors
# Derived from OpenWorker (https://github.com/andrewyng/openworker)
#   Original file:    <original_module>/<original_path>
#   Upstream commit:  <UPSTREAM_COMMIT>  (pinned at W1-1.1 fork time)
#   Original license: MIT
```

字典 `OPENWORKER_HEADER_FILE_MAP` 在 `scripts/add_openworker_header.py` 顶部,记录每个新文件 → 原文件路径的对照表(rename 脚本也生成)。

## 6. 运行步骤

```bash
# 1. 从 GitHub 拉 openworker master
git clone https://github.com/andrewyng/openworker.git upstream-clone
cd upstream-clone && git rev-parse HEAD  # 写入 UPSTREAM_COMMIT

# 2. 跑 rename 脚本(改动代码层)
python scripts/rename_openworker.py \
    --src upstream-clone/ \
    --dst ../oiagent-coworker/ \
    --manifest docs/rename-manifest.md \
    --include-tests \
    --dry-run      # 先看 diff,确认无问题再去掉

# 3. 跑 SPDX header 插入
python scripts/add_openworker_header.py \
    --root ../oiagent-coworker/ \
    --commit <UPSTREAM_COMMIT> \
    --file-map-file docs/w1-1.4-file-map.json

# 4. 跑 license lint
python scripts/license_lint.py ../oiagent-coworker/

# 5. (可选)跑 unit test
cd ../oiagent-coworker && python -m pytest tests/ -v
```

## 7. 不在 rename 范围

- 上游 CI / Dockerfile / docker-compose(整文件重写)
- 上游 CHANGELOG(upstream 自己的历史,不复用)
- 上游 .github/CODEOWNERS(改 OIagent 团队)
- 上游 benchmarks / 性能数据(只在 commit message 引用)

## 8. 复盘清单(release-time 由 reviewer 走)

- [ ] `grep -rn "openworker" --include="*.py" .` → 0 命中(除 `LICENSE-OPENWORKER` / `NOTICE` / `docs/license-policy.md`)
- [ ] `grep -rn "OpenWorker" --include="*.py" .` → 0 命中(除 NOTiCE / header)
- [ ] `grep -rn "OPENWORKER_" --include="*.py" .` → 0 命中
- [ ] `python scripts/license_lint.py . --ci` → exit 0
- [ ] `python -m pytest tests/` → all pass
- [ ] NOTICE.C 5 模块子节全部有真实条目(非 TODO)
- [ ] `<UPSTREAM_COMMIT>` 在 LICENSE-OPENWORKER / NOTICE / header template 全部填实
