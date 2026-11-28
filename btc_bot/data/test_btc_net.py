"""The real urllib code paths (Twelve Data and Telegram) exercised against local fake HTTP servers."""
import contextlib
import io
import json
import os
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import turtle_btc

GOOD = {"meta": {"symbol": "BTC/USD"}, "status": "ok", "values": [
    {"datetime": "2026-01-05 10:30:00", "open": "101.5", "high": "103", "low": "100", "close": "102.5", "volume": "0"},
    {"datetime": "2026-01-05 10:00:00", "open": "100", "high": "102", "low": "99", "close": "101.5", "volume": "0"}]}


class Fake:
    """A tiny scripted HTTP server: script is a list of (status, json) answers used in order (the last repeats)."""

    def __init__(self, script):
        self.script, self.seen = list(script), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                outer.seen.append({"path": self.path, "body": body})
                status, payload = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _answer

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Net(unittest.TestCase):
    def fake(self, script):
        f = Fake(script)
        self.addCleanup(f.close)
        return f

    def env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    # ---- Twelve Data
    def test_candles_are_parsed_sorted_and_the_request_is_what_the_provider_expects(self):
        f = self.fake([(200, GOOD)])
        self.env(TWELVE_DATA_BASE=f.url)
        bars = turtle_btc.fetch_bars("KEY", "BTC/USD", 30, outputsize=5000)
        self.assertEqual([b.c for b in bars], [101.5, 102.5])              # oldest first
        q = urllib.parse.parse_qs(urllib.parse.urlparse(f.seen[0]["path"]).query)
        self.assertEqual(q["symbol"], ["BTC/USD"])
        self.assertEqual(q["interval"], ["30min"])
        self.assertEqual(q["outputsize"], ["5000"])
        self.assertEqual(q["timezone"], ["UTC"])
        self.assertEqual(q["apikey"], ["KEY"])
        self.assertNotIn("end_date", q)
        turtle_btc.fetch_bars("KEY", "BTC/USD", 30, end_date="2026-01-01 00:00:00")
        self.assertIn("end_date", f.seen[1]["path"])

    def test_the_forming_candle_is_dropped(self):
        f = self.fake([(200, GOOD)])
        self.env(TWELVE_DATA_BASE=f.url)
        t_open = 1767609000                                                 # 2026-01-05 10:30:00 UTC
        bars = turtle_btc.fetch_bars("K", "BTC/USD", 30, now=t_open + 600)  # 10:30 candle still forming
        self.assertEqual([b.c for b in bars], [101.5])

    def test_rate_limit_in_http_status_or_in_the_body_is_retried_once(self):
        for first in ((429, {"code": 429, "status": "error", "message": "limit"}),
                      (200, {"code": 429, "status": "error", "message": "limit"})):
            f = self.fake([first, (200, GOOD)])
            self.env(TWELVE_DATA_BASE=f.url)
            with mock.patch.object(turtle_btc._time, "sleep") as sl:
                bars = turtle_btc.fetch_bars("K", "BTC/USD", 30)
            self.assertEqual(len(bars), 2)
            self.assertEqual(len(f.seen), 2)
            sl.assert_called_once_with(20)

    def test_a_provider_error_is_reported_in_its_own_words(self):
        f = self.fake([(200, {"code": 400, "status": "error", "message": "**symbol** not found"})])
        self.env(TWELVE_DATA_BASE=f.url)
        with self.assertRaisesRegex(RuntimeError, r"symbol\*\* not found"):
            turtle_btc.fetch_bars("K", "BTC/USD", 30)
        f2 = self.fake([(401, {"code": 401, "status": "error", "message": "apikey is invalid"})])
        self.env(TWELVE_DATA_BASE=f2.url)
        with self.assertRaisesRegex(RuntimeError, "apikey is invalid"):
            turtle_btc.fetch_bars("K", "BTC/USD", 30)

    def test_two_rate_limits_in_a_row_give_up_with_a_clear_error(self):
        f = self.fake([(429, {"code": 429, "status": "error", "message": "limit"})])
        self.env(TWELVE_DATA_BASE=f.url)
        with mock.patch.object(turtle_btc._time, "sleep"), self.assertRaisesRegex(RuntimeError, "limit"):
            turtle_btc.fetch_bars("K", "BTC/USD", 30)
        self.assertEqual(len(f.seen), 2)

    # ---- Telegram
    def test_a_message_is_posted_to_the_right_bot_and_chat(self):
        f = self.fake([(200, {"ok": True})])
        self.env(TELEGRAM_API_BASE=f.url)
        self.assertTrue(turtle_btc.send_telegram("TOK123", "42", "hello"))
        self.assertEqual(f.seen[0]["path"], "/botTOK123/sendMessage")
        body = urllib.parse.parse_qs(f.seen[0]["body"])
        self.assertEqual((body["chat_id"], body["text"]), (["42"], ["hello"]))

    def test_long_reports_are_split_below_the_4096_limit_without_losing_text(self):
        f = self.fake([(200, {"ok": True})])
        self.env(TELEGRAM_API_BASE=f.url)
        text = "\n\n".join(f"paragraph {i} " + "x" * 500 for i in range(30))   # ~15,000 characters
        self.assertTrue(turtle_btc.send_telegram("T", "1", text))
        parts = [urllib.parse.parse_qs(s["body"])["text"][0] for s in f.seen]
        self.assertGreater(len(parts), 3)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual("\n\n".join(parts), text)

    def test_a_refusal_is_explained_and_never_leaks_the_token(self):
        f = self.fake([(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})])
        self.env(TELEGRAM_API_BASE=f.url)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(turtle_btc.send_telegram("SECRETTOKEN", "1", "hi"))
        self.assertIn("chat not found", err.getvalue())
        self.assertNotIn("SECRETTOKEN", err.getvalue())
        self.env(TELEGRAM_API_BASE="http://127.0.0.1:9")                    # nothing listens: connection refused
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(turtle_btc.send_telegram("SECRETTOKEN", "1", "hi"))
        self.assertNotIn("SECRETTOKEN", err.getvalue())

    # ---- ping
    def test_ping_reports_each_connection_separately(self):
        td, tg = self.fake([(200, GOOD)]), self.fake([(200, {"ok": True})])
        self.env(TWELVE_DATA_BASE=td.url, TELEGRAM_API_BASE=tg.url)
        ok, lines = turtle_btc.ping("K", "BTC/USD", 30, "T", "1")
        self.assertTrue(ok)
        self.assertIn("Twelve Data: OK", lines[0])
        self.assertIn("Telegram: OK", lines[1])
        ok, lines = turtle_btc.ping(None, "BTC/USD", 30, "T", "1")          # missing key, Telegram still checked
        self.assertFalse(ok)
        self.assertIn("TWELVE_DATA_API_KEY is not set", lines[0])
        self.assertIn("Telegram: OK", lines[1])
        bad = self.fake([(400, {"ok": False, "description": "Unauthorized"})])
        self.env(TELEGRAM_API_BASE=bad.url)
        with contextlib.redirect_stderr(io.StringIO()):
            ok, lines = turtle_btc.ping("K", "BTC/USD", 30, "T", "1")
        self.assertFalse(ok)
        self.assertIn("Telegram: FAILED", lines[1])

    def test_ping_from_the_command_line_sets_the_exit_code(self):
        td, tg = self.fake([(200, GOOD)]), self.fake([(200, {"ok": True})])
        self.env(TWELVE_DATA_BASE=td.url, TELEGRAM_API_BASE=tg.url, TWELVE_DATA_API_KEY="K",
                 TELEGRAM_BOT_TOKEN="T", TELEGRAM_CHAT_ID="1")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(turtle_btc.main(["--ping"]), 0)
        self.assertIn("Twelve Data: OK", out.getvalue())
        self.env(TELEGRAM_BOT_TOKEN="")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(turtle_btc.main(["--ping"]), 1)


if __name__ == "__main__":
    unittest.main()
