# oi_enhancements / Prisir AI

> **Prisir(湃睿思) AI** — 本地对话式 AI 助手,免登录,系统级 Agent,本地优先。

这是 `oi_enhancements` 仓库,包含 Prisir AI、oiagent、prisr_findex、
prisr_fcontent、fastlane、aureon 等子项目。

更详细的架构说明请阅读 ARCHITECTURE-2026-07-03.md
与 INVENTORY-2026-07-03.md。仓库根目录还有
按子项目组织的 `docs/` 目录,提供设计与计划文档。


## 子项目概览

| 子项目 | 说明 |
| ------ | ---- |
| `oiagent_web.py` | 主 Web 后端(国画风聊天 UI + LLM 路由 + SQLite 持久化) |
| `oiagent-shell/` | Electron 对话壳(系统托盘常驻,全局热键,自启动) |
| `prisir_findex/` | Rust 本机文件搜索引擎(类 Everything,只索引元数据) |
| `prisir_fcontent/` | 文件内容索引与 OCR(支持翻译、截图识图) |
| `fastlane/` | LLM provider 路由(Anthropic / OpenAI / 兼容 API) |
| `aureon/` | 端侧 Agent 核心模块 |
| `crypto_conduit/` | token / 加密通道 |
| `e2e_share_a2h/`, `e2e_share_rot/` | 配对 / 局域网 / 遥控 |
| `assets/` | 项目图标与 UI 资源(受 TRADEMARKS.md 约束) |
| `installer/` | 安装/卸载脚本(Windows NSIS + Linux bash) |
| `docs/` | 设计与计划文档 |

## 平台

- **Windows**:NSIS 安装包(`installer/PrisirAI-Setup-*.exe`)。
- **Linux**:Debian/Ubuntu bash 安装脚本(`installer/linux-install.sh`),
  X11 + GTK(测试于 Debian 13 + xfwm4)。
- **macOS**:未测试,代码路径已尽量跨平台但需用户自行打包。

## License

**GNU Affero General Public License v3.0 or later** — see [LICENSE](LICENSE).

Attribution is required, and derivative works must remain open source, including
when the software is only offered as a network service (AGPL §13). What is
covered, what keeps its own license, and the commercial-exception path are
described in [LICENSING.md](LICENSING.md).

## Contributing

Contributions are welcome. All commits require DCO sign-off (`git commit -s`).
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Third-party components

Dependencies keep their own licenses. See [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES).

## Security

Report vulnerabilities privately through the repository's Security tab.
Please do not open a public issue for a security problem.

## Trademark and brand use

The project name and logo are trademarks of the copyright holder and are not
granted by the code license. Factual references are permitted.
See [TRADEMARKS.md](TRADEMARKS.md).

## Contact

- Repository: https://github.com/63894696/oiagent-coworker
- Commercial licensing: open an issue titled `commercial licensing`.

## Copyright

Copyright (c) 2026 63894696 and contributors.

Licensed under the GNU Affero General Public License v3.0 or later. Brand
elements are reserved under TRADEMARKS.md.
