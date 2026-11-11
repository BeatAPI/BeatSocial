"""[INPUT] Command-line arguments; data dir from --data, $BEATSOCIAL_DATA or ~/.beatsocial.json.
[OUTPUT] The compatible `beatsocial` command and additive `mcp` stdio entry for Claude/Codex.
[POS] Thin wrapper over core.Store. Human-readable output by default, --json for bots.
[PROTOCOL] Exit code 0 on success, 1 on a BeatSocialError (message printed to stderr).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import core, db
from .model import OWNER

CONFIG = os.path.expanduser("~/.beatsocial.json")

MEMORY_README = """# memory：给人和 AI 读的记忆

写稿 AI 每次开新会话只读这里的文件，不读聊天历史。

| 文件 | 内容 | 谁写 | 怎么改 |
|---|---|---|---|
| LESSONS.md | 当前生效的写法规则 | 系统自动生成 | 在审核台「规则」页采纳/停用，不要手改 |
| playbooks/<账号>.md | 账号打法卡：受众、口吻、栏目、格式、对标、禁用说法 | 主人 / Claude | 直接改 |
| 复盘/<周>.md、复盘/本周复盘.md | 每周数据复盘和结论 | beatsocial retro + 复盘 AI | 复盘 AI 补结论 |
| quota/<周>.json | 下周每个账号每种类型几条 | beatsocial retro 建议 | 主人确认后可手改 |
| 竞对拆解/ | 对标长拆解 | Claude | 直接写 |
| lint.json | 额外的竞对名单 {"competitors": [...]} | 主人 | 直接改 |
"""


def data_dir(arg=None) -> str:
    if arg:
        return arg
    if os.environ.get("BEATSOCIAL_DATA"):
        return os.environ["BEATSOCIAL_DATA"]
    if os.path.exists(CONFIG):
        d = json.load(open(CONFIG, encoding="utf-8")).get("data_dir")
        if d:
            return d
    raise core.BeatSocialError("不知道数据目录在哪：先运行 beatsocial init <目录>，或设置 BEATSOCIAL_DATA")


def out(obj, as_json):
    if as_json or not isinstance(obj, (str, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    elif isinstance(obj, list):
        for line in obj:
            print(line)
    else:
        print(obj)


def table(rows, cols):
    if not rows:
        return ["（空）"]
    widths = {c: max(len(str(c)), *(len(str(r.get(c, "") if r.get(c) is not None else "—")) for r in rows)) for c in cols}
    lines = ["  ".join(str(c).ljust(widths[c]) for c in cols)]
    for r in rows:
        lines.append("  ".join(str(r.get(c) if r.get(c) is not None else "—").ljust(widths[c]) for c in cols))
    return lines


def build_parser():
    p = argparse.ArgumentParser(prog="beatsocial", description="BeatSocial：本地社媒审核台与运营闭环")
    p.add_argument("--data", help="数据目录（默认读 ~/.beatsocial.json）")
    p.add_argument("--json", action="store_true", help="输出 JSON（给 bot 和脚本用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="新建数据目录")
    s.add_argument("dir")
    s.add_argument("--git", action="store_true", help="同时 git init 并写 .gitignore")
    s.add_argument("--default", action="store_true", help="设为默认数据目录（已有默认目录时必须显式加这个）")

    s = sub.add_parser("mcp", help="通过 stdio 提供本地 MCP 工具（复用正式数据，不启动网页）")
    s.add_argument("--read-only", action="store_true", help="仅暴露查询工具")
    s.add_argument("--actor", help="审计里的执行者；默认 MCP:<客户端名>，不是主人身份认证")

    s = sub.add_parser("serve", help="打开审核台网页")
    s.add_argument("--port", type=int, default=9361)
    s.add_argument("--host", default="127.0.0.1")

    s = sub.add_parser("overview", help="今天该看什么：待审、过档、库存、缺数据")

    acc = sub.add_parser("account", help="渠道账号").add_subparsers(dest="sub", required=True)
    acc.add_parser("list")
    a = acc.add_parser("set", help="新增或修改账号：beatsocial account set X-EN name='X 英文' per_week=28")
    a.add_argument("id")
    a.add_argument("fields", nargs="*", help="key=value，可用 name platform country lang handle tz bot "
                                               "publish_method per_week slot_times types rules status notes")

    s = sub.add_parser("plan", help="按账号节奏和配额生成某周的帖子位")
    s.add_argument("week")
    s.add_argument("--account", action="append")

    s = sub.add_parser("brief", help="生成批量写稿包（每个账号一份）")
    s.add_argument("week")
    s.add_argument("--account")

    d = sub.add_parser("draft", help="交稿").add_subparsers(dest="sub", required=True)
    s = d.add_parser("import")
    s.add_argument("file")
    s.add_argument("--author", default="Claude")

    po = sub.add_parser("post", help="帖子").add_subparsers(dest="sub", required=True)
    s = po.add_parser("list")
    for k in ("week", "account", "status", "country", "platform", "since", "until"):
        s.add_argument("--" + k)
    s = po.add_parser("show")
    s.add_argument("id")
    s.add_argument("--text", action="store_true", help="给人读的格式，而不是 JSON")
    s = po.add_parser("new", help="手动加一个帖子位")
    s.add_argument("account")
    s.add_argument("slot_at", help="'2026-11-20 21:30'（北京时间）")
    s.add_argument("--type")
    s.add_argument("--topic")
    s = po.add_parser("approve", help="批准（只代表主人：交互终端确认，或 --owner-said 写明主人原话）")
    s.add_argument("ids", nargs="+")
    s.add_argument("--price-checked", action="store_true", help="主人已确认当天官方价格")
    s.add_argument("--hash", action="append", default=[], help="ID=内容哈希：保证批的是主人看过的那一版（post show 里的 content_hash）")
    s.add_argument("--owner-said", help="主人在对话里的原话（Claude Code 代为操作时必填，会记进操作记录；bot 禁止使用）")
    s.add_argument("--by", default=OWNER)
    s = po.add_parser("edit", help="改稿（生成新版本，之前的批准失效）")
    s.add_argument("id")
    s.add_argument("--text", help="新正文；不写就保留原正文")
    s.add_argument("--text-file", help="从文件读新正文")
    s.add_argument("--reply", help="新的首评 / 回复；不写就保留")
    s.add_argument("--zh", help="新的中文参考；不写就保留")
    s.add_argument("--note", help="写稿备注")
    s.add_argument("--topic", help="改主题（看板上卡片的标题）")
    s.add_argument("--by", default="Claude")
    s = po.add_parser("reject")
    s.add_argument("id")
    s.add_argument("--note", default="")
    s.add_argument("--by", default=OWNER)
    s = po.add_parser("note")
    s.add_argument("id")
    s.add_argument("text")
    s.add_argument("--by", default="unknown")
    s = po.add_parser("cancel")
    s.add_argument("id")
    s.add_argument("--note", default="")
    s = po.add_parser("move", help="改发布时间")
    s.add_argument("id")
    s.add_argument("slot_at")

    s = sub.add_parser("make-media", help="用 Codex + beatapi-social-visual 为帖子出 1 张图并挂到媒体槽")
    s.add_argument("id")
    s.add_argument("--slot", type=int, default=1)
    s.add_argument("--style", choices=["auto", "silver", "paper"], default="auto", help="silver=浅色银灰 3D，paper=暖色纸雕")
    s.add_argument("--size", default="1080x1080")
    s.add_argument("--brief", help="出图要求；不写就用媒体槽里的 spec")

    s = sub.add_parser("media", help="设置媒体槽：beatsocial media W47-X-EN-01 --slot 1 --file a.png")
    s.add_argument("id")
    s.add_argument("--slot", type=int, default=1)
    s.add_argument("--file")
    s.add_argument("--kind", choices=["image", "video"])
    s.add_argument("--spec")
    s.add_argument("--remove", action="store_true")
    s.add_argument("--by", default="unknown")

    rv = sub.add_parser("review", help="AI 审稿").add_subparsers(dest="sub", required=True)
    s = rv.add_parser("pack")
    s.add_argument("week", nargs="?")
    s.add_argument("--account")
    s.add_argument("--since")
    s.add_argument("--until")
    s = rv.add_parser("import")
    s.add_argument("file")
    s.add_argument("--reviewer")

    s = sub.add_parser("queue", help="（bot）领取范围内可发的已批准帖子")
    s.add_argument("--bot")
    s.add_argument("--account")
    s.add_argument("--hours", type=int, default=48)
    s.add_argument("--out", help="同时写到文件（只用于预览和准备素材，不能作为离线发布依据）")

    s = sub.add_parser("claim", help="（bot）开始发某条前先领取")
    s.add_argument("id")
    s.add_argument("--bot", required=True)
    s.add_argument("--hash", required=True, help="queue 里这条的 hash：保证你发的就是被批准的那一版")

    rc = sub.add_parser("receipt", help="（bot）发布回执").add_subparsers(dest="sub", required=True)
    s = rc.add_parser("add")
    s.add_argument("id")
    s.add_argument("--url", required=True)
    s.add_argument("--at", help="发布时间，默认现在")
    s.add_argument("--by", default="unknown")
    s.add_argument("--note", default="")
    s.add_argument("--force", action="store_true", help="人工更正已有链接或代写回执")
    s = rc.add_parser("import", help="旧格式回执 CSV")
    s.add_argument("file")
    s.add_argument("--by", default="unknown")
    s = rc.add_parser("off-plan", help="记录没走审核台就发出的帖子")
    s.add_argument("account")
    s.add_argument("--url", required=True)
    s.add_argument("--at", required=True)
    s.add_argument("--text", default="")
    s.add_argument("--by", default="unknown")

    me = sub.add_parser("metric", help="（bot）指标").add_subparsers(dest="sub", required=True)
    s = me.add_parser("add")
    s.add_argument("id")
    s.add_argument("--period", required=True, choices=["h24", "d7", "d30"])
    s.add_argument("--source", default="")
    s.add_argument("--by", default="unknown")
    s.add_argument("values", nargs="+", help="impressions=120 likes=3 ...；拿不到的不写")
    s = me.add_parser("import", help="JSONL：每行 {id, period, impressions, ...}")
    s.add_argument("file")
    s.add_argument("--by", default="unknown")
    me.add_parser("due", help="该采集还没采的帖子")

    s = sub.add_parser("stats", help="数据统计")
    s.add_argument("--by", default="account", choices=["account", "platform", "country", "type", "week", "bot"])
    s.add_argument("--period", choices=["h24", "d7"], help="只用同一时段的数据比较")
    s.add_argument("--week")
    s.add_argument("--since")
    s.add_argument("--until")

    sub.add_parser("inventory", help="各账号已批准的稿还能撑几天")

    s = sub.add_parser("retro", help="生成周复盘数据和下周配额建议")
    s.add_argument("week")

    qu = sub.add_parser("quota", help="配额：查看 / 确认").add_subparsers(dest="sub", required=True)
    s = qu.add_parser("show")
    s.add_argument("week")
    s = qu.add_parser("confirm", help="主人确认配额后才生效（交互终端，或 --owner-said）")
    s.add_argument("week")
    s.add_argument("--set", action="append", default=[], help="改数字：账号:内容类型=条数，例如 X-EN:比价=6")
    s.add_argument("--owner-said", help="主人在对话里的原话（Claude Code 代为操作时必填，会记进操作记录；bot 禁止使用）")

    mk = sub.add_parser("market", help="市场主推方向").add_subparsers(dest="sub", required=True)
    mk.add_parser("list")
    s = mk.add_parser("set", help="beatsocial market set JP focus='…' audience=… cta=… notes=…")
    s.add_argument("code")
    s.add_argument("fields", nargs="*")

    co = sub.add_parser("competitor", help="竞对").add_subparsers(dest="sub", required=True)
    co.add_parser("list")
    s = co.add_parser("add")
    s.add_argument("id")
    s.add_argument("--name", required=True)
    s.add_argument("--why", default="")

    sa = sub.add_parser("sample", help="对标样本").add_subparsers(dest="sub", required=True)
    s = sa.add_parser("list")
    for k in ("platform", "country", "competitor", "type"):
        s.add_argument("--" + k)
    s.add_argument("--limit", type=int, default=30)
    s = sa.add_parser("add")
    s.add_argument("--url", required=True)
    s.add_argument("fields", nargs="*", help="competitor=fal platform=x country=GLOBAL type=… hook=… structure=… impressions=…")
    s.add_argument("--by", default="unknown")
    s = sa.add_parser("import")
    s.add_argument("file")
    s.add_argument("--by", default="unknown")

    ru = sub.add_parser("rule", help="写法规则").add_subparsers(dest="sub", required=True)
    s = ru.add_parser("list")
    s.add_argument("--status")
    s = ru.add_parser("propose")
    s.add_argument("text")
    s.add_argument("--scope", default="global")
    s.add_argument("--evidence", default="")
    s.add_argument("--by", default="unknown")
    for name in ("accept", "reject", "retire"):
        s = ru.add_parser(name)
        s.add_argument("id", type=int)
        s.add_argument("--owner-said", help="主人在对话里的原话（Claude Code 代为操作时必填，会记进操作记录；bot 禁止使用）")
        s.add_argument("--by", default=OWNER)

    sub.add_parser("backup", help="导出数据库文本并提交到数据目录的 Git")

    s = sub.add_parser("import-legacy", help="从旧审核台 plan.json 迁移")
    s.add_argument("plan_json")
    s.add_argument("--benchmarks", help="对标总库 CSV")
    return p


def owner_gate(args, what):
    """Owner decisions (approve, accept rules, confirm quota) need the owner: either typed yes in a terminal,
    or Claude Code relaying the owner's own words from the chat (--owner-said). Bots must never pass this."""
    said = (getattr(args, "owner_said", None) or "").strip()
    if said:
        return said
    if sys.stdin.isatty():
        if input(f"确认{what}？输入 yes：").strip().lower() == "yes":
            return "terminal: yes"
        raise core.BeatSocialError("已取消")
    raise core.BeatSocialError(f"{what}是主人的决定：在对话里由主人说了才能做，加 --owner-said \"主人原话\"")


