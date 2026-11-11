"""Regression tests for issues found in the first independent review. Run: python3 -m unittest discover tests"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from beatsocial import core, db, server  # noqa: E402

TOMORROW = dt.date.today() + dt.timedelta(days=1)


class RegressionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="beatsocial-reg-")
        db.create(self.dir).close()
        self.st = core.Store(self.dir)
        self.st.upsert_account({"id": "X-EN", "name": "X 英文", "platform": "x", "country": "GLOBAL", "lang": "en",
                                "bot": "Lucy", "per_week": 2, "slot_times": ["21:30"], "types": ["a", "b"]})

    def tearDown(self):
        self.st.conn.close()
        shutil.rmtree(self.dir)

    def approved_post(self, when=None):
        pid = self.st.create_post("X-EN", when or dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.st.submit_version(pid, "body", "", "参考", "", "Claude")
        self.st.approve(pid)
        return pid

    def test_parse_time_converts_zones(self):
        self.assertEqual(core.parse_time("2026-10-28T14:45:00Z"), dt.datetime(2026, 10, 28, 22, 45))
        self.assertEqual(core.parse_time("2026-10-28T21:39:00+08:00"), dt.datetime(2026, 10, 28, 21, 39))
        self.assertEqual(core.parse_time("2026-10-28 22:00+0900"), dt.datetime(2026, 10, 28, 21, 0))
        self.assertEqual(core.parse_time("2026-10-29 16:33 Asia/Shanghai"), dt.datetime(2026, 10, 29, 16, 33))
        self.assertEqual(core.parse_time("2026-10-26T05:31:55.768Z"), dt.datetime(2026, 10, 26, 13, 31, 55, 768000))

    def test_claim_is_exclusive_under_concurrency(self):
        pid = self.approved_post()
        wins, errors = [], []

        def grab(bot):
            st = core.Store(self.dir)
            try:
                st.claim(pid, bot)
                wins.append(bot)
            except core.BeatSocialError:
                errors.append(bot)
            finally:
                st.conn.close()

        # different bots racing: post has no fixed bot, so nobody is excluded up front
        self.st.conn.execute("UPDATE posts SET bot=NULL WHERE id=?", (pid,))
        self.st.conn.execute("UPDATE accounts SET bot=NULL WHERE id='X-EN'")
        threads = [threading.Thread(target=grab, args=(f"bot{i}",)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(wins), 1, wins)

    def test_receipt_first_url_wins(self):
        pid = self.approved_post()
        self.st.claim(pid, "Lucy")
        with self.assertRaises(core.BeatSocialError):
            self.st.receipt(pid, "https://x.com/a/status/1", by="Yuki")  # not the claimant
        self.st.receipt(pid, "https://x.com/a/status/1", "2026-11-01 10:00", by="Lucy")
        self.st.receipt(pid, "https://x.com/a/status/1", "2026-11-02 10:00", by="Lucy")  # same url again
        self.assertEqual(self.st.post(pid)["published_at"], "2026-11-01 10:00")  # time not overwritten
        with self.assertRaises(core.BeatSocialError):
            self.st.receipt(pid, "https://x.com/a/status/2", by="Lucy")
        self.st.receipt(pid, "https://x.com/a/status/2", by="Owner", force=True)
        self.assertEqual(self.st.post(pid)["published_url"], "https://x.com/a/status/2")
        with self.assertRaises(core.BeatSocialError):
            self.st.receipt(pid, "javascript:alert(1)", by="Lucy", force=True)

    def test_approve_refuses_content_changed_since_viewed(self):
        pid = self.st.create_post("X-EN", f"{TOMORROW} 21:30")
        self.st.submit_version(pid, "v1", "", "参考", "", "Claude")
        seen = self.st.post(pid)["content_hash"]
        self.st.submit_version(pid, "v2 unseen", "", "参考", "", "Claude")
        with self.assertRaises(core.BeatSocialError):
            self.st.approve(pid, expected_hash=seen)
        self.st.approve(pid, expected_hash=self.st.post(pid)["content_hash"])

    def test_media_rewrite_same_size_voids_approval(self):
        path = os.path.join(self.dir, "media", "a.png")
        open(path, "wb").write(b"AAAA")
        pid = self.st.create_post("X-EN", f"{TOMORROW} 21:30")
        self.st.submit_version(pid, "body", "", "参考", "", "Claude", media=[{"slot": 1, "file": "a.png"}])
        self.st.approve(pid)
        os.utime(path, ns=(1, 1))
        open(path, "wb").write(b"BBBB")
        self.assertFalse(self.st.post(pid)["approval_valid"])
        self.assertEqual(self.st.queue("Lucy", hours=72)["items"], [])

    def test_media_outside_root_refused(self):
        pid = self.st.create_post("X-EN", f"{TOMORROW} 21:30")
        outside = os.path.join(tempfile.gettempdir(), "beatsocial-secret.txt")
        open(outside, "w").write("secret")
        with self.assertRaises(core.BeatSocialError):
            self.st.set_media(pid, 1, file=outside)
        with self.assertRaises(core.BeatSocialError):
            self.st.set_media(pid, 1, file="../beatsocial.db")
        os.remove(outside)

    def test_import_on_published_post_keeps_media(self):
        open(os.path.join(self.dir, "media", "a.png"), "wb").write(b"A")
        pid = self.st.create_post("X-EN", f"{TOMORROW} 21:30")
        self.st.submit_version(pid, "body", "", "参考", "", "Claude", media=[{"slot": 1, "file": "a.png"}])
        self.st.approve(pid)
        self.st.receipt(pid, "https://x.com/a/status/5", by="Lucy")
        path = os.path.join(self.dir, "drafts.json")
        json.dump({"posts": [{"id": pid, "text": "changed", "media": []}]}, open(path, "w"))
        r = self.st.import_drafts(path)
        self.assertEqual(r["skipped"][0]["id"], pid)
        self.assertEqual(len(self.st.post(pid)["media"]), 1)

    def test_media_only_change_is_imported(self):
        open(os.path.join(self.dir, "media", "a.png"), "wb").write(b"A")
        pid = self.st.create_post("X-EN", f"{TOMORROW} 21:30")
        self.st.submit_version(pid, "body", "", "参考", "", "Claude")
        path = os.path.join(self.dir, "drafts.json")
        json.dump({"posts": [{"id": pid, "text": "body", "zh_ref": "参考", "media": [{"slot": 1, "file": "a.png"}]}]},
                  open(path, "w"))
        self.assertEqual(self.st.import_drafts(path)["imported"], [pid])

    def test_metric_update_keeps_earlier_fields(self):
        pid = self.approved_post()
        self.st.receipt(pid, "https://x.com/a/status/9", by="Lucy")
        self.st.record_metric(pid, "h24", {"impressions": 500, "likes": 5})
        self.st.record_metric(pid, "h24", {"bookmarks": 2})
        m = self.st.post(pid)["metrics"]["h24"]
        self.assertEqual((m["impressions"], m["likes"], m["bookmarks"]), (500, 5, 2))

    def test_engagement_rate_only_over_posts_with_impressions(self):
        a = self.st.off_plan("X-EN", "https://x.com/a/status/10", f"{dt.date.today()} 01:00")
        self.st.record_metric(a, "h24", {"impressions": 1000, "likes": 2, "replies": 0})
        b = self.st.off_plan("X-EN", "https://x.com/a/status/11", f"{dt.date.today()} 02:00")
        self.st.record_metric(b, "h24", {"likes": 500})  # no impressions known
        c = self.st.off_plan("X-EN", "https://x.com/a/status/12", f"{dt.date.today()} 03:00")
        self.st.record_metric(c, "h24", {"impressions": 1000, "likes": 300})  # replies unknown: not 0
        row = self.st.stats("account")[0]
        self.assertEqual(row["engagement_rate"], 0.2)
        self.assertEqual(self.st.stats("account", period="d7")[0]["with_metrics"], 0)

    def test_claim_needs_current_hash_and_takeover_warns(self):
        pid = self.approved_post()
        h = self.st.post(pid)["approved_hash"]
        with self.assertRaises(core.BeatSocialError):
            self.st.claim(pid, "Lucy", "stale-hash")
        self.assertNotIn("warning", self.st.claim(pid, "Lucy", h))
        self.st.conn.execute("UPDATE posts SET bot=NULL, claimed_at='2000-01-01 00:00:00' WHERE id=?", (pid,))
        self.st.conn.execute("UPDATE accounts SET bot=NULL")
        r = self.st.claim(pid, "Yuki", h)  # Lucy's claim expired: Yuki takes over
        self.assertIn("Lucy", r["warning"])

    def test_retro_keeps_unmarked_old_file(self):
        week = core.iso_week(dt.date.today())
        path = os.path.join(self.dir, "memory", "复盘", f"{week}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write("手写的旧复盘结论")
        self.st.retro(week)
        self.assertIn("手写的旧复盘结论", open(path, encoding="utf-8").read())
        self.st.retro(core.shift_week(week, -3))  # re-running an old week keeps "latest" on the newest
        self.assertIn(week, open(os.path.join(self.dir, "memory", "复盘", "本周复盘.md"), encoding="utf-8").read())

    def test_plan_not_blocked_by_off_plan_posts(self):
        next_week = core.iso_week(dt.date.today() + dt.timedelta(days=7))
        day = core.week_days(next_week)[2]
        self.st.off_plan("X-EN", "https://x.com/a/status/12", f"{day} 10:00")
        self.assertEqual(len(self.st.plan_week(next_week)), 2)

    def test_quota_within_30_percent(self):
        week = core.iso_week(dt.date.today())
        for i, (t, imp) in enumerate([("a", 900), ("b", 10), ("c", 500)]):
            pid = self.st.off_plan("X-EN", f"https://x.com/q/status/{i}", f"{dt.date.today()} 0{i}:00")
            self.st.conn.execute("UPDATE posts SET type=? WHERE id=?", (t, pid))
            self.st.record_metric(pid, "h24", {"impressions": imp})
        q = self.st._suggest_quota(self.st.account("X-EN"), week)
        self.assertEqual(sum(q.values()), 2)  # per_week=2: ceil(1.4)=2 .. floor(2.6)=2

    def test_quota_needs_confirmation_and_retro_keeps_conclusions(self):
        week = core.iso_week(dt.date.today())
        nxt = core.shift_week(week, 1)
        r = self.st.retro(week)
        self.assertEqual(self.st._quota(nxt), {})  # suggested only: not in force
        self.st.confirm_quota(nxt, {"X-EN": {"a": 2}})
        self.assertEqual(self.st._quota(nxt), {"X-EN": {"a": 2}})
        self.assertEqual(self.st._quota(core.shift_week(nxt, 1)), {"X-EN": {"a": 2}})  # stays until replaced
        with open(r["retro"], "a", encoding="utf-8") as fh:
            fh.write("\n三句话结论：比价最好。\n")
        self.st.retro(week)  # re-run
        text = open(r["retro"], encoding="utf-8").read()
        self.assertIn("三句话结论：比价最好", text)
        self.assertEqual(text.count(core.RETRO_START), 1)
        self.assertEqual(self.st.quota_file(nxt)["status"], "confirmed")  # re-run never overwrites a confirmed quota

    def test_markets_seeded_editable_and_in_brief(self):
        ms = {m["code"]: m for m in self.st.markets()}
        self.assertEqual(list(ms), ["GLOBAL", "JP", "KR", "TW", "RU"])  # CN is run outside BeatSocial
        self.assertEqual([c["id"] for c in ms["GLOBAL"]["channels"]], ["X-EN"])
        self.st.upsert_market("GLOBAL", {"focus": "只推模型 API", "audience": "开发者", "cta": "模型页"})
        self.assertEqual(self.st.markets()[0]["focus"], "只推模型 API")
        core.db.migrate(self.st.conn)  # re-running the seed never overwrites an edit
        self.assertEqual(self.st.markets()[0]["focus"], "只推模型 API")
        next_week = core.iso_week(dt.date.today() + dt.timedelta(days=7))
        self.st.plan_week(next_week)
        brief = open(self.st.brief(next_week, "X-EN")[0], encoding="utf-8").read()
        self.assertIn("只推模型 API", brief)

    def test_make_media_uses_codex_and_attaches(self):
        fake = os.path.join(self.dir, "fake-codex.sh")
        with open(fake, "w") as fh:  # stands in for Codex: writes the PNG to the path given in the prompt
            fh.write("#!/bin/sh\npython3 - \"$@\" <<'PY'\nimport re,sys\nm=re.search(r'保存为（必须是这个绝对路径，PNG）：(\\S+)', sys.argv[-1])\nopen(m.group(1),'wb').write(b'png')\nPY\n")
        os.chmod(fake, 0o755)
        pid = self.st.create_post("X-EN", f"{TOMORROW} 21:30")
        self.st.submit_version(pid, "Nano Banana 2.1 $0.012", "", "参考", "", "Claude",
                               media=[{"slot": 1, "kind": "image", "spec": "price card"}])
        os.environ["BEATSOCIAL_CODEX"] = fake
        try:
            r = self.st.make_media(pid)
        finally:
            del os.environ["BEATSOCIAL_CODEX"]
        p = self.st.post(pid)
        self.assertTrue(os.path.isfile(r["file"]))
        self.assertEqual(p["media"][0]["file"], f"{p['week']}/{pid}-1.png")
        self.assertNotIn("media_missing", [f["code"] for f in p["flags"]])
        os.environ["BEATSOCIAL_CODEX"] = "/bin/true"  # exits 0 but makes no file
        try:
            with self.assertRaises(core.BeatSocialError):
                self.st.make_media(pid, slot=2)
        finally:
            del os.environ["BEATSOCIAL_CODEX"]

    def test_own_model_prices_not_flagged_as_competitor(self):
        from beatsocial import lint
        codes = [f["code"] for f in lint.check("Kling V3 · $0.88 on BeatAPI", "", "threads", "en", "x", [])]
        self.assertNotIn("competitor_price", codes)
        codes = [f["code"] for f in lint.check("fal: $2.43 vs BeatAPI $1.20", "", "x", "en", "x", [])]
        self.assertIn("competitor_price", codes)

    def test_multiple_media_slots_and_platform_caps(self):
        from beatsocial import lint
        imgs = [{"slot": i, "kind": "image", "file": None, "spec": "x"} for i in range(1, 6)]
        codes = [f["code"] for f in lint.check("t", "", "x", "en", "z", imgs)]
        self.assertIn("too_many_media", codes)
        codes = [f["code"] for f in lint.check("t", "", "threads", "en", "z", imgs)]
        self.assertNotIn("too_many_media", codes)
        mixed = [{"slot": 1, "kind": "image", "file": None}, {"slot": 2, "kind": "video", "file": None}]
        self.assertIn("x_video_mix", [f["code"] for f in lint.check("t", "", "x", "en", "z", mixed)])
        for name in ("a.png", "b.png", "c.png"):
            open(os.path.join(self.dir, "media", name), "wb").write(name.encode())
        pid = self.st.create_post("X-EN", f"{TOMORROW} 21:30")
        self.st.submit_version(pid, "three images", "", "参考", "", "Claude",
                               media=[{"slot": i + 1, "file": n} for i, n in enumerate(("a.png", "b.png", "c.png"))])
        p = self.st.post(pid)
        self.assertEqual([m["file"] for m in p["media"]], ["a.png", "b.png", "c.png"])
        self.st.approve(pid)
        self.assertEqual(len(self.st.post(pid)["media"]), 3)

    def test_sample_url_must_be_http(self):
        with self.assertRaises(core.BeatSocialError):
            self.st.add_sample({"url": "javascript:alert(1)", "competitor": "x"})


class ServerOriginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="beatsocial-srv-")
        db.create(cls.dir).close()
        st = core.Store(cls.dir)
        st.upsert_account({"id": "X-EN", "name": "X", "platform": "x", "country": "GLOBAL", "lang": "en"})
        cls.pid = st.create_post("X-EN", f"{TOMORROW} 21:30")
        st.submit_version(cls.pid, "body", "", "参考", "", "Claude")
        st.conn.close()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.dir, writable=True))
        cls.ro = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(cls.dir))
        threading.Thread(target=cls.ro.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.ro.shutdown()
        shutil.rmtree(cls.dir)

    def post(self, path, body, origin=None, ctype="application/json"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{path}", data=json.dumps(body).encode(),
                                     method="POST")
        req.add_header("Content-Type", ctype)
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.post("post/note", {"id": self.pid, "text": "x"}, origin="http://localhost.evil.com"), 403)
        self.assertEqual(self.post("post/note", {"id": self.pid, "text": "x"},
                                   origin=f"http://127.0.0.1:{self.port}", ctype="text/plain"), 403)
        self.assertEqual(self.post("post/note", {"id": self.pid, "text": "x"}, origin=f"http://127.0.0.1:{self.port}"), 200)

    def test_board_is_read_only_by_default(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.ro.server_address[1]}/api/post/note",
                                     data=json.dumps({"id": self.pid, "text": "x"}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 403)

    def test_approve_needs_hash(self):
        self.assertEqual(self.post("post/approve", {"ids": [self.pid]}, origin=f"http://127.0.0.1:{self.port}"), 400)

    def test_media_outside_root_not_served(self):
        url = f"http://127.0.0.1:{self.port}/media?path=" + urllib.request.quote(os.path.join(self.dir, "beatsocial.db"))
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url)
        self.assertEqual(e.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
