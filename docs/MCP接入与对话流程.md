# BeatSocial MCP 接入与对话流程

## 目标和兼容范围

通过对话完成运营管理，网页继续展示现有状态。MCP 与 CLI 使用同一个 `core.Store`、SQLite、素材和运营记忆；不会创建另一份运营台账。

本次为增量接入：`web/`、原业务核心、数据库结构和原 CLI 子命令保持不变，只增加 `mcp` 子命令和两个适配模块。不重启看板，不重新初始化数据，不自动迁移旧稿、批准正式帖子或切换 Grok。

## 启动

```bash
/absolute/path/to/beat-social/bin/beatsocial --data /absolute/path/to/beat-social-data mcp
```

使用已有数据目录。`--data` 要放在 `mcp` 之前；省略时沿用 `BEATSOCIAL_DATA` 或 `~/.beatsocial.json`。建议客户端显式指定绝对路径，避免以后切换默认目录时连错数据。

客户端启动并管理此子进程；终端直接运行后等待 stdin 是正常现象。MCP 不启动网页、不占用 9361 端口、不进行模型 API 调用。Python 3.9+、仅标准库，无新增运行依赖。

- `mcp --read-only`：只列出查询工具，直接调用写工具也会拒绝。
- `mcp --actor "MCP:Claude Code"`：设置执行者留痕，默认使用初始化消息中的客户端名；此值不是身份认证。
- 正常日志只写 stderr，stdout 只输出逐行 JSON-RPC。
- 支持协议协商 `2025-06-18`、`2025-03-26`、`2024-11-05`。支持 initialize、initialized、ping、tools/list、tools/call；未实现 HTTP、资源订阅或异步任务能力，不对客户端宣告这些能力。

## 客户端配置

通用 stdio 配置（替换两个绝对路径）：

```json
{
  "mcpServers": {
    "beatsocial": {
      "command": "/absolute/path/to/beat-social/bin/beatsocial",
      "args": ["--data", "/absolute/path/to/beat-social-data", "mcp"]
    }
  }
}
```

Claude Code 可用自身 CLI 登记用户级服务，从其他项目也能访问：

```bash
claude mcp add --scope user --transport stdio beatsocial -- /absolute/path/to/beat-social/bin/beatsocial --data /absolute/path/to/beat-social-data mcp
claude mcp get beatsocial
```

Codex 可用本机 CLI 登记：

```bash
codex mcp add beatsocial -- /absolute/path/to/beat-social/bin/beatsocial --data /absolute/path/to/beat-social-data mcp
codex mcp get beatsocial
```

已有同名服务时先检查，不能覆盖其他项目。配置更新后在客户端重连或新开会话；当前会话是否动态发现新工具由客户端决定。全局 Skill 安装是另一件事，本服务通过工具描述和 initialize.instructions 提供基本操作规范；项目内详细规范在 `.claude/skills/beatsocial/SKILL.md`。

## 26 个工具

| 工作 | 工具 |
|---|---|
| 查状态 | get_overview |
| 找帖子与读全文 | list_posts、get_post |
| 新建与修改 | create_post、edit_post |
| 打回、取消、排期 | reject_post、cancel_post、reschedule_post |
| 素材 | set_media |
| 主人批准 | approve_posts |
| 账号 | list_accounts、create_account、update_account |
| 市场 | list_markets、update_market |
| 数据与复盘 | get_stats、get_retrospective、generate_retrospective |
| 排周计划与写稿包 | plan_week、create_brief |
| 写法规则 | list_rules、propose_rule、decide_rule |
| 配额 | get_quota、confirm_quota |
| 对标资料 | list_samples |

原 CLI 的交稿导入、独立审稿导入、竞对录入、指标回收、备份、queue/claim/receipt 继续可用；MCP 首版没有把所有 CLI 命令机械映射成工具，也不提供通用 shell 或任意 SQL 工具。

## 对话执行方式

