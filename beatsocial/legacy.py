"""[INPUT] The old review desk plan.json (channels + posts) and optionally the 对标总库 CSV.
[OUTPUT] Accounts, competitors, samples, posts, versions, media slots, events and metrics in BeatSocial.
[POS] One-time migration. Reads the old files, never writes to them. Safe to run again (skips existing ids).
[PROTOCOL] Unknown legacy fields are kept verbatim in posts.legacy so nothing is lost.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re

from .core import BeatSocialError, Store, _to_int, now, parse_time, iso_week

PLATFORM_BY_KEY = [
    (r"^X-", "x"), (r"^TH-", "threads"), (r"^LI", "linkedin"), (r"^YT-", "youtube"), (r"^RU-VID", "rutube"),
    (r"^BLOG", "blog"), (r"^Qiita", "qiita"), (r"^note", "note"), (r"^NAVER", "naver"), (r"^VELOG", "velog"),
    (r"^GEEKNEWS", "geeknews"), (r"^TISTORY", "tistory"), (r"^HABR", "habr"), (r"^DZEN", "dzen"),
    (r"^VC", "vcru"), (r"^TG-", "telegram"), (r"^BILI", "bilibili"), (r"^CN-", "douyin"),
]
COUNTRY_BY_MARKET = {"英文": "GLOBAL", "日本": "JP", "韩国": "KR", "繁中": "TW", "俄罗斯": "RU", "国内": "CN"}
STATUS = {"待写": "todo", "待审": "review", "打回": "rejected", "已批准": "approved", "已交接": "approved",
          "已发布": "published", "已回测": "measured"}
LINK_RULES = {
    "x": "正文不放链接；链接只放第一条回复或图/视频上；≤280 字符（中日韩算 2）",
    "threads": "产品帖链接放正文最后一行；≤500 字",
    "linkedin": "链接放第一条评论",
}
PLATFORM_BY_CHANNEL = [
    ("X", "x"), ("Threads", "threads"), ("LinkedIn", "linkedin"), ("YouTube", "youtube"), ("Reddit", "reddit"),
    ("Instagram", "instagram"), ("TikTok", "tiktok"), ("Telegram", "telegram"), ("note", "note"), ("Qiita", "qiita"),
]
COUNTRY_BY_HINT = [("日", "JP"), ("韩", "KR"), ("韓", "KR"), ("繁", "TW"), ("俄", "RU"), ("中文", "CN"), ("英", "GLOBAL")]


def platform_for(key):
    for pat, plat in PLATFORM_BY_KEY:
        if re.search(pat, key):
            return plat
    return "other"


def run(store: Store, plan_path: str, benchmarks_csv: str | None = None) -> dict:
    plan = json.load(open(plan_path, encoding="utf-8"))
    media_dir = os.path.join(os.path.dirname(os.path.abspath(plan_path)), "media")
    report = {"accounts": 0, "competitors": 0, "samples": 0, "posts": 0, "skipped_posts": 0, "metrics": 0,
              "media_files": 0, "media_needed": 0}

    # accounts: learn usual slot times and content types from past posts
    from collections import Counter

    times, types = {}, {}
    for p in plan.get("posts", []):
        if p.get("time") and re.fullmatch(r"\d\d:\d\d", p["time"]):
            times.setdefault(p.get("channel"), Counter())[p["time"]] += 1
        if p.get("type") and p.get("type") != "reply":
            types.setdefault(p.get("channel"), Counter())[p["type"]] += 1
    for ch in plan.get("channels", []):
        note = ch.get("handoff_note") or ""
        acct = ch.get("account") or ""
        status = "paused" if note.startswith("⛔") else ("pending" if re.search(r"待开|未开|待定", acct) else "active")
        platform = platform_for(ch["key"])
        per_day = ch.get("per_day") or 0
        n_times = max(1, int(round(per_day))) if per_day else 1
        usual_times = sorted(t for t, _ in times.get(ch["key"], Counter()).most_common(n_times))
        usual_types = [t for t, _ in types.get(ch["key"], Counter()).most_common(6)]
        store.upsert_account({
            "slot_times": usual_times, "types": usual_types,
            "id": ch["key"], "name": ch.get("name") or ch["key"], "platform": platform,
            "country": COUNTRY_BY_MARKET.get(ch.get("market"), "GLOBAL"), "lang": ch.get("lang") or "en",
            "handle": acct, "bot": ch.get("bot") or "", "per_week": int(round(per_day * 7)),
            "rules": LINK_RULES.get(platform, ""), "status": status, "notes": note,
            "tz": {"JP": "Asia/Tokyo", "KR": "Asia/Seoul", "RU": "Europe/Moscow", "TW": "Asia/Taipei"}.get(
                COUNTRY_BY_MARKET.get(ch.get("market"), ""), "Asia/Shanghai"),
        }, actor="legacy-import")
        report["accounts"] += 1

    # benchmark library
    if benchmarks_csv:
        with open(benchmarks_csv, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                url = (row.get("原帖URL") or "").strip()
                if not url:
                    continue
                channel = row.get("渠道") or ""
                platform = next((p for k, p in PLATFORM_BY_CHANNEL if channel.startswith(k)), "other")
                hint = channel.split("（")[1] if "（" in channel else (row.get("我方账号或产品") or "")
                country = next((c for k, c in COUNTRY_BY_HINT if k in hint), "GLOBAL")
                comp = (row.get("竞对或参考对象") or "").strip() or "未分类"
                comp_id = store.upsert_competitor(re.sub(r"\s+", "-", comp.split("·")[0].strip()) or "misc", comp)
                store.add_sample({
                    "id": row.get("ID"), "competitor_id": comp_id, "author": row.get("作者"), "platform": platform,
                    "country": country, "url": url, "posted_at": row.get("原页发布日期"),
                    "observed_at": row.get("观察日期"), "for_account": None, "hook": row.get("原文短开头"),
                    "structure": row.get("文案结构"), "cta": row.get("原帖CTA"), "media_kind": row.get("素材形式"),
                    "impressions": row.get("浏览量"), "likes": row.get("喜欢或反应"), "replies": row.get("评论或回复"),
                    "reposts": row.get("转发"), "bookmarks": row.get("书签或收藏"),
                    "how_to_adapt": row.get("可以怎样替换"), "evidence_note": row.get("证据边界"),
                }, actor="legacy-import")
                report["samples"] += 1
    report["competitors"] = len(store.competitors())

    accounts = {a["id"]: a for a in store.accounts()}
    report["errors"] = []
    for p in plan.get("posts", []):
        try:
            accounts = _import_post(store, p, accounts, media_dir, report)
        except Exception as e:  # one bad legacy row must not stop the migration
            report["errors"].append({"id": p.get("id"), "error": str(e)})
    store.render_lessons()
    return report


def _import_post(store, p, accounts, media_dir, report):
    """Import one legacy post row; returns the (possibly refreshed) account map."""
    pid = p["id"]
    if store.one("SELECT id FROM posts WHERE id=?", (pid,)):
        report["skipped_posts"] += 1
        return accounts
    acc = p.get("channel")
    if acc not in accounts:
        store.upsert_account({"id": acc, "name": p.get("channel_name") or acc, "platform": platform_for(acc),
                              "country": COUNTRY_BY_MARKET.get(p.get("market"), "GLOBAL"),
                              "lang": p.get("lang") or "en", "handle": p.get("account"), "status": "paused"},
                             actor="legacy-import")
        accounts = {a["id"]: a for a in store.accounts()}
    raw_time = (p.get("time") or "").strip()
    hhmm = raw_time if re.fullmatch(r"\d{1,2}:\d\d", raw_time) else "12:00"  # e.g. "全天" -> noon
    slot = f"{p['date']} {hhmm}"
    bench_id = None
    b = p.get("benchmark") or {}
    if b.get("url"):
        found = store.one("SELECT id FROM samples WHERE url=?", (b["url"].strip(),))
        if found:
            bench_id = found["id"]
        else:
            who = (b.get("who") or "未分类").split("·")[0].strip()
            bench_id = store.add_sample({
                "competitor_id": store.upsert_competitor(re.sub(r"\s+", "-", who) or "misc", who),
                "author": b.get("who"), "platform": accounts[acc]["platform"], "country": accounts[acc]["country"],
                "url": b["url"].strip(), "structure": b.get("structure"), "for_account": acc,
                "evidence_note": b.get("stats"), "impressions": _first_number(b.get("stats")),
            }, actor="legacy-import")
    status = STATUS.get(p.get("status"), "todo")
    when = parse_time(slot)
    with store.tx():
        store.conn.execute(
            "INSERT INTO posts(id, account_id, week, slot_at, type, topic, product, source_kind, source_url, "
            "benchmark_id, status, current_v, bot, published_url, published_at, published_by, legacy, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, acc, iso_week(when.date()), when.strftime("%Y-%m-%d %H:%M"), p.get("type"), p.get("topic"),
             p.get("product"), "reuse" if "复用" in (p.get("reuse") or "") else ("benchmark" if bench_id else "original"),
             p.get("source_url") or None, bench_id, status, 1 if (p.get("draft") or "").strip() else 0,
             p.get("bot") or None, p.get("published_url") or None,
             _clean_time(p.get("published_at")), p.get("bot") or None,
             json.dumps(p, ensure_ascii=False), now(), now()))
        if (p.get("draft") or "").strip():
            store.conn.execute(
                "INSERT INTO versions(post_id, v, text, reply_thread, zh_ref, writer_note, author, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (pid, 1, p.get("draft") or "", p.get("reply_thread") or "", p.get("zh_ref") or "",
                 p.get("writer_note") or "", "legacy", p.get("updated_at") or now()))
        files = sorted(glob.glob(os.path.join(media_dir, glob.escape(pid) + ".*")) +
                       glob.glob(os.path.join(media_dir, glob.escape(pid) + "-[0-9]*.*")))
        files = [f for f in files if re.fullmatch(re.escape(pid) + r"(-\d+)?\.\w+", os.path.basename(f))]
        img = (p.get("image") or "").strip()
        if img and os.path.exists(img) and img not in files:
            files.insert(0, img)
        for i, f in enumerate(files, 1):
            store._set_media(pid, {"slot": i, "file": _adopt(store, f), "spec": p.get("media_need") if i == 1 else ""},
                             "legacy-import")
            report["media_files"] += 1
        need = (p.get("media_need") or "").strip()
        if not files and need and status in ("todo", "review", "rejected", "approved") and not re.match(r"^(无|不需要|不配图|纯文字|—|-|none\b)", need, re.I):
            store._set_media(pid, {"slot": 1, "kind": "video" if re.search(r"视频|video|mp4", need, re.I) else "image",
                                   "spec": need, "file": None}, "legacy-import")
            report["media_needed"] += 1
        if p.get("review_note"):
            store.event(pid, "legacy", "note", p["review_note"].strip())
        store.event(pid, "legacy-import", "import", {"status": p.get("status")})
    # approved posts keep their approval, bound to the imported content
    if status == "approved":
        h = store.content_hash(pid)
        store.conn.execute("UPDATE posts SET approved_v=current_v, approved_hash=?, approved_at=?, approved_by=? "
                           "WHERE id=?", (h, p.get("updated_at") or now(), "legacy", pid))
    for period in ("h24", "d7"):
        m = (p.get("metrics") or {}).get(period)
        if isinstance(m, dict) and status in ("published", "measured"):
            vals = {
                "impressions": _pick(m, "impressions", "views", "video_views"),
                "likes": _pick(m, "likes", "reactions"),
                "replies": _pick(m, "replies", "comments"),
                "reposts": _pick(m, "reposts", "retweets"),
                "quotes": m.get("quotes"), "bookmarks": m.get("bookmarks"), "clicks": m.get("clicks"),
            }
            extra = {k: v for k, v in m.items() if isinstance(v, (int, float)) and k not in
                     ("impressions", "views", "video_views", "likes", "reactions", "replies", "comments",
                      "reposts", "retweets", "quotes", "bookmarks", "clicks", "hours_after_post")}
            vals.update(extra)
            vals = {k: v for k, v in vals.items() if v is not None}
            if vals:
                try:
                    store.record_metric(pid, period, vals, source=m.get("source") or "legacy",
                                        collected_at=m.get("read_at") or m.get("collected_at"), actor="legacy-import")
                    report["metrics"] += 1
                except BeatSocialError:
                    pass
    report["posts"] += 1
    return accounts


def _adopt(store: Store, src: str) -> str:
    """Bring a legacy media file into <data>/media/legacy/ as a hard link (no extra disk space).
    Falls back to the original absolute path if linking is not possible (e.g. another volume)."""
    dest_dir = os.path.join(store.paths["media"], "legacy")
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(src)
    dest = os.path.join(dest_dir, name)
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(dest):
        if os.path.samefile(dest, src):
            return os.path.relpath(dest, store.paths["media"])
        n += 1
        dest = os.path.join(dest_dir, f"{stem}~{n}{ext}")
    try:
        os.link(src, dest)
    except OSError:
        import shutil

        shutil.copy2(src, dest)  # different volume: copy so the desk only ever serves files under media/
    return os.path.relpath(dest, store.paths["media"])


def _pick(m, *keys):
    for k in keys:
        if m.get(k) is not None:
            return m[k]
    return None


def _first_number(text):
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*万", text)
    if m:
        return int(float(m.group(1)) * 10000)
    return _to_int(text)


def _clean_time(v):
    if not v:
        return None
    try:
        return parse_time(v).strftime("%Y-%m-%d %H:%M")
    except BeatSocialError:
        return None
