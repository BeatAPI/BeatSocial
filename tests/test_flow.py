"""End-to-end tests of the BeatSocial loop on a throwaway data dir. Run: python3 -m unittest discover tests"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from beatsocial import core, db  # noqa: E402

NEXT_WEEK = core.iso_week(dt.date.today() + dt.timedelta(days=7))


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="beatsocial-test-")
        db.create(self.dir).close()
        self.st = core.Store(self.dir)
        self.st.upsert_account({"id": "X-EN", "name": "X 英文", "platform": "x", "country": "GLOBAL", "lang": "en",
                                "handle": "@BeatAPIio", "bot": "Lucy", "per_week": 7, "slot_times": "21:30",
                                "types": "创始人发布, 比价"})
        self.st.upsert_account({"id": "TH-JP", "name": "Threads 日文", "platform": "threads", "country": "JP",
                                "lang": "ja", "bot": "Yuki", "per_week": 3, "slot_times": ["12:00"]})
        self.st.add_sample({"url": "https://x.com/fal/status/1", "competitor": "fal", "platform": "x",
                            "country": "GLOBAL", "type": "比价", "impressions": "37,612", "structure": "hook → 价格 → CTA"})
        self.st.add_sample({"url": "https://x.com/kie/status/2", "competitor": "kie", "platform": "x",
                            "country": "GLOBAL", "type": "创始人发布", "impressions": 900})

    def tearDown(self):
        self.st.conn.close()
        shutil.rmtree(self.dir)

    def write(self, pid, text, reply="", zh="中文参考", media=None):
        return self.st.submit_version(pid, text, reply, zh, "", "Claude", media=media)

    # 5. account management -------------------------------------------------
    def test_account_validation_and_playbook(self):
        with self.assertRaises(core.BeatSocialError):
            self.st.upsert_account({"id": "BAD", "name": "x", "platform": "myspace", "country": "JP", "lang": "ja"})
        with self.assertRaises(core.BeatSocialError):
            self.st.upsert_account({"id": "X-KR", "name": "X 韩文", "platform": "x", "country": "KR", "lang": "ko",
                                    "slot_times": "9am"})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "memory", "playbooks", "X-EN.md")))
        a = self.st.upsert_account({"id": "X-EN", "status": "paused"})
        self.assertEqual(a["handle"], "@BeatAPIio")  # partial update keeps other fields

    # 1. batch writing + 2. benchmark reference ------------------------------
    def test_plan_brief_import(self):
        ids = self.st.plan_week(NEXT_WEEK)
        self.assertEqual(len([i for i in ids if "X-EN" in i]), 7)
        self.assertEqual(len([i for i in ids if "TH-JP" in i]), 3)
        self.assertEqual(self.st.plan_week(NEXT_WEEK), [])  # idempotent
        x_posts = self.st.posts(week=NEXT_WEEK, account="X-EN")
        self.assertEqual([p["type"] for p in x_posts][:2], ["创始人发布", "比价"])
        # type-matched benchmark first
        self.assertEqual(x_posts[1]["benchmark_id"], self.st.one("SELECT id FROM samples WHERE url LIKE '%fal%'")["id"])
        files = self.st.brief(NEXT_WEEK, "X-EN")
        brief = open(files[0], encoding="utf-8").read()
        self.assertIn("hook → 价格 → CTA", brief)
        drafts_path = os.path.join(self.dir, "work", NEXT_WEEK, "drafts-X-EN.json")
        data = json.load(open(drafts_path, encoding="utf-8"))
        data["posts"][0].update(text="Same model, two prices.", reply_thread="beatapi.io/pricing", zh_ref="同模型两种价",
                                media=[{"slot": 1, "kind": "image", "spec": "1080x1080 价卡"}])
        data["posts"][1].update(text="Hello [待补：真实成片]", zh_ref="你好", media=[])
        json.dump(data, open(drafts_path, "w", encoding="utf-8"), ensure_ascii=False)
        r = self.st.import_drafts(drafts_path, "Claude")
        self.assertEqual(len(r["imported"]), 2)
        p0 = self.st.post(data["posts"][0]["id"])
        self.assertEqual(p0["status"], "review")
        self.assertIn("media_missing", [f["code"] for f in p0["flags"]])
        p1 = self.st.post(data["posts"][1]["id"])
        self.assertIn("placeholder", [f["code"] for f in p1["flags"]])

    # approval rules ---------------------------------------------------------
    def test_approval_binding_and_blocks(self):
        pid = self.st.create_post("X-EN", f"{dt.date.today() + dt.timedelta(days=1)} 21:30", "比价")
        self.write(pid, "Seedance 8s: fal $2.43 vs BeatAPI $1.20")
        with self.assertRaises(core.BeatSocialError) as e:
            self.st.approve(pid)
        self.assertIn("第三方价格", str(e.exception))
        self.write(pid, "Seedance 8s on BeatAPI: $1.20")
        with self.assertRaises(core.BeatSocialError):
            self.st.approve(pid)  # needs price confirmation
        self.st.approve(pid, price_checked=True)
        self.assertTrue(self.st.post(pid)["approval_valid"])
        q = self.st.queue("Lucy")
        self.assertEqual(q["items"], [])  # tomorrow: not due yet, never publish early
        self.assertEqual([i["id"] for i in q["upcoming"]], [pid])
        self.assertEqual(self.st.queue("Yuki")["upcoming"], [])
        # editing voids approval
        self.write(pid, "Seedance 8s on BeatAPI: $1.20 now")
        p = self.st.post(pid)
        self.assertEqual(p["status"], "review")
        self.assertFalse(p["approval_valid"])
        self.assertEqual(self.st.queue("Lucy")["upcoming"], [])
        # batch approve skips blocked items
        other = self.st.create_post("X-EN", f"{dt.date.today() + dt.timedelta(days=2)} 21:30")
        self.write(other, "TODO write this")
        r = self.st.approve_many([pid, other], price_checked=True)
        self.assertEqual(r["approved"], [pid])
        self.assertEqual(r["skipped"][0]["id"], other)

    def test_media_change_voids_approval(self):
        img = os.path.join(self.dir, "media", "card.png")
        open(img, "wb").write(b"png")
        pid = self.st.create_post("X-EN", f"{dt.date.today() + dt.timedelta(days=1)} 21:30")
        self.write(pid, "A post", media=[{"slot": 1, "file": "card.png"}])
        self.st.approve(pid)
        self.st.set_media(pid, 1, file="card.png", spec="new spec text only")
        self.assertTrue(self.st.post(pid)["approval_valid"])  # same file, spec change only
        open(os.path.join(self.dir, "media", "v2.mp4"), "wb").write(b"mp4")
        self.st.set_media(pid, 2, file="v2.mp4")
        p = self.st.post(pid)
        self.assertEqual(p["status"], "review")
        self.assertEqual(p["media"][1]["kind"], "video")

    # publishing + 3. metrics loop --------------------------------------------
    def test_claim_receipt_metrics(self):
        pid = self.st.create_post("X-EN", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.write(pid, "Post body")
        self.st.approve(pid)
        h = self.st.post(pid)["approved_hash"]
        self.assertNotIn("warning", self.st.claim(pid, "Lucy", h))
        self.assertIn("warning", self.st.claim(pid, "Lucy", h))  # re-claim: check platform before re-posting
        with self.assertRaises(core.BeatSocialError):
            self.st.claim(pid, "Yuki", h)  # not the responsible bot
        self.st.receipt(pid, "https://x.com/BeatAPIio/status/1", by="Lucy")
        with self.assertRaises(core.BeatSocialError):
            self.st.receipt(pid, "https://x.com/BeatAPIio/status/2", by="Lucy")  # no double publish
        with self.assertRaises(core.BeatSocialError):
            self.st.record_metric(pid, "h24", {"impressions": ""})  # nothing known -> refuse, never 0
        self.st.record_metric(pid, "h24", {"impressions": "1,200", "likes": 5, "video_views": 30})
        m = self.st.post(pid)["metrics"]["h24"]
        self.assertEqual(m["impressions"], 1200)
        self.assertIsNone(m["replies"])
        self.assertEqual(json.loads(m["extra"]), {"video_views": 30})
        self.st.record_metric(pid, "d7", {"impressions": 2000})
        self.assertEqual(self.st.post(pid)["status"], "measured")
        unapproved = self.st.create_post("X-EN", f"{dt.date.today()} 08:00")
        self.write(unapproved, "not approved")
        with self.assertRaises(core.BeatSocialError):
            self.st.receipt(unapproved, "https://x.com/a/status/9")

    def test_off_plan_and_stats(self):
        pid = self.st.off_plan("X-EN", "https://x.com/BeatAPIio/status/77", f"{dt.date.today()} 10:00", "hand post", "Owner")
        self.st.record_metric(pid, "h24", {"impressions": 100, "likes": 2, "replies": 1})
        pid2 = self.st.off_plan("TH-JP", "https://threads.net/p/1", f"{dt.date.today()} 11:00", "jp", "Yuki")
        self.st.record_metric(pid2, "h24", {"impressions": 10})
        rows = {r["key"]: r for r in self.st.stats("country")}
        self.assertEqual(rows["GLOBAL"]["median_impressions"], 100)
        self.assertEqual(rows["GLOBAL"]["engagement_rate"], 3.0)
        self.assertEqual(rows["JP"]["total_impressions"], 10)
        with self.assertRaises(core.BeatSocialError):
            self.st.off_plan("X-EN", "https://x.com/BeatAPIio/status/77", f"{dt.date.today()} 10:00")

    # 4. retro + rules memory -------------------------------------------------
    def test_retro_quota_and_rules(self):
        week = core.iso_week(dt.date.today())
        for i, (t, imp) in enumerate([("比价", 500), ("比价", 400), ("创始人发布", 20), ("教程", 90)]):
            pid = self.st.off_plan("X-EN", f"https://x.com/s/{i}", f"{dt.date.today()} 0{i}:00", f"t{i}")
            self.st.conn.execute("UPDATE posts SET type=? WHERE id=?", (t, pid))
            self.st.record_metric(pid, "h24", {"impressions": imp})
        r = self.st.retro(week)
        quota = json.load(open(r["quota"], encoding="utf-8"))["accounts"]["X-EN"]
        total = sum(quota.values())
        self.assertTrue(5 <= total <= 9, quota)  # within ±30% of 7
        self.assertGreaterEqual(quota.get("比价", 0), 1)
        self.assertIn("最好", open(r["retro"], encoding="utf-8").read())
        rid = self.st.propose_rule("比价帖开头先给同规格两个数字", "account:X-EN", "W46 比价中位 450", "Grok")
        lessons = open(os.path.join(self.dir, "memory", "LESSONS.md"), encoding="utf-8").read() if os.path.exists(
            os.path.join(self.dir, "memory", "LESSONS.md")) else ""
        self.assertNotIn("同规格两个数字", lessons)
        self.st.decide_rule(rid, "active")
        lessons = open(os.path.join(self.dir, "memory", "LESSONS.md"), encoding="utf-8").read()
        self.assertIn("同规格两个数字", lessons)
        self.assertEqual(len(self.st.active_rules(self.st.account("X-EN"))), 1)
        self.assertEqual(len(self.st.active_rules(self.st.account("TH-JP"))), 0)

    def test_ai_review_stale_version(self):
        pid = self.st.create_post("TH-JP", f"{dt.date.today() + dt.timedelta(days=1)} 12:00")
        self.write(pid, "日本語の投稿")
        pack = self.st.review_pack(None, "TH-JP")
        res = json.load(open(pack["result_template"], encoding="utf-8"))
        res["results"][0].update(result="fail", must_fix=["敬語"])
        json.dump(res, open(pack["result_template"], "w", encoding="utf-8"), ensure_ascii=False)
        self.write(pid, "日本語の投稿 v2")
        out = self.st.import_ai_review(pack["result_template"], "Codex")
        self.assertEqual(out["stale_version"], [pid])
        self.assertIsNone(self.st.post(pid)["ai_review"])

    def test_backup_dump(self):
        r = self.st.backup(commit=False)
        self.assertIn("CREATE TABLE", open(r["dump"], encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
