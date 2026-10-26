"""
test_chat_isolation.py — who is allowed to talk to the bot (independent review, 25 Oct).
Covers BOTH gates: the transport (scanner_live.check_result_commands, which writes the inbox) and the runner (bus_runner.run, which answers it).
Fail closed: a message whose chat cannot be established is dropped, never answered, never recorded.
LIMIT (recorded, not hidden): the system keeps ONE ledger and ONE conversation context. It is built for a single owner; there is no per-chat
separation of notes, trades, alerts or context. These tests prove strangers are shut out, not that two users could share it safely.
"""
import json, os, sys, tempfile, unittest
from unittest import mock
from datetime import datetime, timezone
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE); sys.path.insert(0, os.path.dirname(HERE))
import test_converse as tc

NOW = datetime(2026, 10, 20, 16, 5, tzinfo=timezone.utc)
Q = "when was the last choch"


class RunnerGate(tc.Flow):
    def put(self, rows):
        with open(os.path.join(self.root, "inbox.jsonl"), "a") as f:
            for i, r in enumerate(rows):
                f.write(json.dumps({"update_id": 500 + i, "text": Q, "received_at": NOW.isoformat(), **r}) + "\n")

    def test_the_owner_is_answered(self):
        self.put([{"chat_id": "1"}]); self.run_(now=NOW); self.assertEqual(len(self.sent), 1)

    def test_owner_id_as_a_number_is_still_the_owner(self):
        self.put([{"chat_id": 1}]); self.run_(now=NOW); self.assertEqual(len(self.sent), 1)

    def test_strangers_and_unreadable_identities_get_nothing_and_leave_no_trace(self):
        bad = [{"chat_id": "999"}, {"chat_id": 999}, {"chat_id": ""}, {"chat_id": None}, {"chat_id": " 1"}, {"chat_id": "01"}, {"chat_id": "1; 2"}, {"chat_id": [1]}, {"chat_id": {"id": 1}}, {}]
        self.put(bad); self.run_(now=NOW)
        self.assertEqual(self.sent, [])
        self.assertEqual([r for r in self.records() if r["event_type"] in ("MESSAGE_RECEIVED", "BUS_REPLY", "KNOWLEDGE_STATED")], [])

    def test_a_stranger_cannot_reach_the_owners_context_or_notes(self):
        self.talk("I noticed price tends to reverse after the london open", at=NOW, now=NOW)
        self.talk("yes", at=NOW, now=NOW)                                   # owner saves a note
        before = len(self.records())
        self.put([{"chat_id": "999", "text": t} for t in ("what did I note", "show my notes", "yes", "/ask what notes do I have", "/reconstruct", "alert me at the next choch", "save it")])
        self.run_(now=NOW)
        self.assertEqual(len(self.sent), 2 if len(self.sent) >= 2 else len(self.sent))
        self.assertEqual(len(self.records()), before)

    def test_no_owner_configured_means_nobody_is_answered(self):
        old = os.environ.get("TELEGRAM_CHAT_ID")
        try:
            os.environ["TELEGRAM_CHAT_ID"] = ""
            self.put([{"chat_id": ""}, {"chat_id": "1"}]); n = len(self.sent); self.run_(now=NOW)
            self.assertEqual(len(self.sent), n)
        finally:
            os.environ["TELEGRAM_CHAT_ID"] = old or "1"


class TransportGate(unittest.TestCase):
    """The poller itself, with Telegram mocked."""
    def setUp(self):
        import scanner_live as sl
        self.sl = sl
        self.tmp = tempfile.mkdtemp()
        self.inbox = os.path.join(self.tmp, "inbox.jsonl")

    def feed(self, updates, owner="1", group=""):
        resp = mock.Mock(); resp.json.return_value = {"ok": True, "result": updates}
        with mock.patch.object(self.sl, "INBOX_FILE", self.inbox), mock.patch.object(self.sl.requests, "get", return_value=resp), \
             mock.patch.object(self.sl, "TELEGRAM_CHAT_ID", owner), mock.patch.object(self.sl, "GROUP_CHAT_ID", group), \
             mock.patch.object(self.sl, "send_telegram", lambda *a, **k: True):
            try:
                self.sl.check_result_commands({"last_update_id": 0}, None)
            except Exception as e:                                     # a crash is a finding too
                self.fail(f"poller crashed on {updates!r}: {e!r}")
        return [json.loads(l) for l in open(self.inbox)] if os.path.exists(self.inbox) else []

    def upd(self, uid, chat, text=Q):
        m = {"text": text}
        if chat is not ...:
            m["chat"] = {"id": chat} if chat is not None else None
        return {"update_id": uid, "message": m}

    def test_owner_reaches_the_inbox_stranger_does_not(self):
        rows = self.feed([self.upd(1, 1), self.upd(2, 999)])
        self.assertEqual([r["update_id"] for r in rows], [1])

    def test_missing_or_malformed_chat_is_dropped(self):
        rows = self.feed([self.upd(1, ...), self.upd(2, None), {"update_id": 3}, {"update_id": 4, "message": {"text": Q, "chat": {}}}, {"update_id": 5, "message": None},
                          {"update_id": 6, "edited_message": {"text": Q, "chat": {"id": 1}}}])
        self.assertEqual(rows, [])

    def test_empty_owner_setting_does_not_let_a_message_with_no_chat_through(self):
        rows = self.feed([self.upd(1, ...), {"update_id": 2, "message": {"text": Q, "chat": {}}}], owner="")
        self.assertEqual(rows, [])

    def test_group_chat_never_reaches_the_conversation(self):
        rows = self.feed([self.upd(1, -100777, "I think the sweep matters"), self.upd(2, -100777, "/watch")], group="-100777")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
