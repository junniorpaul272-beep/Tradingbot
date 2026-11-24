"""test_reports_routing.py - protects Stage B: the new plain-words report path in bus_runner.py is OFF by default, changes nothing when off,
and when on answers only the exact routed command(s), with the report text going out as Telegram HTML and nothing else written.
(The byte-for-byte proof that the whole command path is unchanged with the switch off is review/stage_b_equivalence.py, which needs the baseline tree.)"""
import json, os, shutil, subprocess, sys, tempfile, types, unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
for _k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "TWELVE_DATA_KEY"): os.environ.setdefault(_k, "dummy")
OWNER = "1000001"; os.environ["TELEGRAM_CHAT_ID"] = OWNER
import bus_runner as br
import guard

NOW = datetime(2026, 11, 20, 15, 56, tzinfo=timezone.utc)
DATA = ("state.json", "ohlc_5m_log.jsonl", "ohlc_15m_log.jsonl", "ohlc_1h_log.jsonl")


def make_root(state=None):
    root = tempfile.mkdtemp(prefix="rv2_")
    for f in DATA: shutil.copy(os.path.join(REPO, f), root)
    shutil.copytree(os.path.join(REPO, "reports"), os.path.join(root, "reports"), ignore=shutil.ignore_patterns("__pycache__"))
    if state is not None: json.dump(state, open(os.path.join(root, "state.json"), "w"))
    return root


def tree(root):
    import hashlib
    out = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f); out[os.path.relpath(p, root)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def ask(root, texts, now=NOW, flag=True, chat=OWNER):
    with open(os.path.join(root, "inbox.jsonl"), "w") as f:
        for i, t in enumerate(texts): f.write(json.dumps({"update_id": 500 + i, "chat_id": chat, "text": t, "received_at": (now - timedelta(minutes=6)).isoformat()}) + "\n")
    sent = []
    def send(text, html=False): sent.append((text, html)); return True
    old = br.REPORTS_V2; br.REPORTS_V2 = flag
    try: summary = br.run(root, now=now, send=send, signals=False)
    finally: br.REPORTS_V2 = old
    recs = [json.loads(l) for l in open(os.path.join(root, "user_ledger.jsonl")) if l.strip()] if os.path.exists(os.path.join(root, "user_ledger.jsonl")) else []
    return sent, summary, recs


def real_state(**over):
    s = json.load(open(os.path.join(REPO, "state.json"))); s.update(over); return s


class Routing(unittest.TestCase):
    def test_off_by_default(self):
        self.assertIs(br.REPORTS_V2, False)
        self.assertEqual(br.REPORTS_V2_COMMANDS, {"/now": "now"})

    def test_only_the_exact_command_matches(self):
        k = br._reports_v2_kind
        for t in ("/now", "/NOW", "/now@ValleyBot", "  /now  "): self.assertEqual(k(t), "now", t)
        for t in ("/now now", "/now 15m", "/nowhere", "/now?", "now", "what is happening", "/thesis", "/technical", "/quantitative", "", None, "/now\n/now"):
            self.assertIsNone(k(t), t)

    def test_off_means_the_report_package_is_never_imported(self):
        root = make_root()
        code = ("import sys, os, json\nsys.path.insert(0, %r)\nos.environ['TELEGRAM_CHAT_ID']=%r\nimport bus_runner as br\nfrom datetime import datetime, timedelta, timezone\n"
                "open(os.path.join(%r,'inbox.jsonl'),'w').write(json.dumps({'update_id':1,'chat_id':%r,'text':'/now','received_at':'2026-11-20T15:50:00+00:00'})+'\\n')\n"
                "sent=[]\nbr.run(%r, now=datetime(2026,11,20,15,56,tzinfo=timezone.utc), send=lambda t: sent.append(t) or True)\n"
                "print('IMPORTED' if 'reports' in sys.modules else 'NOT_IMPORTED', len(sent), sent[0].startswith('<b>'))\n") % (HERE, OWNER, root, OWNER, root)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "0"}).stdout.split()
        self.assertEqual(out[:3], ["NOT_IMPORTED", "1", "False"], out)
        shutil.rmtree(root)