def post_text(p):
    from .model import COUNTRIES, PLATFORMS

    lines = [f"{COUNTRIES.get(p['country'], p['country'])} · {PLATFORMS.get(p['platform'], p['platform'])} | {p['account_name']} {p['handle'] or ''} | {p['slot_at']} | {p['status_label']} | 第 {p['current_v']} 版",
             f"主题：{p['topic'] or ''}  类型：{p['type'] or ''}  编号：{p['id']}  content_hash：{p['content_hash']}"]
    for f in p["flags"]:
        lines.append(f"[{ {'block': '阻断', 'price': '需核价', 'warn': '提醒'}[f['level']] }] {f['msg']}")
    lines += ["", "【正文】", p["text"] or "（空）"]
    if p["reply_thread"]:
        lines += ["", "【首评 / 回复】", p["reply_thread"]]
    if p["zh_ref"]:
        lines += ["", "【中文参考】", p["zh_ref"]]
    for m in p["media"]:
        lines.append(f"【{'视频' if m['kind'] == 'video' else '图片'} {m['slot']}】{m['file'] or '还没做'}  {m['spec'] or ''}")
    if p.get("last_feedback"):
        lines.append(f"【最近意见】{p['last_feedback']['actor']}：{p['last_feedback']['detail']}")
    return lines


