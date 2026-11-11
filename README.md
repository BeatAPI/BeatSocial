# BeatSocial

**A local review desk and feedback loop for multi-market social media run by AI agents.** Agents draft in batches, a person approves in chat, publishing bots claim approved posts and report back metrics, and every week the data turns into writing rules and quotas. Python standard library + SQLite, MIT licensed.

一个本地运行的社媒审核台和运营闭环：AI 批量写稿，人批稿，发布 bot 领取发布并回传数据，系统每周把数据变成下周的写法规则和配额。

- 每条帖子明确属于一个**市场 × 渠道**账号，内容 = 文本 + 媒体槽（图片 / 视频）
- 批准绑定内容版本：改一个字、换一张图都要重新批，bot 只能拿到你批过的版本
- 阻断项（占位符、竞品价、缺素材）批不了；含价格必须确认核价
- 对标样本库，生成帖子位时自动配对标；写稿包提示同一天其他渠道已写的主题，避免撞车
- 内容策略模板（prompts/内容策略.template.md，init 时复制到数据目录 memory/）：推什么、配比、各渠道写法
- 一条帖子可放多张图或视频（按平台限制张数），默认一张
- `beatsocial make-media`：调用 Codex CLI 和你的出图技能为帖子出图（`BEATSOCIAL_IMAGE_SKILL`、`BEATSOCIAL_LOGO` 可配置），品牌标识用原图叠加
- 24h / 7d 数据回收、按账号 / 市场 / 平台 / 类型统计、周复盘、配额建议
- 规则先提案、人采纳后才进入写稿记忆（memory/LESSONS.md）
- 只依赖 Python 3.9+ 标准库和 SQLite

## 快速开始

```bash
git clone <this repo> beat-social && cd beat-social
./bin/beatsocial init ~/beat-social-data --git     # 建数据目录
./bin/beatsocial serve                              # 打开 http://127.0.0.1:9361
```

在对话中通过 MCP 或 CLI 添加渠道账号后：

```bash
./bin/beatsocial plan 2026-W47      # 生成帖子位
./bin/beatsocial brief 2026-W47     # 生成写稿包，交给 Claude Code 写
```

## 通过 MCP 对话操作

在已有数据目录上启动本地 stdio MCP：

```bash
./bin/beatsocial --data /absolute/path/to/beat-social-data mcp
```

Claude Code、Codex 可调用 26 个工具，查询或编辑帖子、管理账号和市场、设置素材、批准具体版本、生成写稿包和复盘、确认规则与配额。网页保持原样，发布 bot 继续用原 CLI。

- 查询 → 展示正文、中文参考和实际媒体 → 按 `revision` 修改或批准。
- 批准、规则决定和配额确认必须有主人的 `owner_said`；它是审计引文，不是身份认证。
- 加 `--read-only` 只提供查询工具；不添加任何发布权限。
- MCP 与 CLI 指向同一数据目录；连接不需要再次 `init`。

完整接入配置和操作约束见 [MCP 接入与对话流程](docs/MCP接入与对话流程.md)。

## 文档

- [用户手册](docs/用户手册.md)：每天、每周怎么用
- [架构](docs/架构.md)：数据模型、流程、约束
- [AGENTS.md](AGENTS.md)：给 AI 和 bot 的规则
- [prompts/](prompts/)：写稿、审稿、复盘、竞对、bot 发布的标准提示词

## 配置

- `~/.beatsocial.json`：`data_dir`（默认数据目录）、`owner`（批准人的名字，默认「主人」）
- `BEATSOCIAL_DATA`、`BEATSOCIAL_OWNER`、`BEATSOCIAL_CODEX`、`BEATSOCIAL_IMAGE_SKILL`、`BEATSOCIAL_LOGO`：同名环境变量覆盖

## 测试

```bash
python3 -m unittest discover tests
```

## 许可

MIT，见 [LICENSE](LICENSE)。