class FlagOn(unittest.TestCase):
    def setUp(self): self.root = make_root()
    def tearDown(self): shutil.rmtree(self.root, ignore_errors=True)

    def test_now_is_answered_by_the_report_and_sent_as_html(self):
        before = tree(self.root)
        sent, summary, recs = ask(self.root, ["/now"])
        self.assertEqual(len(sent), 1); text, html = sent[0]
        self.assertTrue(html); self.assertTrue(text.startswith("<b>GBPUSD")); self.assertIn("Answered 16:56 Lagos", text)
        rep = [r for r in recs if r["event_type"] == br.BUS_REPLY][0]["payload"]
        self.assertEqual(rep["reports_v2"], {"kind": "now", "status": "ok"}); self.assertEqual(rep["kind"], "other")
        self.assertEqual(summary["answered"], 1); self.assertEqual(summary["errors"], 0)
        after = tree(self.root)
        self.assertEqual(sorted(k for k in after if before.get(k) != after[k]), ["inbox.jsonl", "user_ledger.jsonl"], "only the inbox it was given and the ledger may change")

    def test_the_old_bot_words_are_gone_from_the_reply(self):
        text = ask(self.root, ["/now"])[0][0][0]
        for w in ("engine", "leg ", "stretched", "higher timeframe", "thesis"): self.assertNotIn(w, text.lower(), w)

    def test_stale_bias_gives_the_plain_refusal(self):
        json.dump(real_state(macro_bias_stale=True), open(os.path.join(self.root, "state.json"), "w"))
        sent, _, recs = ask(self.root, ["/now"])
        self.assertIn("no confirmed 1H bias", sent[0][0])
        self.assertEqual([r for r in recs if r["event_type"] == br.BUS_REPLY][0]["payload"]["reports_v2"]["status"], "refused")

    def test_missing_files_never_raise(self):
        os.remove(os.path.join(self.root, "state.json")); os.remove(os.path.join(self.root, "ohlc_5m_log.jsonl"))
        sent, summary, _ = ask(self.root, ["/now"])
        self.assertEqual(summary["errors"], 0); self.assertEqual(len(sent), 1); self.assertNotIn("Traceback", sent[0][0])

    def test_market_closed_refuses(self):
        closed = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        json.dump(real_state(market_data_timestamp=closed.isoformat()), open(os.path.join(self.root, "state.json"), "w"))
        sent, _, _ = ask(self.root, ["/now"], now=closed)
        self.assertIn("market is closed", sent[0][0])

    def test_nothing_else_is_touched(self):
        for t in ("/now now", "/now 15m", "/nowhere", "/thesis", "/technical", "/quantitative", "/help", "now", "what's happening"):
            r = make_root()
            try:
                sent, _, recs = ask(r, [t])
                self.assertTrue(all(not h for _, h in sent), t)
                self.assertTrue(all("reports_v2" not in x["payload"] for x in recs if x["event_type"] == br.BUS_REPLY), t)
            finally: shutil.rmtree(r, ignore_errors=True)

    def test_a_message_from_someone_else_is_ignored(self):
        sent, summary, _ = ask(self.root, ["/now"], chat="999")
        self.assertEqual(sent, []); self.assertEqual(summary["answered"], 0)

    def test_every_report_text_passes_both_bus_gates_unchanged(self):
        import pandas as pd
        for ts in pd.date_range("2026-11-17T08:07", "2026-11-20T15:50", freq="97min", tz="UTC"):
            if ts.weekday() > 4 or (ts.weekday() == 4 and ts.hour >= 21): continue
            r = make_root()
            try:
                sent, _, recs = ask(r, ["/now"], now=ts.to_pydatetime())
                rep = [x for x in recs if x["event_type"] == br.BUS_REPLY][0]["payload"]
                self.assertNotIn("guard", rep, (ts, rep.get("guard")))
                self.assertEqual(guard.final(sent[0][0]).text, sent[0][0], ts)
            finally: shutil.rmtree(r, ignore_errors=True)


class Delivery(unittest.TestCase):
    def test_an_undelivered_report_is_resent_as_html_and_an_old_reply_as_plain_text(self):
        root = make_root(); import ledger
        path = os.path.join(root, "user_ledger.jsonl")
        ledger.append(path, br.BUS_REPLY, {"update_id": 1, "text": "<b>report</b>", "kind": "other", "used_fallback": False, "violations": [], "reports_v2": {"kind": "now", "status": "ok"}}, now=NOW)
        ledger.append(path, br.BUS_REPLY, {"update_id": 2, "text": "old style reply", "kind": "answer", "used_fallback": False, "violations": []}, now=NOW)
        sent, summary, _ = ask(root, [])
        self.assertEqual(sorted(sent), [("<b>report</b>", True), ("old style reply", False)]); self.assertEqual(summary["resent"], 2)
        shutil.rmtree(root)

    def _sender(self, codes):
        posts = []
        def post(url, json=None, timeout=None):
            posts.append(dict(json)); code = codes.pop(0)
            return types.SimpleNamespace(status_code=code, json=lambda: {"ok": code == 200})
        sys.modules["requests"] = types.SimpleNamespace(post=post)
        return br.telegram_sender("t", "c"), posts

    def test_plain_sends_are_exactly_as_before(self):
        send, posts = self._sender([200]); self.assertTrue(send("hello"))
        self.assertEqual(posts, [{"chat_id": "c", "text": "hello", "disable_web_page_preview": True}])

    def test_html_send_uses_parse_mode(self):
        send, posts = self._sender([200]); self.assertTrue(send("<b>x</b>", html=True))
        self.assertEqual(posts[0]["parse_mode"], "HTML")

    def test_if_telegram_cannot_read_the_formatting_the_same_words_go_out_plain(self):
        send, posts = self._sender([400, 200]); self.assertTrue(send("<b>1 &lt; 2</b>", html=True))
        self.assertEqual(len(posts), 2); self.assertNotIn("parse_mode", posts[1]); self.assertEqual(posts[1]["text"], "1 < 2")


if __name__ == "__main__":
    unittest.main(verbosity=1)