def kv(fields):
    data = {}
    for f in fields:
        if "=" not in f:
            raise core.BeatSocialError(f"参数要写成 key=value：{f}")
        k, v = f.split("=", 1)
        data[k.strip()] = v
    return data


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except core.BeatSocialError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.IntegrityError as e:
        print(f"错误：和已有数据冲突（可能另一个 bot 同时在写），请重试：{e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.OperationalError as e:
        print(f"错误：数据库忙或不可用，请稍后重试：{e}", file=sys.stderr)
        sys.exit(1)


def run(args):
    j = args.json
    if args.cmd == "init":
        path = os.path.abspath(os.path.expanduser(args.dir))
        db.create(path).close()
        if args.git and not os.path.isdir(os.path.join(path, ".git")):
            import subprocess

            subprocess.run(["git", "init", "-q", path], check=True)
            with open(os.path.join(path, ".gitignore"), "w") as fh:
                fh.write("beatsocial.db\nbeatsocial.db-*\nmedia/\nwork/\n.DS_Store\n")
        current = None
        if os.path.exists(CONFIG):
            current = json.load(open(CONFIG, encoding="utf-8")).get("data_dir")
        if not current or args.default or current == path:
            with open(CONFIG, "w", encoding="utf-8") as fh:
                json.dump({"data_dir": path}, fh)
            note = f"已设为默认数据目录（{CONFIG}）"
        else:
            note = f"默认数据目录仍是 {current}；要切换加 --default，或用 --data 指定"
        store = core.Store(path)
        store.render_lessons()
        strategy = os.path.join(path, "memory", "内容策略.md")
        template = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "内容策略.template.md")
        if not os.path.exists(strategy) and os.path.exists(template):
            with open(template, encoding="utf-8") as src, open(strategy, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        readme = os.path.join(path, "memory", "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as fh:
                fh.write(MEMORY_README)
        out(f"数据目录已就绪：{path}。{note}", j)
        return
    if args.cmd == "mcp":
        from . import mcp_server

        mcp_server.serve(data_dir(args.data), args.actor, args.read_only)
        return
    if args.cmd == "serve":
        from . import server

        server.serve(data_dir(args.data), args.host, args.port)
        return
    if args.cmd == "import-legacy":
        from . import legacy

        out(legacy.run(core.Store(data_dir(args.data)), args.plan_json, args.benchmarks), True)
        return

    st = core.Store(data_dir(args.data))
    c, s = args.cmd, getattr(args, "sub", None)
    if c == "overview":
        o = st.overview()
        if j:
            return out(o, True)
        lines = [f"现在 {o['now']}", "状态：" + "  ".join(f"{core.STATUSES[k]} {v}" for k, v in o["counts"].items()),
                 f"过档未发 {len(o['overdue'])} · 待采数据 {o['metrics_due']} · 待确认规则 {o['rules_proposed']}", "", "库存："]
        lines += table(o["inventory"], ["account", "per_week", "approved_ready", "in_review", "todo", "days_covered", "level"])
        return out(lines, False)
    if c == "account":
        if s == "list":
            rows = st.accounts()
            return out(rows if j else table(rows, ["id", "name", "platform", "country", "lang", "handle", "bot", "per_week", "status"]), j)
        return out(st.upsert_account({"id": args.id, **kv(args.fields)}), True)
    if c == "plan":
        ids = st.plan_week(args.week, args.account)
        return out({"created": len(ids), "ids": ids} if j else f"已生成 {len(ids)} 个帖子位", j)
    if c == "brief":
        files = st.brief(args.week, args.account)
        return out(files if j else ["写稿包："] + files, j)
    if c == "draft":
        return out(st.import_drafts(args.file, args.author), True)
    if c == "post":
        if s == "list":
            rows = st.posts(args.week, args.account, args.status, args.country, args.platform, args.since, args.until)
            if j:
                return out(rows, True)
            for r in rows:
                r["flag"] = ",".join(f["code"] for f in r["flags"])
            return out(table(rows, ["id", "slot_at", "account_id", "type", "status_label", "impressions", "flag"]), False)
        if s == "show":
            p = st.post(args.id)
            return out(post_text(p) if args.text else p, False if args.text else True)
        if s == "edit":
            cur = st.post(args.id)
            text = open(args.text_file, encoding="utf-8").read() if args.text_file else args.text
            v = st.submit_version(args.id, text if text is not None else cur["text"],
                                  args.reply if args.reply is not None else cur["reply_thread"],
                                  args.zh if args.zh is not None else cur["zh_ref"],
                                  args.note if args.note is not None else cur["writer_note"], args.by, args.topic)
            return out({"id": args.id, "version": v, "flags": [f["msg"] for f in st.post(args.id)["flags"]]}, True)
        if s == "new":
            return out(st.create_post(args.account, args.slot_at, args.type, args.topic, actor=OWNER), j)
        if s == "approve":
            said = owner_gate(args, f"以 {args.by} 的身份批准 {len(args.ids)} 条")
            hashes = dict(h.split("=", 1) for h in args.hash if "=" in h)
            return out(st.approve_many(args.ids, args.by, args.price_checked, hashes, said), True)
        if s == "reject":
            st.reject(args.id, args.by, args.note)
            return out("已打回", j)
        if s == "note":
            st.add_note(args.id, args.by, args.text)
            return out("已记录", j)
        if s == "cancel":
            st.cancel(args.id, OWNER, args.note)
            return out("已取消", j)
        if s == "move":
            st.reschedule(args.id, args.slot_at)
            return out("已改时间", j)
    if c == "make-media":
        return out(st.make_media(args.id, args.slot, args.style, args.size, args.brief), True)
    if c == "media":
        st.set_media(args.id, args.slot, args.file, args.kind, args.spec, args.by, args.remove)
        return out("媒体槽已更新（如之前已批准，需要重新批准）", j)
    if c == "review":
        if s == "pack":
            return out(st.review_pack(args.week, args.account, "review", args.since, args.until), True)
        return out(st.import_ai_review(args.file, args.reviewer), True)
    if c == "queue":
        q = st.queue(args.bot, args.hours, args.account)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(q, fh, ensure_ascii=False, indent=1)
        return out(q, True)
    if c == "claim":
        return out(st.claim(args.id, args.bot, args.hash), True)
    if c == "receipt":
        if s == "add":
            st.receipt(args.id, args.url, args.at, args.by, args.note, args.force)
            return out({"published": args.id}, True)
        if s == "import":
            return out(core.receipts_from_csv(st, args.file, args.by), True)
        return out({"id": st.off_plan(args.account, args.url, args.at, args.text, args.by)}, True)
    if c == "metric":
        if s == "add":
            st.record_metric(args.id, args.period, kv(args.values), args.source, actor=args.by)
            return out({"recorded": args.id, "period": args.period}, True)
        if s == "import":
            done, errors = [], []
            for line in open(args.file, encoding="utf-8"):
                if not line.strip():
                    continue
                row = json.loads(line)
                pid, period = row.pop("id", None), row.pop("period", None)
                src, at = row.pop("source", ""), row.pop("collected_at", None)
                try:
                    st.record_metric(pid, period, row, src, at, args.by)
                    done.append(pid)
                except core.BeatSocialError as e:
                    errors.append({"id": pid, "reason": str(e)})
            return out({"done": done, "errors": errors}, True)
        return out(st.metrics_due(), True)
    if c == "stats":
        rows = st.stats(args.by, args.week, args.since, args.until, args.period)
        return out(rows if j else table(rows, ["key", "planned", "published", "with_metrics", "median_impressions",
                                               "total_impressions", "median_likes", "engagement_rate", "missing_metrics"]), j)
    if c == "inventory":
        rows = st.inventory()
        return out(rows if j else table(rows, ["account", "per_week", "approved_ready", "in_review", "todo", "days_covered", "level"]), j)
    if c == "retro":
        return out(st.retro(args.week), True)
    if c == "quota":
        if s == "show":
            return out({"file": st.quota_file(args.week), "in_force": st._quota(args.week)}, True)
        said = owner_gate(args, f"确认 {args.week} 的配额")
        accounts = None
        if args.set:
            accounts = dict((st.quota_file(args.week) or {}).get("accounts") or {})
            for item in args.set:
                m = __import__("re").fullmatch(r"([^:]+):(.+)=(\d+)", item)
                if not m:
                    raise core.BeatSocialError(f"--set 要写成 账号:内容类型=条数，收到：{item}")
                accounts.setdefault(m.group(1), {})[m.group(2)] = int(m.group(3))
        return out(st.confirm_quota(args.week, accounts, OWNER, said), True)
    if c == "market":
        if s == "list":
            rows = st.markets()
            if j:
                return out(rows, True)
            return out([f"{m['name']}（{m['code']}）主推：{m.get('focus') or ''}｜对谁：{m.get('audience') or ''}｜引导：{m.get('cta') or ''}｜渠道："
                        + "、".join(f"{c['platform']}({c['review']}待审)" for c in m["channels"]) for m in rows], False)
        cur = [m for m in st.markets() if m["code"] == args.code]
        data = {k: (cur[0].get(k) if cur else "") for k in ("focus", "audience", "cta", "notes")}
        data.update(kv(args.fields))
        return out(st.upsert_market(args.code, data), True)
    if c == "competitor":
        if s == "list":
            rows = st.competitors()
            return out(rows if j else table(rows, ["id", "name", "samples", "used", "why"]), j)
        return out(st.upsert_competitor(args.id, args.name, args.why), j)
    if c == "sample":
        if s == "list":
            rows = st.samples(args.platform, args.country, args.competitor, args.type, args.limit)
            return out(rows if j else table(rows, ["id", "competitor_name", "platform", "country", "type", "impressions", "used", "url"]), j)
        if s == "add":
            return out(st.add_sample({"url": args.url, **kv(args.fields)}, args.by), j)
        return out({"imported": st.import_samples(args.file, args.by)}, True)
    if c == "rule":
        if s == "list":
            rows = st.rules(args.status)
            return out(rows if j else table(rows, ["id", "status", "scope", "text", "evidence"]), j)
        if s == "propose":
            return out({"id": st.propose_rule(args.text, args.scope, args.evidence, args.by)}, True)
        said = owner_gate(args, {"accept": "采纳规则", "reject": "不采纳规则", "retire": "停用规则"}[s])
        st.decide_rule(args.id, {"accept": "active", "reject": "rejected", "retire": "retired"}[s], args.by, None, said)
        return out("已更新，LESSONS.md 已重新生成", j)
    if c == "backup":
        return out(st.backup(), True)
    raise core.BeatSocialError(f"未知命令：{c}")


if __name__ == "__main__":
    main()