1. 主人说“明早日文 X 那条”：先 list_posts 按 JP、x 和绝对日期定位。时间默认北京时间；until 是不包含上界的时间。多条候选时列出供主人选择。
2. get_post 返回全文、中文参考、媒体本地路径、最近审稿意见和 revision。把正文、首评、实际媒体展示给主人。不能仅看文件名就声称完成素材审阅。完整历史仍可从原 CLI 或看板查看；MCP 返回最近 20 个事件及 5 个版本。
3. edit_post 传 id、expected_revision、changes。只提供需要修改的字段，其余保持；成功后返回完整新帖子及新 revision。所有修改仍由 Store 生成版本和事件。
4. 主人明确说“这三条可以发”后，approve_posts 只接收已展示的三条及其 revision，再带 owner_said 原话。不因重新搜索扩大批准范围。
5. 普通批准要求当前 ai_review=pass。主人明确要求人工覆盖未通过或缺失审稿时，可另传 review_override，必须写清主人决定和理由，并留痕。含价格仍要求 price_checked；占位符、缺素材等原阻断条件继续生效。
6. 批准成功只表示批准，工具返回 published=false。真实发布继续由 bot 完成；只有发布回执才能说已发布。

例：编辑正文。

```json
{
  "id": "W47-X-JP-03",
  "expected_revision": "从 get_post 原样复制的 64 位 revision",
  "changes": {"text": "新的日文正文", "zh_ref": "对应中文参考"}
}
```

例：批准已经展示的帖子。

```json
{
  "posts": [
    {"id": "W47-X-JP-03", "expected_revision": "从主人看过的版本原样复制"}
  ],
  "owner_said": "刚才日文 X 那条可以发"
}
```

例中的 revision 是说明占位文字，实际必须传工具返回的摘要。开发讨论中的“好的”不构成正式帖子审批。

## 并发和发布边界

- MCP revision 覆盖正文、首评、中文参考、版本、主题、媒体字节摘要、账号身份、排期和审稿状态。保留既有 content_hash 算法，避免因接入 MCP 而批量作废历史批准。
- MCP 写帖子时在同一个 SQLite 写事务里读取、比对 revision 并执行修改。旧 revision 会被拒绝；两个会话基于同一版本改稿，只允许一个成功。
- 批量批准全成或全败；任何条目过期或有阻断项，数据库不会留下半批批准。
- MCP 拒绝修改、取消或改期已领取帖子，即便领取时间已过。先核对是否已发、补回执或由现有发布流程处理；不能把“不确定”自动当成“没发”。
- MCP 换素材时生成新版本并清除旧 AI 审稿。通过 MCP 更换目标账号身份时，相关未发批准失效；有已领取任务时拒绝更换目标。
- 这些附加保护属于新增 MCP 接口。为了保持原有基础功能，本轮没有给旧 CLI/HTTP 写入口追加相同限制。客户端不能在 MCP 拒绝后改用旧入口绕过保护。
- owner_said 是引文审计，不证明调用者身份。同机具备 shell 权限的 bot 仍可调用原 CLI；目前不构成权限隔离。不要把本服务配置给发布 bot 作为审批入口。
- 账号打法卡、LESSONS、复盘、配额文件仍沿用原 Store 的文件写入方式。SQLite 与普通文件不是跨介质事务；本轮不宣称有故障恢复或多进程配额文件编辑的完整隔离。

## 错误与续做

协议错误返回 JSON-RPC error；工具参数错误、版本冲突和业务阻断返回 isError=true，连接保持可用。调用者应向主人解释原因，不能把 error 当作成功。一次消息最大 2 MiB。

换会话/模型时先重新查询。原数据、版本和操作事件就是续做依据。创建帖子在网络中断后不要盲目重试：先按账号、时刻与主题查询是否已创建。plan_week 保留原有幂等行为。批量生成、独立审稿与更细的任务恢复沿用 CLI 工作包。

复盘比较用相同 h24 或 d7 窗口；缺数据仍为空。generate_retrospective 产生建议，不会采纳规则、确认配额或发布帖子。旧运营资料中的冲突仍需整理后由主人决定，本次没有自动激活。

## 验证

```bash
python3 -m unittest discover tests
```

新增测试使用临时目录，覆盖：stdio 子进程初始化和调用、只读模式、参数校验、并发修改、审批原话和版本、整批回滚、核价、媒体变更、已领取保护、账号身份变化、规则和配额。原 30 个测试保留。

此外，开发时用隔离环境中的官方 Python MCP SDK 验证了初始化、工具发现、建账号、建帖、改稿和批准；没有调用模型 API 或真实发布。客户端 UI 内的实际会话发现和正式运营发布仍需在相应客户端使用时核验。

参考协议：[stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、[lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)、[tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。Claude 配置参考：[Claude Code MCP](https://code.claude.com/docs/en/mcp)。Codex 登记参数以本机 `codex mcp add --help` 为准。
