"""[INPUT] Isolated temporary data, JSON-RPC frames and concurrent conversation edits.
[OUTPUT] MCP transport and business regression checks without touching the default data directory.
[POS] Additive compatibility suite alongside the original 30 tests.
[PROTOCOL] All fixtures are disposable; no real approval, publication or global config changes.
"""
import concurrent.futures
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from beatsocial import core, db
from beatsocial.mcp_server import Server, serve, MAX_LINE
from beatsocial.mcp_tools import Operations, TOOLS

ROOT = Path(__file__).resolve().parents[1]


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="beatsocial-mcp-test-")
        self.data = self.tmp.name
        db.create(self.data).close()
        self.st = core.Store(self.data)
        self.st.upsert_account({"id": "X-TEST", "name": "临时测试", "platform": "x", "country": "GLOBAL",
                                "lang": "en", "handle": "@test", "bot": "TestBot"})
        self.ops = Operations(self.data, "MCP:test-client")
        self.pid = self.st.create_post("X-TEST", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.st.submit_version(self.pid, "Original body", "First reply", "中文参考", "source note", "fixture")

    def tearDown(self):
        self.st.conn.close()
        self.tmp.cleanup()

    def get(self, pid=None):
        return self.ops.call("get_post", {"id": pid or self.pid})

    def approve(self, posts=None, **kwargs):
        p = self.get()
        return self.ops.call("approve_posts", {"posts": posts or [{"id": p["id"], "expected_revision": p["revision"]}],
                            "owner_said": "临时测试这条可以发", "review_override": "临时测试主人明确人工审稿通过", **kwargs})

    def test_edit_only_supplied_fields_and_shared_cli_data(self):
        p = self.get()
        result = self.ops.call("edit_post", {"id": self.pid, "expected_revision": p["revision"], "changes": {"text": "New body\n中文 $ `literal`"}})
        self.assertEqual(result["reply_thread"], "First reply")
        self.assertEqual(result["zh_ref"], "中文参考")
        self.assertEqual(result["current_v"], 2)
        cli = subprocess.check_output([str(ROOT / "bin/beatsocial"), "--data", self.data, "--json", "post", "show", self.pid], text=True)
        self.assertEqual(json.loads(cli)["text"], result["text"])
        self.assertEqual(result["events"][0]["actor"], "MCP:test-client")

    def test_stale_edit_rejected(self):
        p = self.get()
        self.st.submit_version(self.pid, "Changed by CLI")
        with self.assertRaises(core.BeatSocialError):
            self.ops.call("edit_post", {"id": self.pid, "expected_revision": p["revision"], "changes": {"text": "overwrite"}})
        self.assertEqual(self.get()["text"], "Changed by CLI")

    def test_empty_patch_unknown_field_wrong_type_rejected(self):
        p = self.get()
        for patch in ({}, {"status": "approved"}, {"text": None}):
            with self.assertRaises(core.BeatSocialError):
                self.ops.call("edit_post", {"id": self.pid, "expected_revision": p["revision"], "changes": patch})
        with self.assertRaises(core.BeatSocialError):
            self.ops.call("list_posts", {"limit": True})

    def test_concurrent_edits_only_one_wins(self):
        rev = self.get()["revision"]
        def edit(text):
            try:
                self.ops.call("edit_post", {"id": self.pid, "expected_revision": rev, "changes": {"text": text}})
                return True
            except core.BeatSocialError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(edit, ["first", "second"])), 1)
        self.assertEqual(self.get()["current_v"], 2)

    def test_approval_requires_owner_words_revision_and_review(self):
        p = self.get()
        for args in (
            {"posts": [{"id": self.pid}], "owner_said": "yes"},
            {"posts": [{"id": self.pid, "expected_revision": p["revision"]}], "owner_said": "  "},
            {"posts": [{"id": self.pid, "expected_revision": p["revision"]}], "owner_said": "yes"},
        ):
            with self.assertRaises(core.BeatSocialError):
                self.ops.call("approve_posts", args)
        self.assertEqual(self.get()["status"], "review")
        self.approve()
        self.assertEqual(self.get()["status"], "approved")
        event = next(e for e in self.get()["events"] if e["action"] == "mcp_approval")
        self.assertEqual(json.loads(event["detail"])["owner_said"], "临时测试这条可以发")

    def test_batch_approval_is_atomic(self):
        p = self.get()
        p2 = self.st.create_post("X-TEST", dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.st.submit_version(p2, "[待补：资料]")
        with self.assertRaises(core.BeatSocialError):
            self.approve(posts=[{"id": self.pid, "expected_revision": p["revision"]},
                                {"id": p2, "expected_revision": self.get(p2)["revision"]}])
        self.assertEqual(self.get()["status"], "review")
        self.assertFalse(any(e["action"] == "approve" for e in self.get()["events"]))

    def test_price_confirmation_still_required(self):
        self.st.submit_version(self.pid, "Our model costs $1", zh_ref="测试价格")
        with self.assertRaises(core.BeatSocialError):
            self.approve()
        self.approve(price_checked=True)
        self.assertIsNotNone(self.get()["price_checked_at"])

    def test_revision_covers_target_schedule_and_reference(self):
        for mutate in (
            lambda: self.st.upsert_account({"id": "X-TEST", "handle": "@different"}),
            lambda: self.st.reschedule(self.pid, "2030-01-02 03:04"),
            lambda: self.st.submit_version(self.pid, "Original body", "First reply", "新中文参考"),
        ):
            before = self.get()["revision"]
            mutate()
            self.assertNotEqual(before, self.get()["revision"])

    def test_media_bytes_detected_even_if_stat_preserved(self):
        path = Path(self.data) / "media" / "image.png"
        path.write_bytes(b"AAAA")
        self.st.set_media(self.pid, 1, file="image.png")
        before = self.get()["revision"]
        stat = path.stat()
        path.write_bytes(b"BBBB")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertNotEqual(before, self.get()["revision"])

    def test_media_change_invalidates_ai_review(self):
        path = Path(self.data) / "media" / "image.png"
        path.write_bytes(b"AAAA")
        result = Path(self.data) / "review.json"
        result.write_text(json.dumps({"results": [{"id": self.pid, "v": 1, "result": "pass"}]}))
        self.st.import_ai_review(str(result), "Independent reviewer")
        p = self.get()
        changed = self.ops.call("set_media", {"id": self.pid, "expected_revision": p["revision"], "file": "image.png"})
        self.assertIsNone(changed["ai_review"])
        self.assertEqual(changed["current_v"], 2)

    def test_claimed_post_cannot_edit_cancel_move_or_reject(self):
        self.approve()
        self.st.claim(self.pid, "TestBot", self.get()["content_hash"])
        p = self.get()
        for name, extra in (("edit_post", {"changes": {"text": "changed"}}), ("cancel_post", {}),
                            ("reject_post", {}), ("reschedule_post", {"slot_at": "2030-01-01"}),
                            ("set_media", {"remove": True})):
            with self.assertRaises(core.BeatSocialError):
                self.ops.call(name, {"id": self.pid, "expected_revision": p["revision"], **extra})
        self.st.receipt(self.pid, "https://example.com/test-post", by="TestBot")
        self.assertEqual(self.get()["status"], "published")

    def test_account_partial_update_revision_and_identity(self):
        self.approve()
        a = self.ops.call("list_accounts", {})["items"][0]
        changed = self.ops.call("update_account", {"id": a["id"], "expected_revision": a["revision"], "changes": {"handle": "@new"}})
        self.assertEqual(changed["bot"], "TestBot")
        self.assertEqual(self.get()["status"], "review")
        with self.assertRaises(core.BeatSocialError):
            self.ops.call("update_account", {"id": a["id"], "expected_revision": a["revision"], "changes": {"status": "paused"}})

    def test_claimed_account_target_update_rejected(self):
        self.approve()
        self.st.claim(self.pid, "TestBot", self.get()["content_hash"])
        a = self.ops.call("list_accounts", {})["items"][0]
        with self.assertRaises(core.BeatSocialError):
            self.ops.call("update_account", {"id": a["id"], "expected_revision": a["revision"], "changes": {"handle": "@new"}})
        self.assertEqual(self.st.account(a["id"])["handle"], "@test")

    def test_market_preserves_unspecified_fields(self):
        m = self.ops.call("list_markets", {})["items"][0]
        updated = self.ops.call("update_market", {"code": m["code"], "expected_revision": m["revision"], "changes": {"focus": "测试方向"}})
        self.assertEqual(updated["audience"], m["audience"])
        self.assertEqual(updated["focus"], "测试方向")

    def test_rules_and_quota_require_exact_revision(self):
        result = self.ops.call("propose_rule", {"text": "测试规则"})
        rule = self.ops.call("list_rules", {})["items"][0]
        self.ops.call("decide_rule", {"id": result["id"], "expected_revision": rule["revision"], "status": "active", "owner_said": "测试采纳"})
        self.assertIn("测试规则", (Path(self.data) / "memory/LESSONS.md").read_text())
        week = core.iso_week(dt.date.today())
        self.ops.call("generate_retrospective", {"week": week})
        nxt = core.shift_week(week, 1)
        quota = self.ops.call("get_quota", {"week": nxt})
        self.ops.call("confirm_quota", {"week": nxt, "expected_revision": quota["revision"], "owner_said": "测试确认配额"})
        self.assertEqual(self.st.quota_file(nxt)["status"], "confirmed")

    def test_read_only_and_pagination(self):
        ops = Operations(self.data, read_only=True)
        self.assertEqual(ops.call("list_posts", {"limit": 1})["total"], 1)
        with self.assertRaises(core.BeatSocialError):
            ops.call("create_post", {"account": "X-TEST", "slot_at": "2030-01-01"})
        self.assertEqual(self.ops.call("get_stats", {"period": "d7"})["items"][0]["with_metrics"], 0)


class ProtocolTest(unittest.TestCase):
    def init(self, server):
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
        self.assertIn("tools", response["result"]["capabilities"])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_lifecycle_and_unknown_method(self):
        s = Server("/unused")
        request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        self.assertIn("error", s.handle(request))
        self.init(s)
        self.assertEqual(len(s.handle(request)["result"]["tools"]), len(TOOLS))
        self.assertEqual(s.handle({**request, "method": "unknown"})["error"]["code"], -32601)
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}))

    def test_read_only_catalog_and_no_publishing_tools(self):
        s = Server("/unused", read_only=True)
        self.init(s)
        listed = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in listed))
        self.assertNotIn("claim", TOOLS)
        self.assertNotIn("receipt", TOOLS)
        self.assertNotIn("publish", TOOLS)
        r = s.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "create_post", "arguments": {"account": "x", "slot_at": "2030-01-01"}}})
        self.assertTrue(r["result"]["isError"])

    def test_invalid_json_and_oversize_do_not_kill_stream(self):
        source = io.BytesIO(b'bad json\n' + b'x' * (MAX_LINE + 10) + b'\n' + b'{"jsonrpc":"2.0","id":4,"method":"ping"}\n')
        dest = io.StringIO()
        serve("/unused", input_stream=source, output_stream=dest)
        messages = [json.loads(line) for line in dest.getvalue().splitlines()]
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0]["error"]["code"], -32700)
        self.assertEqual(messages[2]["result"], {})

    def test_real_stdio_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="beatsocial-stdio-") as data:
            db.create(data).close()
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test-client", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_markets", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "edit_post", "arguments": {"id": "missing"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            ]
            r = subprocess.run([str(ROOT / "bin/beatsocial"), "--data", data, "mcp"],
                               input="".join(json.dumps(m) + "\n" for m in messages), capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            replies = [json.loads(line) for line in r.stdout.splitlines()]
            self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])
            markets = json.loads(replies[2]["result"]["content"][0]["text"])
            self.assertEqual(len(markets["items"]), 5)  # CN is run outside BeatSocial
            self.assertTrue(replies[3]["result"]["isError"])
            self.assertEqual(replies[4]["result"], {})


if __name__ == "__main__":
    unittest.main()
