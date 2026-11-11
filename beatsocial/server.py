"""[INPUT] A data directory; HTTP requests from the local browser.
[OUTPUT] The read-only board (web/) plus a JSON API over core.Store and media file serving; writes are off unless BEATSOCIAL_WEB_WRITES=1.
[POS] Local-only UI layer. Binds to 127.0.0.1 by default; every write goes through core.Store.
[PROTOCOL] JSON in, JSON out. Errors return {"error": message} with status 400.
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core
from .model import OWNER, ACCOUNT_STATUSES, COUNTRIES, MARKET_ORDER, METRIC_LABELS, PLATFORMS, RULE_STATUSES, STATUSES

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
ACTOR = os.environ.get("BEATSOCIAL_USER", OWNER)


def make_handler(data_dir, writable=False):
    """writable=False (default): the web page is a read-only board; every change goes through Claude Code + CLI."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _local_host_ok(self):
            """Reject DNS-rebinding: the Host header must be this machine."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def _origin_ok(self):
            """Writes must come from this desk's own page: exact origin match, JSON body."""
            port = self.server.server_address[1]
            allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            origin = self.headers.get("Origin")
            if origin is None:  # non-browser clients (curl, scripts) send no Origin; browsers always do on POST
                referer = self.headers.get("Referer")
                if referer and urllib.parse.urlparse(referer)._replace(path="", params="", query="", fragment="").geturl() not in allowed:
                    return False
            elif origin not in allowed:
                return False
            return (self.headers.get("Content-Type") or "").split(";")[0].strip() == "application/json"

        def do_GET(self):
            if not self._local_host_ok():
                return self._send(403, {"error": "只接受本机访问"})
            url = urllib.parse.urlparse(self.path)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                if url.path.startswith("/api/"):
                    return self._send(200, self.api_get(url.path[5:], qs))
                if url.path == "/media":
                    return self.media(qs.get("path", ""))
                return self.static(url.path)
            except core.BeatSocialError as e:
                return self._send(400, {"error": str(e)})

        def do_POST(self):
            if not writable:
                return self._send(403, {"error": "看板是只读的：要改什么，在 Claude Code 里说"})
            url = urllib.parse.urlparse(self.path)
            if not self._local_host_ok() or not self._origin_ok():
                return self._send(403, {"error": "只接受审核台页面自己发出的请求"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                return self._send(200, self.api_post(url.path[5:], body))
            except core.BeatSocialError as e:
                return self._send(400, {"error": str(e)})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": f"请求格式不对：{e}"})
            except Exception as e:  # sqlite busy / integrity: report instead of dropping the connection
                return self._send(500, {"error": f"服务器出错，请重试：{e}"})

        def store(self):
            return core.Store(data_dir)

        def api_get(self, path, qs):
            st = self.store()
            if path == "meta":
                return {"statuses": STATUSES, "platforms": PLATFORMS, "countries": COUNTRIES,
                        "account_statuses": ACCOUNT_STATUSES, "rule_statuses": RULE_STATUSES,
                        "metric_labels": METRIC_LABELS, "market_order": MARKET_ORDER, "weeks": st.weeks(), "user": ACTOR,
                        "accounts": [{k: a[k] for k in ("id", "name", "platform", "country", "status", "bot", "handle", "lang")}
                                     for a in st.accounts()],
                        "data_dir": st.data_dir, "this_week": core.iso_week(core.dt.date.today())}
            if path == "overview":
                return st.overview()
            if path == "posts":
                return st.posts(qs.get("week"), qs.get("account"), qs.get("status"), qs.get("country"),
                                qs.get("platform"), qs.get("since"), qs.get("until"))
            if path == "post":
                return st.post(qs["id"])
            if path == "accounts":
                return st.accounts()
            if path == "markets":
                return st.markets()
            if path == "stats":
                return st.stats(qs.get("by", "account"), qs.get("week"), qs.get("since"), qs.get("until"),
                                qs.get("period") or None)
            if path == "retro":
                return st.retro_text(qs.get("week"))
            if path == "metrics_due":
                return st.metrics_due()
            if path == "competitors":
                return st.competitors()
            if path == "samples":
                return st.samples(qs.get("platform"), qs.get("country"), qs.get("competitor"), qs.get("type"),
                                  int(qs.get("limit", 300)))
            if path == "quota":
                return {"file": st.quota_file(qs["week"]), "in_force": st._quota(qs["week"])}
            if path == "rules":
                return st.rules(qs.get("status"))
            raise core.BeatSocialError(f"没有这个接口：{path}")

        def api_post(self, path, b):
            st = self.store()
            if path == "post/edit":
                v = st.submit_version(b["id"], b.get("text", ""), b.get("reply_thread", ""), b.get("zh_ref", ""),
                                      b.get("writer_note", ""), ACTOR)
                return {"v": v, "post": st.post(b["id"])}
            if path == "post/approve":
                hashes = b.get("hashes") or {}
                missing = [i for i in b["ids"] if not hashes.get(i)]
                if missing:  # the page must say which version the person saw
                    raise core.BeatSocialError("批准请求缺少内容版本，请刷新页面后再批")
                return st.approve_many(b["ids"], ACTOR, bool(b.get("price_checked")), hashes)
            if path == "post/reject":
                st.reject(b["id"], ACTOR, b.get("note", ""))
                return {"ok": True}
            if path == "post/note":
                st.add_note(b["id"], ACTOR, b["text"])
                return {"ok": True}
            if path == "post/move":
                st.reschedule(b["id"], b["slot_at"], ACTOR)
                return {"ok": True}
            if path == "post/cancel":
                st.cancel(b["id"], ACTOR, b.get("note", ""))
                return {"ok": True}
            if path == "post/new":
                return {"id": st.create_post(b["account"], b["slot_at"], b.get("type"), b.get("topic"), actor=ACTOR)}
            if path == "media":
                st.set_media(b["id"], int(b.get("slot") or 1), b.get("file"), b.get("kind"), b.get("spec"), ACTOR,
                             bool(b.get("remove")))
                return st.post(b["id"])
            if path == "account":
                return st.upsert_account(b, ACTOR)
            if path == "market":
                return st.upsert_market(b["code"], b, ACTOR)
            if path == "rule/decide":
                st.decide_rule(int(b["id"]), b["status"], ACTOR, b.get("text"))
                return {"ok": True}
            if path == "rule/propose":
                return {"id": st.propose_rule(b["text"], b.get("scope", "global"), b.get("evidence", ""), ACTOR)}
            if path == "plan":
                return {"created": st.plan_week(b["week"], b.get("accounts"), ACTOR)}
            if path == "quota/confirm":
                return st.confirm_quota(b["week"], b.get("accounts"), ACTOR)
            if path == "retro":
                return st.retro(b["week"])
            if path == "backup":
                return st.backup()
            raise core.BeatSocialError(f"没有这个接口：{path}")

        def media(self, path):
            st = self.store()
            full = os.path.realpath(st.media_path(path))
            inside = full.startswith(os.path.realpath(st.paths["media"]) + os.sep)
            if not inside or not os.path.isfile(full):
                return self._send(404, {"error": "没有这个文件"})
            size = os.path.getsize(full)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            try:
                fh = open(full, "rb")
            except OSError as e:
                return self._send(403, {"error": f"读不到文件（权限）：{e.strerror}"})
            with fh:
                if rng and rng.startswith("bytes="):
                    start_s, _, end_s = rng[6:].partition("-")
                    start = int(start_s or 0)
                    end = min(int(end_s) if end_s else size - 1, size - 1)
                    fh.seek(start)
                    data = fh.read(end - start + 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    data = fh.read()
                    self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.realpath(os.path.join(WEB, name))
            if not full.startswith(os.path.realpath(WEB)) or not os.path.isfile(full):
                return self._send(404, "not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "text/plain"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, open(full, "rb").read(), ctype)

    return Handler


def serve(data_dir, host="127.0.0.1", port=9361):
    core.Store(data_dir)  # fail fast if the data dir is wrong
    httpd = ThreadingHTTPServer((host, port), make_handler(data_dir, writable=os.environ.get("BEATSOCIAL_WEB_WRITES") == "1"))
    print(f"BeatSocial 审核台：http://{host}:{port}/  （数据：{data_dir}）  Ctrl+C 退出")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
