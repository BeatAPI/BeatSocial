"""[INPUT] A data directory (SQLite + memory/ + media/).
[OUTPUT] Every BeatSocial operation: accounts, slots, drafts, approval, bot queue, receipts,
         metrics, stats, retros, competitors, rules, backup.
[POS] The only module that changes data. cli.py and server.py are thin wrappers around Store.
[PROTOCOL] Each write records an event; approval binds a content hash, so any edit voids it.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import subprocess

from . import db, lint
from .model import (OWNER, COUNTRIES, MARKET_ORDER, CLAIM_TTL_HOURS, METRIC_FIELDS, METRIC_WINDOWS, OVERDUE_HOURS, STATUSES,
                    media_kind_for)

TIME_FMT = "%Y-%m-%d %H:%M"
RETRO_START = "<!-- beatsocial:data:start -->"
RETRO_END = "<!-- beatsocial:data:end -->"


class BeatSocialError(Exception):
    """A user-facing error: the message is shown as-is."""


def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


BEIJING = dt.timezone(dt.timedelta(hours=8))


def parse_time(value: str) -> dt.datetime:
    """Parse a time and return naive Beijing time. Times with a zone (Z, +09:00, +0900) are converted."""
    raw = (value or "").strip()
    v = raw.replace("T", " ")
    v = re.sub(r"\s*(Asia/Shanghai|CST|北京时间)$", "", v)
    v = re.sub(r"Z$", "+00:00", v)
    v = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", v)
    v = re.sub(r"\s+([+-]\d\d:\d\d)$", r"\1", v)
    parsed = None
    try:
        parsed = dt.datetime.fromisoformat(v)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
            try:
                parsed = dt.datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise BeatSocialError(f"看不懂的时间：{raw}")
    if parsed.tzinfo:
        parsed = parsed.astimezone(BEIJING).replace(tzinfo=None)
    return parsed


def check_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s]+$", url, re.IGNORECASE):
        raise BeatSocialError(f"链接必须以 http:// 或 https:// 开头：{url}")
    return url


def iso_week(day: dt.date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def week_days(week: str) -> list:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not m:
        raise BeatSocialError(f"周的格式应为 2026-W47，收到：{week}")
    monday = dt.datetime.strptime(f"{m.group(1)}-W{m.group(2)}-1", "%G-W%V-%u").date()
    return [monday + dt.timedelta(days=i) for i in range(7)]


def shift_week(week: str, n: int) -> str:
    return iso_week(week_days(week)[0] + dt.timedelta(days=7 * n))


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


class Store:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir))
        self.paths = db.layout(self.data_dir)
        self.conn = db.connect(self.data_dir)
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except Exception:  # meta table missing: very old or empty db
            row = None
        if not row or row[0] != str(db.SCHEMA_VERSION):
            db.migrate(self.conn)

    # ------------------------------------------------------------------ helpers
    def q(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def event(self, post_id, actor, action, detail=None):
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO events(post_id, at, actor, action, detail) VALUES(?,?,?,?,?)",
            (post_id, now(), actor or "unknown", action, detail),
        )

    def tx(self):
        return _Tx(self.conn)

    def media_path(self, f: str) -> str:
        if not f:
            return ""
        return f if os.path.isabs(f) else os.path.join(self.paths["media"], f)

    # ----------------------------------------------------------------- accounts
    def accounts(self, status=None):
        sql = "SELECT * FROM accounts"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        rows = self.q(sql + " ORDER BY country, platform, id", args)
        for r in rows:
            r["slot_times"] = _loads(r["slot_times"], [])
            r["types"] = _loads(r["types"], [])
        return rows

    def account(self, account_id):
        r = self.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not r:
            raise BeatSocialError(f"没有这个账号：{account_id}")
        r["slot_times"] = _loads(r["slot_times"], [])
        r["types"] = _loads(r["types"], [])
        return r

    def upsert_account(self, data: dict, actor=OWNER):
        from .model import ACCOUNT_STATUSES, COUNTRIES, PLATFORMS

        acc_id = (data.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", acc_id):
            raise BeatSocialError("账号 ID 只能用字母、数字、- 和 _，例如 X-EN")
        existing = self.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
        row = dict(existing or {"status": "active", "tz": "Asia/Shanghai", "per_week": 0,
                                "slot_times": "[]", "types": "[]"})
        for key in ("name", "platform", "country", "lang", "handle", "tz", "bot", "publish_method",
                    "rules", "status", "notes"):
            if key in data and data[key] is not None:
                row[key] = str(data[key]).strip()
        if "per_week" in data and data["per_week"] not in (None, ""):
            row["per_week"] = int(data["per_week"])
        for key in ("slot_times", "types"):
            if key in data and data[key] is not None:
                val = data[key]
                if isinstance(val, str):
                    val = [s.strip() for s in re.split(r"[,，\n]", val) if s.strip()]
                row[key] = json.dumps(val, ensure_ascii=False)
        for key, allowed in (("platform", PLATFORMS), ("country", COUNTRIES), ("status", ACCOUNT_STATUSES)):
            if row.get(key) not in allowed:
                raise BeatSocialError(f"{key} 必须是 {', '.join(allowed)} 之一，收到：{row.get(key)}")
        for t in _loads(row.get("slot_times"), []):
            if not re.fullmatch(r"\d\d:\d\d", t):
                raise BeatSocialError(f"发布时间要写成 HH:MM，收到：{t}")
        if not row.get("name") or not row.get("lang"):
            raise BeatSocialError("账号需要 name 和 lang")
        row["id"] = acc_id
        row.setdefault("created_at", now())
        row["updated_at"] = now()
        cols = ["id", "name", "platform", "country", "lang", "handle", "tz", "bot", "publish_method",
                "per_week", "slot_times", "types", "rules", "status", "notes", "created_at", "updated_at"]
        with self.tx():
            self.conn.execute(
                f"INSERT OR REPLACE INTO accounts({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )
            self.event(None, actor, "account", {"id": acc_id, "changed": sorted(k for k in data if k != "id")})
        playbook = os.path.join(self.paths["playbooks"], f"{acc_id}.md")
        if not os.path.exists(playbook):
            with open(playbook, "w", encoding="utf-8") as fh:
                fh.write(f"# {row['name']}（{acc_id}）打法卡\n\n"
                         "## 受众与定位\n\n## 口吻\n\n## 栏目与内容类型\n\n## 格式规则\n\n"
                         "## 对标账号\n\n## 禁用说法\n")
        return self.account(acc_id)

    # ------------------------------------------------------------------ markets
    def markets(self):
        """Each market with its focus, its channels and what is waiting in each channel."""
        rows = {r["code"]: r for r in self.q("SELECT * FROM markets")}
        nowt = dt.datetime.now().strftime(TIME_FMT)
        waiting = {r["account_id"]: r for r in self.q(
            "SELECT account_id, SUM(status IN ('review','rejected')) review, SUM(status='approved') approved, "
            "SUM(status='todo') todo FROM posts WHERE slot_at >= ? GROUP BY account_id", (nowt,))}
        out = []
        for code in MARKET_ORDER:
            m = dict(rows.get(code) or {"code": code})
            m["name"] = COUNTRIES[code]
            chans = []
            for a in self.accounts():
                if a["country"] != code:
                    continue
                w = waiting.get(a["id"], {})
                chans.append({"id": a["id"], "platform": a["platform"], "name": a["name"], "handle": a["handle"],
                              "status": a["status"], "per_week": a["per_week"], "bot": a["bot"],
                              "review": w.get("review") or 0, "approved": w.get("approved") or 0,
                              "todo": w.get("todo") or 0})
            m["channels"] = chans
            m["review"] = sum(c["review"] for c in chans)
            m["per_week"] = sum(c["per_week"] or 0 for c in chans if c["status"] == "active")
            out.append(m)
        return out

    def upsert_market(self, code, data, actor=OWNER):
        if code not in COUNTRIES:
            raise BeatSocialError(f"没有这个市场：{code}")
        with self.tx():
            self.conn.execute(
                "INSERT INTO markets(code, focus, audience, cta, notes, updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET focus=excluded.focus, audience=excluded.audience, cta=excluded.cta, "
                "notes=excluded.notes, updated_at=excluded.updated_at",
                (code, data.get("focus", ""), data.get("audience", ""), data.get("cta", ""), data.get("notes", ""), now()))
            self.event(None, actor, "market", {"code": code})
        return [m for m in self.markets() if m["code"] == code][0]

    # -------------------------------------------------------------------- posts
    def _version(self, post_id, v):
        return self.one("SELECT * FROM versions WHERE post_id=? AND v=?", (post_id, v)) or {
            "text": "", "reply_thread": "", "zh_ref": "", "writer_note": "", "author": None, "v": 0}

    def _media(self, post_id):
        return self.q("SELECT * FROM media WHERE post_id=? ORDER BY slot", (post_id,))

    def content_hash(self, post_id, v=None) -> str:
        post = self.one("SELECT current_v FROM posts WHERE id=?", (post_id,))
        ver = self._version(post_id, post["current_v"] if v is None else v)
        parts = [ver["text"] or "", ver["reply_thread"] or ""]
        for m in self._media(post_id):
            path = self.media_path(m["file"])
            if path and os.path.exists(path):
                s = os.stat(path)
                sig = f"{s.st_size}:{s.st_mtime_ns}:{s.st_ino}"  # any rewrite of the file changes this
            else:
                sig = "missing"
            parts.append(f"{m['slot']}|{m['kind']}|{m['file']}|{sig}")
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]

    def flags(self, post: dict, ver: dict, media: list) -> list:
        return lint.check(ver["text"], ver["reply_thread"], post["platform"], post["lang"],
                          ver["zh_ref"], media, self.data_dir, self.paths["media"])

    def post(self, post_id, full=True) -> dict:
        p = self.one(
            "SELECT p.*, a.name AS account_name, a.platform, a.country, a.lang, a.handle, "
            "a.bot AS account_bot, a.rules AS account_rules FROM posts p JOIN accounts a ON a.id=p.account_id "
            "WHERE p.id=?", (post_id,))
        if not p:
            raise BeatSocialError(f"没有这个帖子：{post_id}")
        return self._decorate(p, full)

    def _decorate(self, p, full):
        ver = self._version(p["id"], p["current_v"])
        media = self._media(p["id"])
        p["bot"] = p["bot"] or p["account_bot"]
        p["text"] = ver["text"]
        p["reply_thread"] = ver["reply_thread"]
        p["zh_ref"] = ver["zh_ref"]
        p["writer_note"] = ver["writer_note"]
        p["media"] = media
        p["flags"] = self.flags(p, ver, media)
        p["status_label"] = STATUSES.get(p["status"], p["status"])
        p["content_hash"] = self.content_hash(p["id"])
        p["approval_valid"] = bool(p["approved_hash"]) and p["approved_hash"] == p["content_hash"]
        mets = {m["period"]: m for m in self.q("SELECT * FROM metrics WHERE post_id=?", (p["id"],))}
        p["metrics"] = mets
        best = mets.get("d7") or mets.get("h24")
        p["impressions"] = best["impressions"] if best else None
        p.pop("legacy", None) if not full else None
        fb = self.one("SELECT actor, action, detail, at FROM events WHERE post_id=? AND action IN "
                      "('reject','ai_review') ORDER BY id DESC LIMIT 1", (p["id"],))
        p["last_feedback"] = fb
        p["benchmark"] = (self.one("SELECT s.*, c.name AS competitor_name FROM samples s LEFT JOIN competitors c "
                                   "ON c.id=s.competitor_id WHERE s.id=?", (p["benchmark_id"],))
                          if p["benchmark_id"] else None)
        if full:
            p["versions"] = self.q("SELECT * FROM versions WHERE post_id=? ORDER BY v DESC", (p["id"],))
            p["events"] = self.q("SELECT * FROM events WHERE post_id=? ORDER BY id DESC", (p["id"],))
        return p

    def posts(self, week=None, account=None, status=None, country=None, platform=None, since=None,
              until=None, ids=None, full=False) -> list:
        sql = ("SELECT p.*, a.name AS account_name, a.platform, a.country, a.lang, a.handle, "
               "a.bot AS account_bot, a.rules AS account_rules FROM posts p JOIN accounts a ON a.id=p.account_id WHERE 1=1")
        args = []
        for col, val in (("p.week", week), ("p.account_id", account), ("a.country", country), ("a.platform", platform)):
            if val:
                vals = val if isinstance(val, (list, tuple)) else [v for v in str(val).split(",") if v]
                sql += f" AND {col} IN ({','.join('?' * len(vals))})"
                args += vals
        if status:
            vals = status if isinstance(status, (list, tuple)) else str(status).split(",")
            sql += f" AND p.status IN ({','.join('?' * len(vals))})"
            args += vals
        if since:
            sql += " AND p.slot_at >= ?"
            args.append(since)
        if until:
            sql += " AND p.slot_at < ?"
            args.append(until)
        if ids:
            sql += f" AND p.id IN ({','.join('?' * len(ids))})"
            args += list(ids)
        rows = self.q(sql + " ORDER BY p.slot_at, p.account_id, p.id", args)
        return [self._decorate(r, full) for r in rows]

    def weeks(self):
        return [r["week"] for r in self.q("SELECT DISTINCT week FROM posts ORDER BY week DESC")]

    def _next_id(self, week, account_id):
        prefix = f"{week.split('-')[1]}-{account_id}-"
        rows = self.q("SELECT id FROM posts WHERE id LIKE ?", (prefix + "%",))
        nums = [int(m.group(1)) for r in rows for m in [re.search(r"-(\d+)$", r["id"])] if m]
        return f"{prefix}{(max(nums) + 1 if nums else 1):02d}"

    def create_post(self, account_id, slot_at, type_=None, topic=None, benchmark_id=None, product=None,
                    source_kind="original", source_url=None, post_id=None, actor="system", status="todo"):
        self.account(account_id)
        when = parse_time(slot_at)
        week = iso_week(when.date())
        with self.tx():  # allocate the id and insert under one write lock
            post_id = post_id or self._next_id(week, account_id)
            if self.one("SELECT id FROM posts WHERE id=?", (post_id,)):
                raise BeatSocialError(f"帖子 ID 已存在：{post_id}")
            self.conn.execute(
                "INSERT INTO posts(id, account_id, week, slot_at, type, topic, product, source_kind, source_url, "
                "benchmark_id, status, current_v, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (post_id, account_id, week, when.strftime(TIME_FMT), type_, topic, product, source_kind,
                 source_url, benchmark_id, status, now(), now()))
            self.event(post_id, actor, "create", {"slot_at": when.strftime(TIME_FMT), "type": type_})
        return post_id

    def _void_approval(self, post_id, actor, why):
        p = self.one("SELECT status, approved_hash FROM posts WHERE id=?", (post_id,))
        if p["status"] == "approved" or p["approved_hash"]:
            self.conn.execute(
                "UPDATE posts SET status=CASE WHEN status='approved' THEN 'review' ELSE status END, approved_v=NULL, "
                "approved_hash=NULL, approved_at=NULL, approved_by=NULL, price_checked_at=NULL, claimed_by=NULL, "
                "claimed_at=NULL WHERE id=?", (post_id,))
            self.event(post_id, actor, "approval_void", why)

    def submit_version(self, post_id, text, reply_thread="", zh_ref="", writer_note="", author="unknown",
                       topic=None, media=None, replace_media=False):
        with self.tx():
            p = self.one("SELECT * FROM posts WHERE id=?", (post_id,))
            if not p:
                raise BeatSocialError(f"没有这个帖子：{post_id}")
            if p["status"] in ("published", "measured"):
                raise BeatSocialError(f"{post_id} 已经发布，不能再改稿")
            if p["status"] == "cancelled":
                raise BeatSocialError(f"{post_id} 已取消")
            v = (p["current_v"] or 0) + 1
            if replace_media:
                self.conn.execute("DELETE FROM media WHERE post_id=?", (post_id,))
            self.conn.execute(
                "INSERT INTO versions(post_id, v, text, reply_thread, zh_ref, writer_note, author, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (post_id, v, text or "", reply_thread or "", zh_ref or "", writer_note or "", author, now()))
            self._void_approval(post_id, author, f"新版本 v{v}")
            self.conn.execute(
                "UPDATE posts SET current_v=?, status='review', ai_review=NULL, topic=COALESCE(?, topic), "
                "updated_at=? WHERE id=?", (v, topic, now(), post_id))
            for m in media or []:
                self._set_media(post_id, m, author)
            self.event(post_id, author, "draft", {"v": v})
        return v

    def _set_media(self, post_id, m, actor):
        slot = int(m.get("slot") or 1)
        f = (m.get("file") or "").strip() or None
        if f:
            root = os.path.realpath(self.paths["media"])
            full = os.path.realpath(f if os.path.isabs(f) else os.path.join(root, f))
            if not full.startswith(root + os.sep):
                raise BeatSocialError(f"素材文件要先放进数据目录的 media/ 下：{f}")
            f = os.path.relpath(full, root)
        kind = m.get("kind") or (media_kind_for(f) if f else "image")
        if kind not in ("image", "video"):
            raise BeatSocialError(f"媒体类型只能是 image 或 video，收到：{kind}")
        old = self.one("SELECT * FROM media WHERE post_id=? AND slot=?", (post_id, slot))
        spec = m.get("spec") if m.get("spec") is not None else (old or {}).get("spec", "")
        if m.get("remove"):
            self.conn.execute("DELETE FROM media WHERE post_id=? AND slot=?", (post_id, slot))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO media(post_id, slot, kind, spec, file, updated_at) VALUES(?,?,?,?,?,?)",
                (post_id, slot, kind, spec, f if f is not None else (old or {}).get("file"), now()))
        self.event(post_id, actor, "media", {"slot": slot, "file": f, "kind": kind})

    def set_media(self, post_id, slot, file=None, kind=None, spec=None, actor="unknown", remove=False):
        p = self.one("SELECT status FROM posts WHERE id=?", (post_id,))
        if not p:
            raise BeatSocialError(f"没有这个帖子：{post_id}")
        if p["status"] in ("published", "measured"):
            raise BeatSocialError(f"{post_id} 已经发布，不能换素材")
        before = self.content_hash(post_id)
        with self.tx():
            self._set_media(post_id, {"slot": slot, "file": file, "kind": kind, "spec": spec, "remove": remove}, actor)
            if self.content_hash(post_id) != before:
                self._void_approval(post_id, actor, f"媒体槽 {slot} 变更")
            self.conn.execute("UPDATE posts SET updated_at=? WHERE id=?", (now(), post_id))

    def make_media(self, post_id, slot=1, style="auto", size="1080x1080", brief=None, actor="Codex", timeout=900):
        """Generate one image for a post's media slot with Codex CLI and the beatapi-social-visual skill,
        save it under media/<week>/, and attach it. Prices on the image must match the post text."""
        p = self.post(post_id)
        if p["status"] in ("published", "measured", "cancelled"):
            raise BeatSocialError(f"{post_id} 已发布或已取消，不再出图")
        codex = os.environ.get("BEATSOCIAL_CODEX") or \
            "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        if not os.path.exists(codex):
            raise BeatSocialError(f"找不到 Codex CLI：{codex}（可用环境变量 BEATSOCIAL_CODEX 指定）")
        out_dir = os.path.join(self.paths["media"], p["week"])
        os.makedirs(out_dir, exist_ok=True)
        name = f"{post_id}-{slot}.png"
        target = os.path.join(out_dir, name)
        old = next((m for m in p["media"] if m["slot"] == slot), None)
        spec = brief or (old or {}).get("spec") or ""
        lang = {"en": "English", "ja": "Japanese", "zh-Hant": "Traditional Chinese", "ko": "Korean",
                "ru": "Russian"}.get(p["lang"], p["lang"])
        style_line = {"silver": "用浅色银灰 3D 风格。", "paper": "用暖色手工纸雕插画风格。"}.get(style, "两套主风格按内容选一套。")
        skill = os.environ.get("BEATSOCIAL_IMAGE_SKILL", "beatapi-social-visual")
        logo = os.environ.get("BEATSOCIAL_LOGO", f"~/.codex/skills/{skill}/assets/beatapi-logo.png")
        prompt = (
            f"使用 {skill} 技能，为下面这条社媒帖子做 1 张配图。只生成图片，不发布、不改其他文件。\n"
            f"平台：{p['platform']}；尺寸：{size}；图中文字语言：{lang}。{style_line}\n"
            f"保存为（必须是这个绝对路径，PNG）：{target}\n"
            f"出图要求：{spec or '按正文提炼一个视觉主体和短标题'}\n"
            "图里出现的型号、价格、规格只能取自下面正文，逐字核对；正文没有的数字不要放进图里。\n"
            "左下角的品牌标识不要让图像模型画：生成时把左下角留空，再用代码把 "
            f"{logo} 原图按比例缩放后叠加到左下角，保存前确认标识和原图一致。\n"
            "--- 正文 ---\n" + (p["text"] or "") + "\n--- 首评 ---\n" + (p["reply_thread"] or ""))
        log = os.path.join(self.paths["work"], f"make-media-{post_id}-{slot}.log")
        os.makedirs(self.paths["work"], exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            r = subprocess.run([codex, "exec", "--skip-git-repo-check", "-C", out_dir, "-s", "workspace-write", prompt],
                               stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(target):
            raise BeatSocialError(f"Codex 没有产出图片（退出码 {r.returncode}），日志：{log}")
        self.set_media(post_id, slot, os.path.relpath(target, self.paths["media"]), "image", spec or None, actor)
        return {"id": post_id, "slot": slot, "file": target, "log": log}

    def approve(self, post_id, actor=OWNER, price_checked=False, expected_hash=None, owner_said=None):
        """Approve the current version. expected_hash is the hash of the version the person looked at;
        if the content changed since (e.g. Claude re-imported), the approval is refused."""
        with self.tx():
            p = self.post(post_id, full=False)
            if p["status"] not in ("review", "rejected"):
                raise BeatSocialError(f"{post_id} 当前是「{p['status_label']}」，只有待审的稿能批准")
            h = p["content_hash"]
            if expected_hash and expected_hash != h:
                raise BeatSocialError(f"{post_id} 在你看过之后内容变了，请刷新后再看一遍")
            blocks = lint.blocking(p["flags"])
            if blocks:
                raise BeatSocialError(f"{post_id} 有阻断项：" + "；".join(f["msg"] for f in blocks))
            if lint.needs_price(p["flags"]) and not price_checked:
                raise BeatSocialError(f"{post_id} 含价格，批准前要勾选「已核对当天官方价格」")
            self.conn.execute(
                "UPDATE posts SET status='approved', approved_v=current_v, approved_hash=?, approved_at=?, "
                "approved_by=?, price_checked_at=?, updated_at=? WHERE id=?",
                (h, now(), actor, now() if price_checked else None, now(), post_id))
            self.event(post_id, actor, "approve", {"v": p["current_v"], "hash": h, "price_checked": bool(price_checked),
                                                  "owner_said": owner_said})
        return h

    def approve_many(self, ids, actor=OWNER, price_checked=False, hashes=None, owner_said=None):
        done, skipped = [], []
        for pid in ids:
            try:
                self.approve(pid, actor, price_checked, (hashes or {}).get(pid), owner_said)
                done.append(pid)
            except BeatSocialError as e:
                skipped.append({"id": pid, "reason": str(e)})
        return {"approved": done, "skipped": skipped}

    def reject(self, post_id, actor=OWNER, note=""):
        p = self.post(post_id, full=False)
        if p["status"] in ("published", "measured", "cancelled"):
            raise BeatSocialError(f"{post_id} 当前是「{p['status_label']}」，不能打回")
        with self.tx():
            self._void_approval(post_id, actor, "打回")
            self.conn.execute("UPDATE posts SET status='rejected', updated_at=? WHERE id=?", (now(), post_id))
            self.event(post_id, actor, "reject", note)

    def add_note(self, post_id, actor, text):
        self.post(post_id, full=False)
        with self.tx():
            self.event(post_id, actor, "note", text)

    def cancel(self, post_id, actor=OWNER, note=""):
        p = self.post(post_id, full=False)
        if p["status"] in ("published", "measured"):
            raise BeatSocialError(f"{post_id} 已发布，不能取消")
        with self.tx():
            self.conn.execute("UPDATE posts SET status='cancelled', claimed_by=NULL, updated_at=? WHERE id=?",
                              (now(), post_id))
            self.event(post_id, actor, "cancel", note)

    def reschedule(self, post_id, slot_at, actor=OWNER):
        p = self.post(post_id, full=False)
        if p["status"] in ("published", "measured"):
            raise BeatSocialError(f"{post_id} 已发布，不能改时间")
        when = parse_time(slot_at).strftime(TIME_FMT)
        with self.tx():
            self.conn.execute("UPDATE posts SET slot_at=?, updated_at=? WHERE id=?", (when, now(), post_id))
            self.event(post_id, actor, "reschedule", {"from": p["slot_at"], "to": when})

    # ---------------------------------------------------------------- publishing
    EARLY_MINUTES = 0  # never publish before the slot

    def _hold_reason(self, p, bot=None):
        """Why this approved post must not be published by `bot` right now (None = OK to publish now).
        Shared by queue() and claim() so they can never disagree."""
        if p["status"] != "approved":
            return f"状态是「{p['status_label']}」，不是已批准"
        if bot and p["bot"] and p["bot"] != bot:
            return f"这条由 {p['bot']} 负责发布，不是 {bot}"
        acc = self.one("SELECT status FROM accounts WHERE id=?", (p["account_id"],))
        if not acc or acc["status"] != "active":
            return "账号不是「运营中」，暂停发布"
        if not p["approval_valid"]:
            return "批准后内容变过，需要重新批准"
        blocks = lint.blocking(p["flags"])
        if blocks:
            return "有阻断项：" + "；".join(f["msg"] for f in blocks)
        if lint.needs_price(p["flags"]) and not p["price_checked_at"]:
            return "含价格但批准时没确认核价"
        overdue = (dt.datetime.now() - parse_time(p["slot_at"])).total_seconds() / 3600
        if overdue > OVERDUE_HOURS:
            return f"已过发布时间 {round(overdue)} 小时：等主人改时间或取消，不要直接补发"
        if overdue < -self.EARLY_MINUTES / 60:
            return f"还没到发布时间（{p['slot_at']}）"
        return None

    def queue(self, bot=None, hours=48, account=None):
        """items: publish now (slot reached, within OVERDUE_HOURS). upcoming: approved and clean but not due yet —
        for preparing media only, never publish before slot_at. held: blocked, with the reason."""
        limit = (dt.datetime.now() + dt.timedelta(hours=hours)).strftime(TIME_FMT)
        posts = self.posts(status="approved", account=account, until=limit)
        out, upcoming, held = [], [], []
        for p in posts:
            if bot and (p["bot"] or "") != bot:
                continue
            reason = self._hold_reason(p)
            item = {
                "id": p["id"], "account": p["account_id"], "platform": p["platform"], "country": p["country"],
                "handle": p["handle"], "bot": p["bot"], "slot_at": p["slot_at"],
                "version": p["current_v"], "hash": p["approved_hash"],
                "text": p["text"], "reply_thread": p["reply_thread"],
                "media": [{"slot": m["slot"], "kind": m["kind"], "path": self.media_path(m["file"])} for m in p["media"]],
                "account_rules": p["account_rules"], "claimed_by": p["claimed_by"], "claimed_at": p["claimed_at"],
            }
            if reason and reason.startswith("还没到发布时间"):
                upcoming.append(item)
            elif reason:
                held.append({"id": p["id"], "slot_at": p["slot_at"], "reason": reason})
            else:
                out.append(item)
        return {"generated_at": now(), "hours": hours, "items": out, "upcoming": upcoming, "held": held}

    def claim(self, post_id, bot, expected_hash=None):
        """Atomic: only the responsible bot, only when due, only one holder. Re-claiming your own post returns a
        warning: you claimed it before, so check the platform for an existing post before publishing again."""
        with self.tx():
            p = self.post(post_id, full=False)
            reason = self._hold_reason(p, bot)
            if reason:
                raise BeatSocialError(f"{post_id} 不能领取：{reason}")
            if expected_hash and expected_hash != p["approved_hash"]:
                raise BeatSocialError(f"{post_id} 的批准版本已变，重新运行 queue 拿最新内容后再领取")
            stale = (dt.datetime.now() - dt.timedelta(hours=CLAIM_TTL_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            cur = self.conn.execute(
                "UPDATE posts SET claimed_by=?, claimed_at=? WHERE id=? AND status='approved' AND approved_hash=? "
                "AND (claimed_by IS NULL OR claimed_by=? OR claimed_at < ?)",
                (bot, now(), post_id, p["approved_hash"], bot, stale))
            if cur.rowcount != 1:
                raise BeatSocialError(f"{post_id} 已被 {p['claimed_by']} 领取（{p['claimed_at']}）")
            prev = p["claimed_by"]
            self.event(post_id, bot, "claim", {"hash": p["approved_hash"], "previous": prev})
        result = {"claimed": post_id, "bot": bot, "hash": p["approved_hash"]}
        if prev:  # any earlier claim, by this bot or a bot whose claim expired: it may already be posted
            who = "你" if prev == bot else prev
            result["warning"] = (f"{who} 在 {p['claimed_at']} 领过这条：先去平台确认是否已经发出，"
                                 "已发出就直接写回执，不要再发一次")
        return result

    def receipt(self, post_id, url, published_at=None, by="unknown", note="", force=False):
        """Atomic: the first URL wins; a different URL later is refused unless force=True (a person correcting it)."""
        url = check_url(url)
        at = parse_time(published_at).strftime("%Y-%m-%d %H:%M") if published_at else now()[:16]
        with self.tx():
            p = self.post(post_id, full=False)
            if p["status"] not in ("approved", "published", "measured"):
                raise BeatSocialError(f"{post_id} 是「{p['status_label']}」，没批准的帖子不应该发出")
            if p["claimed_by"] and p["claimed_by"] != by and not force:
                raise BeatSocialError(f"{post_id} 由 {p['claimed_by']} 领取，回执应由它写；确需代写加 --force")
            cur = self.conn.execute(
                "UPDATE posts SET status=CASE WHEN status='measured' THEN status ELSE 'published' END, "
                "published_at=CASE WHEN published_url=? AND NOT ? THEN published_at ELSE ? END, "
                "published_url=?, published_by=COALESCE(published_by, ?), claimed_by=NULL, updated_at=? "
                "WHERE id=? AND (published_url IS NULL OR published_url='' OR published_url=? OR ?)",
                (url, 1 if force else 0, at, url, by, now(), post_id, url, 1 if force else 0))
            if cur.rowcount != 1:
                raise BeatSocialError(f"{post_id} 已有发布链接 {p['published_url']}，不要重复发；确需更正加 --force")
            self.event(post_id, by, "receipt", {"url": url, "at": at, "note": note, "force": force,
                                               "hash_ok": p["approval_valid"]})

    def off_plan(self, account_id, url, published_at, text="", by="unknown", type_="计划外"):
        """Record a post that was published without going through the desk (e.g. the owner posted by hand)."""
        url = check_url(url)
        when = parse_time(published_at)
        with self.tx():
            if self.one("SELECT id FROM posts WHERE published_url=?", (url,)):
                raise BeatSocialError(f"这个链接已经记录过：{url}")
            pid = f"OFF-{account_id}-{when.strftime('%Y%m%d')}-"
            n = len(self.q("SELECT id FROM posts WHERE id LIKE ?", (pid + "%",))) + 1
            pid = f"{pid}{n:02d}"
            self.create_post(account_id, when.strftime(TIME_FMT), type_, None, post_id=pid, actor=by, status="todo")
            self.conn.execute(
                "INSERT INTO versions(post_id, v, text, author, created_at) VALUES(?,?,?,?,?)",
                (pid, 1, text, by, now()))
            self.conn.execute(
                "UPDATE posts SET current_v=1, status='published', published_url=?, published_at=?, published_by=?, "
                "updated_at=? WHERE id=?", (url, when.strftime(TIME_FMT), by, now(), pid))
            self.event(pid, by, "off_plan", {"url": url})
        return pid

    # ------------------------------------------------------------------ metrics
    def record_metric(self, post_id, period, values: dict, source="", collected_at=None, actor="unknown"):
        if period not in METRIC_WINDOWS:
            raise BeatSocialError(f"指标时段只能是 {METRIC_WINDOWS}，收到：{period}")
        p = self.post(post_id, full=False)
        if p["status"] not in ("published", "measured"):
            raise BeatSocialError(f"{post_id} 还没发布，不能记指标")
        clean, extra = {}, {}
        for k, v in values.items():
            if v in (None, ""):
                continue
            try:
                num = int(float(str(v).replace(",", "")))
            except ValueError:
                raise BeatSocialError(f"{k} 不是数字：{v}")
            (clean if k in METRIC_FIELDS else extra)[k] = num
        if not clean and not extra:
            raise BeatSocialError("没有任何指标值；拿不到的指标留空，不要填 0")
        with self.tx():
            old = self.one("SELECT extra FROM metrics WHERE post_id=? AND period=?", (post_id, period))
            merged = dict(_loads((old or {}).get("extra"), {}))
            merged.update(extra)
            cols = ["post_id", "period", "collected_at", "source"] + METRIC_FIELDS + ["extra"]
            row = [post_id, period, collected_at or now(), source] + [clean.get(k) for k in METRIC_FIELDS] + [
                json.dumps(merged, ensure_ascii=False) if merged else None]
            # Upsert that keeps earlier values for fields this reading does not include.
            keep = ", ".join(f"{k}=COALESCE(excluded.{k}, {k})" for k in METRIC_FIELDS)
            self.conn.execute(
                f"INSERT INTO metrics({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                f"ON CONFLICT(post_id, period) DO UPDATE SET collected_at=excluded.collected_at, "
                f"source=COALESCE(NULLIF(excluded.source, ''), source), extra=excluded.extra, {keep}", row)
            if period == "d7":
                self.conn.execute("UPDATE posts SET status='measured', updated_at=? WHERE id=?", (now(), post_id))
            self.event(post_id, actor, "metric", {"period": period, **clean})

    def metrics_due(self):
        """Published posts whose h24 / d7 window has passed but has no metric yet."""
        out = []
        t = dt.datetime.now()
        for p in self.posts(status=["published", "measured"]):
            if not p["published_at"]:
                continue
            age = (t - parse_time(p["published_at"])).total_seconds() / 3600
            for period, hours in (("h24", 24), ("d7", 168)):
                if age >= hours and period not in p["metrics"]:
                    out.append({"id": p["id"], "account": p["account_id"], "platform": p["platform"],
                                "url": p["published_url"], "period": period, "hours_since": round(age),
                                "bot": p["bot"]})
        return out

    # -------------------------------------------------------------------- stats
    def stats(self, by="account", week=None, since=None, until=None, period=None):
        """period=None uses d7 when present else h24 (column 'basis' says how many of each);
        period='h24' or 'd7' compares posts at the same age only."""
        keys = {"account": "account_id", "platform": "platform", "country": "country", "type": "type",
                "week": "week", "bot": "bot"}
        if by not in keys:
            raise BeatSocialError(f"统计维度只能是 {', '.join(keys)}")
        posts = self.posts(week=week, since=since, until=until)
        groups = {}
        for p in posts:
            if p["status"] == "cancelled":
                continue
            g = groups.setdefault(p[keys[by]] or "（未填）", {
                "key": p[keys[by]] or "（未填）", "planned": 0, "published": 0, "approved_waiting": 0,
                "in_review": 0, "todo": 0, "with_metrics": 0, "impr": [], "likes": [], "replies": []})
            g["planned"] += 1
            if p["status"] in ("published", "measured"):
                g["published"] += 1
                best = (p["metrics"].get(period) if period
                        else p["metrics"].get("d7") or p["metrics"].get("h24"))
                if best:
                    g["with_metrics"] += 1
                    g.setdefault("basis", {}).setdefault(best["period"], 0)
                    g["basis"][best["period"]] += 1
                    g["impr"].append(best["impressions"])
                    g["likes"].append(best["likes"])
                    g["replies"].append(best["replies"])
                    if best["impressions"] and best["likes"] is not None and best["replies"] is not None:
                        # engagement rate only over posts where impressions, likes and replies are all known
                        g["eng_n"] = g.get("eng_n", 0) + best["likes"] + best["replies"]
                        g["eng_d"] = g.get("eng_d", 0) + best["impressions"]
            elif p["status"] == "approved":
                g["approved_waiting"] += 1
            elif p["status"] in ("review", "rejected"):
                g["in_review"] += 1
            elif p["status"] == "todo":
                g["todo"] += 1
        rows = []
        for g in groups.values():
            impr = [v for v in g.pop("impr") if v is not None]
            likes = [v for v in g.pop("likes") if v is not None]
            replies = [v for v in g.pop("replies") if v is not None]
            eng_n, eng_d = g.pop("eng_n", 0), g.pop("eng_d", 0)
            g.update({
                "median_impressions": _median(impr), "total_impressions": sum(impr) if impr else None,
                "max_impressions": max(impr) if impr else None,
                "median_likes": _median(likes), "median_replies": _median(replies),
                "engagement_rate": round(eng_n / eng_d * 100, 2) if eng_d else None,
                "missing_metrics": g["published"] - g["with_metrics"],
                "basis": "、".join(f"{k} {v} 条" for k, v in sorted(g.pop("basis", {}).items())) or None,
            })
            rows.append(g)
        rows.sort(key=lambda r: (-(r["total_impressions"] or 0), r["key"]))
        return rows

    def inventory(self):
        """How many days of approved, publishable posts each active account has left."""
        nowt = dt.datetime.now()
        out = []
        for a in self.accounts(status="active"):
            if not a["per_week"]:
                continue
            upcoming = self.q("SELECT id FROM posts WHERE account_id=? AND status='approved' AND slot_at>=?",
                              (a["id"], nowt.strftime(TIME_FMT)))
            ready = [r for r in upcoming if self.post(r["id"], full=False)["approval_valid"]]
            waiting = self.one("SELECT COUNT(*) n FROM posts WHERE account_id=? AND status IN ('review','rejected') "
                               "AND slot_at>=?", (a["id"], nowt.strftime(TIME_FMT)))["n"]
            todo = self.one("SELECT COUNT(*) n FROM posts WHERE account_id=? AND status='todo' AND slot_at>=?",
                            (a["id"], nowt.strftime(TIME_FMT)))["n"]
            last = self.one("SELECT MAX(slot_at) m FROM posts WHERE account_id=? AND status='approved'", (a["id"],))["m"]
            days = round(len(ready) / (a["per_week"] / 7), 1)
            out.append({"account": a["id"], "name": a["name"], "country": a["country"], "platform": a["platform"],
                        "per_week": a["per_week"], "approved_ready": len(ready), "in_review": waiting, "todo": todo,
                        "days_covered": days, "last_approved_slot": last,
                        "level": "red" if days < 1 else ("yellow" if days < 3 else "ok")})
        return out

    def overview(self):
        t = dt.datetime.now()
        today = t.strftime("%Y-%m-%d")
        counts = {s: 0 for s in STATUSES}
        for r in self.q("SELECT status, COUNT(*) n FROM posts GROUP BY status"):
            counts[r["status"]] = r["n"]
        overdue = self.q("SELECT p.id, p.slot_at, p.account_id, p.topic, p.type FROM posts p WHERE status='approved' "
                         "AND slot_at < ? ORDER BY slot_at DESC",
                         ((t - dt.timedelta(hours=OVERDUE_HOURS)).strftime(TIME_FMT),))
        today_posts = self.q("SELECT id, account_id, slot_at, status, topic, type FROM posts WHERE slot_at LIKE ? "
                             "ORDER BY slot_at",
                             (today + "%",))
        return {
            "now": now(), "counts": counts, "today": today_posts, "overdue": overdue,
            "metrics_due": len(self.metrics_due()),
            "rules_proposed": self.one("SELECT COUNT(*) n FROM rules WHERE status='proposed'")["n"],
            "samples_missing_type": self.one("SELECT COUNT(*) n FROM samples WHERE type IS NULL OR type=''")["n"],
            "samples_missing_adapt": self.one("SELECT COUNT(*) n FROM samples WHERE how_to_adapt IS NULL OR how_to_adapt=''")["n"],
            "slots_without_benchmark": self.one(
                "SELECT COUNT(*) n FROM posts WHERE status IN ('todo','rejected') AND benchmark_id IS NULL "
                "AND slot_at >= ? AND id NOT LIKE 'OFF-%'", (t.strftime(TIME_FMT),))["n"],
            "pending_quotas": [q for q in self._quota_list() if q.get("status") != "confirmed"],
            "this_week": iso_week(dt.date.today()), "next_week": shift_week(iso_week(dt.date.today()), 1),
            "inventory": self.inventory(),
        }

    # ------------------------------------------------------------ batch writing
    def _quota(self, week):
        """The quota in force for `week`: the newest *confirmed* quota file for this week or an earlier one.
        Suggested quotas are ignored until a person confirms them."""
        best = None
        for name in sorted(os.listdir(self.paths["quota"])) if os.path.isdir(self.paths["quota"]) else []:
            m = re.fullmatch(r"(\d{4}-W\d{2})\.json", name)
            if not m or m.group(1) > week:
                continue
            data = _loads(_read(os.path.join(self.paths["quota"], name)), {})
            if data.get("status") == "confirmed":
                best = data
        return (best or {}).get("accounts", {})

    def retro_text(self, week=None):
        files = sorted(f for f in os.listdir(self.paths["retros"]) if re.fullmatch(r"\d{4}-W\d{2}\.md", f)) \
            if os.path.isdir(self.paths["retros"]) else []
        name = f"{week}.md" if week else (files[-1] if files else None)
        if not name or not os.path.exists(os.path.join(self.paths["retros"], name)):
            return {"week": week, "text": None, "weeks": [f[:-3] for f in files]}
        return {"week": name[:-3], "text": _read(os.path.join(self.paths["retros"], name)),
                "weeks": [f[:-3] for f in files]}

    def _quota_list(self):
        out = []
        if os.path.isdir(self.paths["quota"]):
            for name in sorted(os.listdir(self.paths["quota"])):
                if re.fullmatch(r"\d{4}-W\d{2}\.json", name):
                    out.append(_loads(_read(os.path.join(self.paths["quota"], name)), {}))
        return out

    def quota_file(self, week):
        path = os.path.join(self.paths["quota"], f"{week}.json")
        return _loads(_read(path), None) if os.path.exists(path) else None

    def confirm_quota(self, week, accounts=None, by=OWNER, owner_said=None):
        data = self.quota_file(week)
        if data is None and accounts is None:
            raise BeatSocialError(f"没有 {week} 的配额建议，先运行复盘")
        data = data or {"week": week}
        if accounts is not None:
            clean = {}
            for acc, types in accounts.items():
                self.account(acc)
                clean[acc] = {t: int(n) for t, n in types.items() if int(n) > 0}
            data["accounts"] = clean
        data.update(status="confirmed", confirmed_by=by, confirmed_at=now())
        with open(os.path.join(self.paths["quota"], f"{week}.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        with self.tx():
            self.event(None, by, "quota_confirm", {"week": week, "owner_said": owner_said})
        return data

    def pick_benchmark(self, account, type_=None, exclude=()):
        """Best competitor sample for this account: same platform and country first, same type preferred,
        least reused, then most impressions."""
        candidates = self.q(
            "SELECT s.*, (SELECT COUNT(*) FROM posts p WHERE p.benchmark_id=s.id) AS used "
            "FROM samples s WHERE s.platform=?", (account["platform"],))
        if not candidates:
            return None

        def score(s):
            return (
                0 if s["for_account"] == account["id"] else 1,
                0 if s["country"] == account["country"] else 1,
                0 if type_ and s["type"] == type_ else 1,
                s["used"],
                -(s["impressions"] or 0),
            )

        candidates = [c for c in candidates if c["id"] not in exclude]
        return sorted(candidates, key=score)[0]["id"] if candidates else None

    def plan_week(self, week, accounts=None, actor="system"):
        days = week_days(week)
        quota = self._quota(week)
        created = []
        for a in self.accounts(status="active"):
            if accounts and a["id"] not in accounts:
                continue
            per_week = int(sum(quota[a["id"]].values())) if a["id"] in quota else a["per_week"]
            if not per_week:
                continue
            existing = self.q("SELECT slot_at FROM posts WHERE account_id=? AND week=? AND status!='cancelled' "
                              "AND id NOT LIKE 'OFF-%'", (a["id"], week))
            taken = {r["slot_at"] for r in existing}
            room = per_week - len(existing)  # fill the gap; never duplicate an existing slot
            if room <= 0:
                continue
            types = []
            if a["id"] in quota:
                for t, n in quota[a["id"]].items():
                    types += [t] * int(n)
            elif a["types"]:
                types = [a["types"][i % len(a["types"])] for i in range(per_week)]
            times = a["slot_times"] or ["09:00", "12:30", "18:00", "21:30", "08:00", "15:00"]
            used = set()
            n = 0
            for i, day in enumerate(days):
                k = round((i + 1) * per_week / 7) - round(i * per_week / 7)
                for j in range(k):
                    t = times[j % len(times)]
                    if f"{day} {t}" in taken or room <= 0:
                        n += 1
                        continue
                    room -= 1
                    type_ = types[n] if n < len(types) else None
                    bench = self.pick_benchmark(a, type_, used)
                    if bench:
                        used.add(bench)
                    if self.one("SELECT 1 FROM posts WHERE account_id=? AND slot_at=?", (a["id"], f"{day} {t}")):
                        continue  # another run already created this slot
                    pid = self.create_post(a["id"], f"{day} {t}", type_, None, bench, actor=actor)
                    created.append(pid)
                    n += 1
        return created

    def active_rules(self, account=None):
        rows = self.q("SELECT * FROM rules WHERE status='active' ORDER BY scope, id")
        if not account:
            return rows
        scopes = {"global", f"account:{account['id']}", f"platform:{account['platform']}",
                  f"country:{account['country']}"}
        return [r for r in rows if r["scope"] in scopes or r["scope"].startswith("type:")]

    def top_bottom(self, account_id, weeks, n=3):
        posts = [p for p in self.posts(account=account_id, week=weeks, status=["published", "measured"])
                 if p["impressions"] is not None and (p["type"] or "").lower() != "reply"]
        posts.sort(key=lambda p: -p["impressions"])
        top = posts[:n]
        bottom = posts[n:][-n:][::-1]  # never repeat a post already listed as best
        return top, bottom

    def brief(self, week, account=None):
        """Write one writing pack per account that has posts to write in `week`. Returns file paths."""
        out_dir = os.path.join(self.paths["work"], week)
        os.makedirs(out_dir, exist_ok=True)
        files = []
        prev = [shift_week(week, -1), shift_week(week, -2)]
        for a in self.accounts():
            if account and a["id"] != account:
                continue
            posts = self.posts(week=week, account=a["id"], status=["todo", "rejected"], full=True)
            if not posts:
                continue
            top, bottom = self.top_bottom(a["id"], prev)
            mk = self.one("SELECT * FROM markets WHERE code=?", (a["country"],)) or {}
            lines = [f"# 写稿包：{COUNTRIES.get(a['country'], a['country'])} · {a['name']}（{a['id']}）· {week}", "",
                     f"- 平台 {a['platform']} · 市场 {COUNTRIES.get(a['country'], a['country'])} · 语言 {a['lang']} · 账号 {a['handle'] or ''} · 发布 {a['bot'] or ''}",
                     "", "## 这个市场主推什么（先对齐这个）", "",
                     f"- 主推：{mk.get('focus') or '（未写）'}",
                     f"- 对谁说：{mk.get('audience') or '（未写）'}",
                     f"- 引导去哪：{mk.get('cta') or '（未写）'}",
                     *( [f"- 备注：{mk['notes']}"] if mk.get("notes") else [] ), "",
                     f"- 打法卡：memory/playbooks/{a['id']}.md（先读）",
                     "- 通用规则：memory/LESSONS.md（先读）", "",
                     "## 本账号格式规则", "", a["rules"] or "（未填，按打法卡）", "",
                     "## 当前生效的规则", ""]
            rules = self.active_rules(a)
            lines += [f"- [{r['scope']}] {r['text']}" for r in rules] or ["- （暂无）"]
            lines += ["", "## 近两周本账号表现（曝光取 7 天，没有则 24 小时）", ""]
            for label, group in (("最好", top), ("最差", bottom)):
                for p in group:
                    first = (p["text"] or "").strip().splitlines()[0][:80] if p["text"] else ""
                    lines.append(f"- {label} · {p['id']} · {p['type'] or ''} · 曝光 {p['impressions']} · {first} · {p['published_url'] or ''}")
            if not top:
                lines.append("- （没有带数据的已发帖子）")
            lines += ["", "## 内容策略（必读）", "",
                      "按数据目录 memory/内容策略.md 的配比和各渠道写法写（先读）；同一渠道一天最多 1 条固定栏目；",
                      "同一天同语言的两个渠道不要讲同一件事的同一句话，下面列出了同一天其他渠道已有的主题。", "",
                      "## 要写的帖子", ""]
            template = []
            for p in posts:
                same_day = self.q(
                    "SELECT p.account_id, p.topic, p.type FROM posts p JOIN accounts a ON a.id=p.account_id "
                    "WHERE substr(p.slot_at,1,10)=? AND p.id!=? AND p.status NOT IN ('cancelled','todo') AND a.country!='CN'",
                    (p["slot_at"][:10], p["id"]))
                lines += [f"### {p['id']} · {p['slot_at']} · 类型：{p['type'] or '未定'}", ""]
                if same_day:
                    lines.append("- 同一天其他渠道已有（别撞）：" + "；".join(
                        f"{r['account_id']} {r['topic'] or r['type'] or ''}"[:60] for r in same_day[:8]))
                if p["topic"]:
                    lines.append(f"- 主题：{p['topic']}")
                b = p.get("benchmark")
                if b:
                    lines += [f"- 对标：{b.get('competitor_name') or b.get('author') or ''} · {b['url']}",
                              f"  - 开头：{b['hook'] or '（未记录，打开原帖看）'}", f"  - 结构：{b['structure'] or ''}",
                              f"  - 数据：曝光 {_fmt(b['impressions'])} · 赞 {_fmt(b['likes'])} · 评 {_fmt(b['replies'])}",
                              f"  - 怎么替换成我们的：{b['how_to_adapt'] or '（未写，按我们自己的事实替换）'}"]
                rejects = [e for e in p["events"] if e["action"] in ("reject", "note")]
                if rejects:
                    lines.append(f"- 上次审核意见：{rejects[0]['detail']}")
                if p["text"]:
                    lines += ["- 当前稿（被打回，在此基础上改）：", "", "```", p["text"], "```"]
                for m in p["media"]:
                    lines.append(f"- 媒体槽 {m['slot']}（{m['kind']}）：{m['spec'] or ''} {'· 已有文件' if m['file'] else '· 待做'}")
                lines.append("")
                template.append({
                    "id": p["id"], "topic": p["topic"] or "", "text": "", "reply_thread": "",
                    "zh_ref": "" if not a["lang"].startswith("zh") else None,
                    "writer_note": "",
                    "media": [{"slot": m["slot"], "kind": m["kind"], "spec": m["spec"] or "", "file": m["file"] or ""}
                              for m in p["media"]] or [{"slot": 1, "kind": "image", "spec": "", "file": ""}],
                })
            out_json = os.path.join(out_dir, f"drafts-{a['id']}.json")
            lines += ["## 交稿方式", "",
                      f"把稿填进 `{out_json}`（已生成模板），然后运行：",
                      "", f"    beatsocial draft import {out_json} --author <你的名字>", "",
                      "- writer_note 写数据来源和需要审核特别核对的地方。",
                      "- 不需要媒体的帖子，把 media 改成 []；需要的写清 spec（尺寸、内容），file 有文件再填。",
                      "- 没有的事实写成 [待补：需要什么]，系统会挡住不让批准，不要编。"]
            path = os.path.join(out_dir, f"brief-{a['id']}.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            if not os.path.exists(out_json):
                with open(out_json, "w", encoding="utf-8") as fh:
                    json.dump({"week": week, "account": a["id"], "posts": template}, fh, ensure_ascii=False, indent=1)
            files.append(path)
        return files

    def import_drafts(self, path, author="unknown"):
        data = _loads(_read(path), None)
        if not data or not isinstance(data.get("posts"), list):
            raise BeatSocialError(f"{path} 不是有效的稿件文件：需要 {{\"posts\": [...]}}")
        result = {"imported": [], "not_written": [], "skipped": [], "flags": {}}
        for item in data["posts"]:
            pid = item.get("id")
            text = (item.get("text") or "").strip()
            if not pid:
                result["skipped"].append({"id": None, "reason": "缺少 id"})
                continue
            if not text:
                result["not_written"].append(pid)
                continue
            media = item.get("media")
            if isinstance(media, list):
                media = [m for m in media if m.get("spec") or m.get("file")]
            try:
                p = self.one("SELECT current_v FROM posts WHERE id=?", (pid,))
                if p is None:
                    raise BeatSocialError("没有这个帖子位")
                cur = self._version(pid, p["current_v"])
                cur_media = [(m["slot"], m["kind"], m["spec"] or "", m["file"] or "") for m in self._media(pid)]
                new_media = None if media is None else [
                    (int(m.get("slot") or 1), m.get("kind") or "image", m.get("spec") or "", m.get("file") or "")
                    for m in media]
                if (cur["text"] == text and cur["reply_thread"] == (item.get("reply_thread") or "")
                        and cur["zh_ref"] == (item.get("zh_ref") or "")
                        and (new_media is None or sorted(new_media) == sorted(cur_media))):
                    result["skipped"].append({"id": pid, "reason": "和当前版本一样"})
                    continue
                self.submit_version(pid, text, item.get("reply_thread") or "", item.get("zh_ref") or "",
                                    item.get("writer_note") or "", author, item.get("topic") or None, media or [],
                                    replace_media=media is not None)
                if item.get("type"):
                    with self.tx():
                        self.conn.execute("UPDATE posts SET type=? WHERE id=?", (item["type"].strip(), pid))
                post = self.post(pid, full=False)
                result["imported"].append(pid)
                if post["flags"]:
                    result["flags"][pid] = [f["msg"] for f in post["flags"]]
            except BeatSocialError as e:
                result["skipped"].append({"id": pid, "reason": str(e)})
        return result

    # ---------------------------------------------------------------- AI review
    def review_pack(self, week=None, account=None, status="review", since=None, until=None):
        posts = [p for p in self.posts(week=week, account=account, status=status, since=since, until=until, full=True)
                 if p["country"] != "CN"]
        out_dir = os.path.join(self.paths["work"], week or "all")
        os.makedirs(out_dir, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
        md = os.path.join(out_dir, f"review-pack-{stamp}.md")
        js = os.path.join(out_dir, f"review-result-{stamp}.json")
        lines = [f"# 审稿包 · {week or '全部'} · {len(posts)} 条", "",
                 "按 prompts/审稿.md 的清单逐条检查，把结论写进同目录的 review-result JSON。", ""]
        for p in posts:
            lines += [f"## {p['id']} · v{p['current_v']} · {p['account_name']}（{p['platform']} · {p['country']} · {p['lang']}）",
                      f"- 排期 {p['slot_at']} · 类型 {p['type'] or ''} · 系统检查：" +
                      ("；".join(f["msg"] for f in p["flags"]) or "无"),
                      f"- 账号规则：{p['account_rules'] or '见打法卡'}", "", "正文：", "```", p["text"], "```"]
            if p["reply_thread"]:
                lines += ["首评 / 回复串：", "```", p["reply_thread"], "```"]
            if p["zh_ref"]:
                lines += ["中文参考：", "```", p["zh_ref"], "```"]
            for m in p["media"]:
                lines.append(f"- 媒体槽 {m['slot']} {m['kind']}：{m['spec'] or ''} · {self.media_path(m['file']) or '无文件'}")
            if p.get("benchmark"):
                lines.append(f"- 对标：{p['benchmark']['url']} · 结构 {p['benchmark']['structure'] or ''}")
            lines.append("")
        with open(md, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        with open(js, "w", encoding="utf-8") as fh:
            json.dump({"reviewer": "", "results": [
                {"id": p["id"], "v": p["current_v"], "result": "pass|fail", "must_fix": [], "suggestions": [],
                 "notes": ""} for p in posts]}, fh, ensure_ascii=False, indent=1)
        return {"pack": md, "result_template": js, "count": len(posts)}

    def import_ai_review(self, path, reviewer=None):
        data = _loads(_read(path), None)
        if not data or not isinstance(data.get("results"), list):
            raise BeatSocialError(f"{path} 不是有效的审稿结果：需要 {{\"results\": [...]}}")
        reviewer = reviewer or data.get("reviewer") or "AI 审稿"
        done, stale = [], []
        for r in data["results"]:
            if r.get("result") not in ("pass", "fail"):
                continue
            p = self.one("SELECT current_v FROM posts WHERE id=?", (r.get("id"),))
            if not p:
                continue
            if int(r.get("v") or 0) != p["current_v"]:
                stale.append(r["id"])
                continue
            with self.tx():
                self.conn.execute("UPDATE posts SET ai_review=? WHERE id=?", (r["result"], r["id"]))
                self.event(r["id"], reviewer, "ai_review", {k: r.get(k) for k in ("v", "result", "must_fix", "suggestions", "notes")})
            done.append(r["id"])
        return {"recorded": done, "stale_version": stale}

    # -------------------------------------------------------------- competitors
    def upsert_competitor(self, cid, name, why="", notes="", status="active"):
        cid = re.sub(r"[\s/\\?#&]+", "-", (cid or "").strip().lower()).strip("-")[:60]
        if not cid:
            raise BeatSocialError("竞对 ID 不能为空")
        with self.tx():
            self.conn.execute(
                "INSERT INTO competitors(id, name, why, status, notes, created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, why=COALESCE(NULLIF(excluded.why,''), why), "
                "status=excluded.status, notes=COALESCE(NULLIF(excluded.notes,''), notes)",
                (cid, name, why, status, notes, now()))
        return cid

    def competitors(self):
        return self.q(
            "SELECT c.*, (SELECT COUNT(*) FROM samples s WHERE s.competitor_id=c.id) AS samples, "
            "(SELECT COUNT(*) FROM posts p JOIN samples s ON s.id=p.benchmark_id WHERE s.competitor_id=c.id) AS used "
            "FROM competitors c ORDER BY samples DESC, name")

    def add_sample(self, data: dict, actor="unknown"):
        url = check_url(data.get("url"))
        existing = self.one("SELECT id FROM samples WHERE url=?", (url,))
        sid = data.get("id") or (existing or {}).get("id")
        if not sid:
            n = self.one("SELECT COUNT(*) n FROM samples")["n"] + 1
            sid = f"S-{n:04d}"
            while self.one("SELECT id FROM samples WHERE id=?", (sid,)):
                n += 1
                sid = f"S-{n:04d}"
        comp = data.get("competitor_id")
        if not comp and data.get("competitor"):
            comp = self.upsert_competitor(data["competitor"], data["competitor"])
        cols = ["id", "competitor_id", "author", "platform", "country", "url", "posted_at", "observed_at", "type",
                "for_account", "hook", "structure", "cta", "media_kind", "impressions", "likes", "replies",
                "reposts", "bookmarks", "how_to_adapt", "evidence_note", "created_at"]
        row = {c: data.get(c) for c in cols}
        row.update({"id": sid, "competitor_id": comp, "url": url, "created_at": now()})
        for k in ("impressions", "likes", "replies", "reposts", "bookmarks"):
            row[k] = _to_int(row[k])
        with self.tx():
            if existing:
                sets = ", ".join(f"{c}=COALESCE(?, {c})" for c in cols if c not in ("id", "url", "created_at"))
                self.conn.execute(f"UPDATE samples SET {sets} WHERE id=?",
                                  [row[c] for c in cols if c not in ("id", "url", "created_at")] + [sid])
            else:
                self.conn.execute(f"INSERT INTO samples({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                  [row[c] for c in cols])
            self.event(None, actor, "sample", {"id": sid, "url": url})
        return sid

    def samples(self, platform=None, country=None, competitor=None, type_=None, limit=200):
        sql = ("SELECT s.*, c.name AS competitor_name, (SELECT COUNT(*) FROM posts p WHERE p.benchmark_id=s.id) AS used "
               "FROM samples s LEFT JOIN competitors c ON c.id=s.competitor_id WHERE 1=1")
        args = []
        for col, val in (("s.platform", platform), ("s.country", country), ("s.competitor_id", competitor), ("s.type", type_)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        return self.q(sql + " ORDER BY COALESCE(s.impressions, 0) DESC LIMIT ?", args + [limit])

    def import_samples(self, path, actor="unknown"):
        rows = []
        if path.endswith(".jsonl"):
            rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        elif path.endswith(".json"):
            data = json.load(open(path, encoding="utf-8"))
            rows = data.get("samples", data) if isinstance(data, dict) else data
        else:
            raise BeatSocialError("对标样本文件要用 .json 或 .jsonl")
        ids = [self.add_sample(r, actor) for r in rows]
        return ids

    # -------------------------------------------------------------------- rules
    def propose_rule(self, text, scope="global", evidence="", by="unknown"):
        if not re.fullmatch(r"global|(platform|country|account|type):.+", scope):
            raise BeatSocialError("scope 只能是 global、platform:x、country:JP、account:X-EN 或 type:<类型>")
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO rules(scope, text, evidence, status, proposed_by, proposed_at) VALUES(?,?,?,?,?,?)",
                (scope, text.strip(), evidence, "proposed", by, now()))
            self.event(None, by, "rule_propose", {"id": cur.lastrowid, "scope": scope})
        return cur.lastrowid

    def decide_rule(self, rule_id, status, by=OWNER, text=None, owner_said=None):
        if status not in ("active", "rejected", "retired"):
            raise BeatSocialError("规则状态只能改成 active、rejected、retired")
        r = self.one("SELECT * FROM rules WHERE id=?", (rule_id,))
        if not r:
            raise BeatSocialError(f"没有这条规则：{rule_id}")
        with self.tx():
            self.conn.execute("UPDATE rules SET status=?, decided_by=?, decided_at=?, text=COALESCE(?, text) WHERE id=?",
                              (status, by, now(), text, rule_id))
            self.event(None, by, "rule_" + status, {"id": rule_id, "owner_said": owner_said})
        self.render_lessons()

    def rules(self, status=None):
        if status:
            return self.q("SELECT * FROM rules WHERE status=? ORDER BY id DESC", (status,))
        return self.q("SELECT * FROM rules ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, id DESC")

    def render_lessons(self):
        rows = self.q("SELECT * FROM rules WHERE status='active' ORDER BY scope, id")
        lines = ["# LESSONS · 当前生效的写法规则", "",
                 "由 BeatSocial 根据「已确认」的规则自动生成，不要手改；要改规则请在审核台「规则」页处理。",
                 f"更新时间：{now()}", ""]
        scope = None
        for r in rows:
            if r["scope"] != scope:
                scope = r["scope"]
                lines += ["", f"## {scope}", ""]
            ev = f"（依据：{r['evidence']}）" if r["evidence"] else ""
            lines.append(f"- R{r['id']} {r['text']}{ev}")
        if not rows:
            lines.append("（还没有确认的规则）")
        path = os.path.join(self.paths["memory"], "LESSONS.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return path

    # -------------------------------------------------------------------- retro
    def retro(self, week):
        """Write memory/复盘/<week>.md (data part) and memory/quota/<next week>.json (suggestion)."""
        prev = shift_week(week, -1)
        nxt = shift_week(week, 1)
        cur_stats = {r["key"]: r for r in self.stats("account", week=week)}
        prev_stats = {r["key"]: r for r in self.stats("account", week=prev)}
        lines = [f"# {week} 周复盘（数据部分，系统生成）", "",
                 f"生成时间 {now()}。曝光优先用 7 天数据，没有就用 24 小时；拿不到的记为未知，不当 0。", "",
                 "## 各账号", "",
                 "| 账号 | 计划 | 已发 | 有数据 | 中位曝光 | 上周中位 | 总曝光 | 中位赞 | 互动率% |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for k, r in sorted(cur_stats.items()):
            pr = prev_stats.get(k, {})
            lines.append(f"| {k} | {r['planned']} | {r['published']} | {r['with_metrics']} | {_fmt(r['median_impressions'])} | "
                         f"{_fmt(pr.get('median_impressions'))} | {_fmt(r['total_impressions'])} | {_fmt(r['median_likes'])} | "
                         f"{_fmt(r['engagement_rate'])} |")
        lines += ["", "## 按市场", "", "| 市场 | 已发 | 中位曝光 | 总曝光 |", "|---|---|---|---|"]
        for r in self.stats("country", week=week):
            lines.append(f"| {r['key']} | {r['published']} | {_fmt(r['median_impressions'])} | {_fmt(r['total_impressions'])} |")
        lines += ["", "## 每个账号最好和最差的 3 条", ""]
        quota = {}
        for a in self.accounts():
            top, bottom = self.top_bottom(a["id"], [week])
            if top:
                lines.append(f"### {a['id']}")
                for label, group in (("最好", top), ("最差", bottom)):
                    for p in group:
                        first = (p["text"] or "").strip().splitlines()[0][:70] if p["text"] else ""
                        lines.append(f"- {label} · {p['id']} · {p['type'] or ''} · 曝光 {p['impressions']} · {first} · {p['published_url'] or ''}")
                lines.append("")
            if a["status"] == "active" and a["per_week"]:
                quota[a["id"]] = self._suggest_quota(a, week)
        unpublished = [p for p in self.posts(week=week, status=["approved", "review", "rejected", "todo"])
                       if parse_time(p["slot_at"]) < dt.datetime.now()]
        lines += ["## 漏发", "", f"计划时间已过但没发出：{len(unpublished)} 条"]
        for p in unpublished[:30]:
            lines.append(f"- {p['id']} · {p['status_label']} · {p['slot_at']}")
        missing = [p for p in self.posts(week=week, status=["published", "measured"]) if not p["metrics"]]
        lines += ["", "## 缺数据", "", f"已发但没有任何指标：{len(missing)} 条"]
        for p in missing[:30]:
            lines.append(f"- {p['id']} · {p['published_url'] or ''}")
        lines += ["", "## 配额建议", "",
                  f"已写入 memory/quota/{nxt}.json（状态：待确认）。在审核台「今天」页确认后才生效，"
                  "之后生成的帖子位都按它排。"]
        data_part = "\n".join(lines)
        os.makedirs(self.paths["retros"], exist_ok=True)
        path = os.path.join(self.paths["retros"], f"{week}.md")
        tail = ("\n## 结论与规则提案（由复盘 AI 填写，重跑复盘不会覆盖这一节）\n\n"
                "按 prompts/周复盘.md 填写三句话结论，并用 `beatsocial rule propose` 提交规则提案。\n")
        if os.path.exists(path):
            old_text = _read(path)
            if RETRO_END in old_text:
                tail = old_text.split(RETRO_END, 1)[1]
            else:  # a file written by hand or by an older version: keep all of it
                tail = "\n## 之前的内容（重跑复盘前已存在）\n\n" + old_text
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(RETRO_START + "\n" + data_part + "\n" + RETRO_END + tail)
        qpath = os.path.join(self.paths["quota"], f"{nxt}.json")
        existing = self.quota_file(nxt)
        if not existing or existing.get("status") != "confirmed":  # never overwrite a confirmed quota
            with open(qpath, "w", encoding="utf-8") as fh:
                json.dump({"week": nxt, "based_on": week, "status": "suggested", "accounts": quota}, fh,
                          ensure_ascii=False, indent=1)
        latest = max(f[:-3] for f in os.listdir(self.paths["retros"]) if re.fullmatch(r"\d{4}-W\d{2}\.md", f))
        with open(os.path.join(self.paths["retros"], "本周复盘.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# 最新复盘\n\n最新一期是 [{latest}]({latest}.md)。写稿前读那个文件，包括最后一节的结论。\n")
        return {"retro": path, "quota": qpath}

    def _suggest_quota(self, account, week):
        """Per-type counts for next week. Types in the top third by median impressions get +1, bottom third -1,
        total stays within ±30% of the account's per_week."""
        per_week = account["per_week"]
        posts = self.posts(account=account["id"], week=[week, shift_week(week, -1)], status=["published", "measured"])
        by_type = {}
        for p in posts:
            if p["impressions"] is not None and p["type"]:
                by_type.setdefault(p["type"], []).append(p["impressions"])
        by_type = {t: v for t, v in by_type.items() if len(v) >= 3}  # fewer than 3 posts: too noisy to judge
        in_force = self._quota(shift_week(week, 1)).get(account["id"])
        if in_force:  # start from the confirmed quota, so adjustments accumulate week over week
            counts = dict(in_force)
            base_types = list(in_force)
        else:
            base_types = account["types"] or sorted(by_type) or ["未分类"]
            counts = {t: 0 for t in base_types}
            for i in range(per_week):
                counts[base_types[i % len(base_types)]] += 1
        ranked = sorted(by_type, key=lambda t: -_median(by_type[t]))
        third = max(1, len(ranked) // 3) if len(ranked) >= 3 else 0
        if third:
            for t in ranked[:third]:
                counts[t] = counts.get(t, 0) + 1
            for t in ranked[-third:]:
                if counts.get(t, 0) > 0:
                    counts[t] -= 1
        total = sum(counts.values())
        import math

        lo, hi = math.ceil(per_week * 0.7), math.floor(per_week * 1.3)
        while total > hi:
            t = max(counts, key=counts.get)
            counts[t] -= 1
            total -= 1
        while total < lo:
            t = ranked[0] if ranked else base_types[0]
            counts[t] = counts.get(t, 0) + 1
            total += 1
        return {t: n for t, n in counts.items() if n > 0}

    # ------------------------------------------------------------------- backup
    def backup(self, commit=True):
        path = os.path.join(self.paths["backup"], "db.sql")
        with open(path, "w", encoding="utf-8") as fh:
            for line in self.conn.iterdump():
                fh.write(line + "\n")
        self.render_lessons()
        msg = None
        if commit and os.path.isdir(os.path.join(self.data_dir, ".git")):
            subprocess.run(["git", "-C", self.data_dir, "add", "backup", "memory", ".gitignore"], check=False)
            r = subprocess.run(["git", "-C", self.data_dir, "commit", "-q", "-m", f"backup {now()}"],
                               capture_output=True, text=True)
            msg = "已提交到 Git" if r.returncode == 0 else "没有变化，未提交"
        return {"dump": path, "git": msg}


class _Tx:
    def __init__(self, conn):
        self.conn = conn
        self.outer = False

    def __enter__(self):
        if self.conn.in_transaction:
            self.outer = True
        else:
            self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.outer:
            return False
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _to_int(v):
    if v in (None, ""):
        return None
    m = re.search(r"-?\d[\d,]*", str(v))
    return int(m.group(0).replace(",", "")) if m else None


def _fmt(v):
    if v is None:
        return "未知"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}" if isinstance(v, int) else str(v)


def receipts_from_csv(store: Store, path: str, by="unknown"):
    """Legacy receipt CSV: 帖子ID, 发布链接, 发布时间, bot, 备注, 24h浏览 ... 7天收藏."""
    fields = {"浏览": "impressions", "点赞": "likes", "评论": "replies", "转发": "reposts", "收藏": "bookmarks"}
    done, errors = [], []
    with open(path, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("帖子ID") or "").strip()
            if not pid:
                continue
            try:
                if row.get("发布链接"):
                    store.receipt(pid, row["发布链接"].strip(), row.get("发布时间") or None, row.get("bot") or by,
                                  row.get("备注") or "")
                for prefix, period in (("24h", "h24"), ("7天", "d7")):
                    vals = {fields[k]: row.get(prefix + k) for k in fields if row.get(prefix + k)}
                    if vals:
                        store.record_metric(pid, period, vals, source=f"csv:{os.path.basename(path)}", actor=row.get("bot") or by)
                done.append(pid)
            except BeatSocialError as e:
                errors.append({"id": pid, "reason": str(e)})
    return {"done": done, "errors": errors}
