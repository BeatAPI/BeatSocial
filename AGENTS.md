# BeatSocial — Agent Instructions

You are working with BeatSocial, a local social-media review desk. People view the read-only web board and operate through conversation. Claude Code / Codex use the BeatSocial MCP tools or the compatible `beatsocial` command; publishing bots keep using the CLI.

## Rules for any AI or bot
1. Read and write only through BeatSocial MCP tools or `beatsocial` (`bin/beatsocial` in this repo). Never edit `beatsocial.db` or files in the data dir's `backup/`.
2. Never approve posts, accept rules or confirm quotas on your own: those are the owner's decisions. Claude Code / Codex may run them only when the owner said so in the chat, passing the owner's exact words as MCP `owner_said` or CLI `--owner-said`. MCP approvals also require the revision actually shown to the owner; never silently refresh a stale revision and approve unseen content. Bots must never use `--owner-said`. The web board is read-only.
   Chat operations guide: `.claude/skills/beatsocial/SKILL.md`.
3. Never publish a post that `queue` did not give you, and `claim` it before publishing.
4. Missing metrics stay empty. Never write 0 for something you could not read.
5. Facts, prices, results and customers must be ours. Unknowns go in as `[待补：…]`; the desk blocks them.
6. Each job has a standard prompt in `prompts/`: 批量写稿, 审稿, 周复盘, 竞对刷新, bot发布与回收. Follow it.
7. Start each writing session fresh. Memory lives in the data dir's `memory/` (LESSONS.md, playbooks/, 复盘/), not in chat history.

## For code changes in this repo
- Python 3.9, standard library only. Run `python3 -m unittest discover tests` before finishing.
- Every source file has an `[INPUT]/[OUTPUT]/[POS]/[PROTOCOL]` header; keep it accurate.
- Vocabularies (statuses, platforms, countries, metric fields) live only in `beatsocial/model.py`.
- Architecture and invariants: `docs/架构.md`. User workflow: `docs/用户手册.md`.

## MCP development and compatibility
- Start with `bin/beatsocial --data <existing-data-dir> mcp`; stdout is JSON-RPC only.
- `beatsocial/mcp_server.py` owns stdio and protocol negotiation; `beatsocial/mcp_tools.py` owns tool schemas and conversation checks over core.Store.
- `docs/MCP接入与对话流程.md` documents tools, configuration, guarantees and limitations.
- Preserve existing web resources, CLI commands and data format. MCP does not publish, claim or write receipts.
- Use temporary data for mutation tests. No approval or migration of formal operations is implied by development work.
