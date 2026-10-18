"""Run: python test_converse.py — Step 2e: how you type (slang), when you mean (times), plain questions, clarification,
market/conversation reconstruction, and the glue voice. Unit tests first, then the runner end to end with a fake Telegram."""
import ast, json, os, re, sys, unittest
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE); sys.path.append(ROOT)   # scanner files, fixtures, docs live one level up
import informal, timeref, voice, converse, market_reconstruct as mr, conversation_search as cs
import bus, bus_runner as br
from test_bus_runner import Base

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)          # 16:00 Lagos


class Informal(unittest.TestCase):
    def test_slang_is_understood_and_reported_but_raw_text_is_kept(self):
        r = informal.normalise("wah is price doing rn?")
        self.assertEqual(r["raw"], "wah is price doing rn?")
        self.assertEqual(r["text"], "what is price doing right now?")
        self.assertEqual({c["from"] for c in r["changes"]}, {"wah", "rn"})
        self.assertEqual({c["from"]: c["to"] for c in r["changes"]}, {"wah": "what", "rn": "right now"})

    def test_ambiguous_words_are_reported_never_expanded(self):
        r = informal.normalise("price go retrace abi")
        self.assertEqual(r["text"], "price go retrace abi")
        self.assertEqual({u["word"] for u in r["uncertain"]}, {"go", "abi"})

    def test_typo_repair_is_small_and_reported(self):
        r = informal.normalise("what is the thesiss")
        self.assertIn("thesis", r["text"]); self.assertEqual(r["changes"][0]["basis"], "near-miss spelling")
        self.assertEqual(informal.normalise("hello there")["changes"], [])

    def test_your_own_slang_table_is_used_for_routing_only(self):
        r = informal.normalise("zzz is up", {"zzz": "price"})
        self.assertEqual(r["text"], "price is up"); self.assertEqual(r["raw"], "zzz is up")
        self.assertEqual(r["changes"][0]["basis"], "your /slang")

    def test_stance_is_a_reading_with_its_cue(self):
        s = informal.classify_stance("price broke the high and I think it will retrace because it swept liquidity")
        self.assertEqual({x["stance"] for x in s}, {"observation", "expectation", "reasoning"})
        self.assertTrue(all(x["cue"] for x in s))
        self.assertEqual(informal.classify_stance("hello"), [])

    def test_kind_of(self):
        self.assertEqual(informal.kind_of("/now"), "command")
        self.assertEqual(informal.kind_of("wah u seeing rn"), "question")
        self.assertEqual(informal.kind_of("price broke the high"), "statement")
        self.assertEqual(informal.kind_of("  "), "empty")


class Times(unittest.TestCase):
    def test_by_means_the_hour_before_and_assumptions_are_spoken(self):
        w = timeref.parse("what happened by 8:30", NOW)
        self.assertEqual((timeref.hm(w.start), timeref.hm(w.end)), ("07:30", "08:30"))
        self.assertTrue(any("Lagos" in a for a in w.assumptions))

    def test_between_and_named_day(self):
        w = timeref.parse("between 8 and 8:30", NOW)
        self.assertEqual((timeref.hm(w.start), timeref.hm(w.end)), ("08:00", "08:30"))
        d = timeref.parse("what did we talk about tuesday", NOW)         # NOW is a Thursday: Tuesday is 29 Sep
        self.assertEqual(timeref.lagos(d.start).day, 29); self.assertEqual(d.kind, "day")

    def test_no_time_given_is_none_not_a_guess(self):
        self.assertIsNone(timeref.parse("what happened", NOW))


class Voice(unittest.TestCase):
    def test_glue_words_contain_no_digits_or_prices(self):
        for p in voice.phrases():
            self.assertFalse(re.search(r"\d", p), p)

    def test_wrap_never_changes_the_body_and_is_stable_for_one_seed(self):
        body = "HTF bias is bearish, 1.3200."
        out = voice.wrap("answer", body, 7)
        self.assertTrue(out.endswith(body)); self.assertEqual(out, voice.wrap("answer", body, 7))


class Interpret(unittest.TestCase):
    def kind(self, t, **kw):
        return converse.interpret(t, NOW, kw.get("extra"))["kind"]

    def test_the_messy_phrasings(self):
        self.assertEqual(converse.interpret("wah is price doing rn?", NOW)["canonical"], "/now")
        self.assertEqual(converse.interpret("what are u looking at rn", NOW)["canonical"], "/watch")
        self.assertEqual(converse.interpret("abeg wetin dey happen", NOW)["canonical"], "/now")
        self.assertEqual(self.kind("buh it broke the high tho, im looking for a retracement to support"), "offer_save")
        self.assertEqual(self.kind("what happened by 8:30"), "market_recon")
        self.assertEqual(self.kind("what did we talk about tuesday"), "convo_recon")
        self.assertEqual(self.kind("and after that?"), "followup")
        self.assertEqual(self.kind("what happened"), "need_time")
        self.assertEqual(self.kind("hmm"), "unknown")
        self.assertEqual(self.kind("/now"), "command")

    def test_resolve_replies(self):
        two = [{"id": "1", "label": "answer it now"}, {"id": "2", "label": "save it as a research question"}]
        self.assertEqual(converse.resolve(two, "2")[1]["id"], "2")
        self.assertEqual(converse.resolve(two, "the second one")[1]["id"], "2")
        self.assertEqual(converse.resolve(two, "research pls")[1]["id"], "2")
        self.assertEqual(converse.resolve(two, "nah")[0], "cancel")
        self.assertEqual(converse.resolve(two, "yes")[0], "ambiguous")           # two options: "yes" is not an answer
        self.assertEqual(converse.resolve(two[:1], "yeah")[1]["id"], "1")
        self.assertEqual(converse.resolve(two, "what is the weather")[0], None)  # not an answer: treated as a new message


def candle(t, o, h, l, c):
    return {"datetime": t.isoformat(), "open": o, "high": h, "low": l, "close": c}


class MarketWindow(unittest.TestCase):
    S = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)

    def test_nothing_after_the_end_is_used_and_gaps_are_stated(self):
        cs_ = [candle(self.S + timedelta(minutes=5 * i), 1.33, 1.3310 + i * 0.0001, 1.3290, 1.3305) for i in range(12)]
        cs_.append(candle(self.S + timedelta(minutes=70), 1.33, 1.40, 1.20, 1.3))             # AFTER the window: must not count
        rep = mr.reconstruct_window(self.S, self.S + timedelta(hours=1), cs_, now=NOW)
        self.assertEqual(rep["candle_count"], 12)
        self.assertLess(rep["price"]["high"], 1.35)
        self.assertEqual(mr.reconstruct_window(self.S, self.S + timedelta(hours=1), cs_[:6], now=NOW)["candle_count"], 6)
        self.assertTrue(mr.reconstruct_window(self.S, self.S + timedelta(hours=1), cs_[:6], now=NOW)["candle_gaps"])

    def test_no_archive_and_future_and_empty_are_said(self):
        self.assertIsNone(mr.reconstruct_window(self.S, self.S + timedelta(hours=1), [], now=NOW)["price"])
        self.assertEqual(mr.reconstruct_window(NOW + timedelta(hours=1), NOW + timedelta(hours=2), [], now=NOW)["status"], "FUTURE")
        self.assertEqual(mr.reconstruct_window(self.S, self.S, [], now=NOW)["status"], "EMPTY_WINDOW")

    def test_unreadable_candles_are_counted_not_dropped_silently(self):
        bad = [{"datetime": self.S.isoformat(), "open": "x", "high": 1, "low": 1, "close": 1}]
        self.assertEqual(mr.reconstruct_window(self.S, self.S + timedelta(hours=1), bad, now=NOW)["unreadable_candles"], 1)

    def trade(self, d="SELL"):
        return {"direction": d, "entry": 1.3200, "sl": 1.3260, "tp": 1.3100}

    def test_follow_through_stop_and_target_in_one_candle_is_unknowable(self):
        a = self.S
        both = [candle(a + timedelta(minutes=5), 1.32, 1.3270, 1.3090, 1.32)]           # touches stop AND target
        ft = mr.follow_through(self.trade(), a, both, now=NOW)
        self.assertTrue(ft["first_touch"]["stop"] and ft["first_touch"]["target"])
        self.assertIn("which came first", mr.render_follow(ft).lower())                # said to be unknowable

    def test_follow_through_stops_at_the_recorded_close_and_never_decides_the_outcome(self):
        a = self.S
        cs_ = [candle(a + timedelta(minutes=5 * i), 1.32, 1.3205, 1.3195, 1.32) for i in range(1, 7)]
        ft = mr.follow_through(self.trade(), a, cs_, outcome_at=a + timedelta(minutes=15), now=NOW)
        self.assertEqual(ft["candle_count"], 2); self.assertEqual(ft["bounded_by"], "the recorded close")
        txt = mr.render_follow(ft, "loss")
        self.assertIn("loss", txt); self.assertIn("recorded", txt.lower())
        self.assertEqual(mr.follow_through(self.trade(), a, [], now=NOW)["status"], "NO_CANDLES")
        self.assertEqual(mr.follow_through(self.trade(), a, cs_, now=a)["status"], "NOTHING_TO_FOLLOW")


class Flow(Base):
    """The runner end to end. Messages carry their own received_at so waiting is visible."""
    def say(self, *texts, at=None, base=100):
        at = at or self.now
        with open(os.path.join(self.root, "inbox.jsonl"), "a") as f:
            for i, t in enumerate(texts):
                f.write(json.dumps({"update_id": base + len(self.sent) + i, "chat_id": "1", "text": t, "received_at": at.isoformat()}) + "\n")

    def talk(self, text, at=None, now=None):
        n = len(self.sent)
        self.say(text, at=at)
        self.run_(now=now or at or self.now)
        return self.sent[n] if len(self.sent) > n else None

    def candles(self, start, n, step=5):
        with open(os.path.join(self.root, "ohlc_5m_log.jsonl"), "a") as f:
            for i in range(n):
                f.write(json.dumps(candle(start + timedelta(minutes=step * i), 1.33, 1.3310, 1.3290, 1.3300 + i * 0.0001)) + "\n")

    def types(self, name):
        return [r for r in self.records() if r["event_type"] == name]

    # plain questions are answered, never captured -----------------------------------------------
    def test_a_slangy_question_is_answered_and_nothing_is_saved(self):
        out = self.talk("wah is price doing rn?")
        self.assertIn("HTF bias is bearish", out)
        self.assertEqual(self.types("KNOWLEDGE_STATED"), []); self.assertEqual(self.types("RESEARCH_QUESTION_CAPTURED"), [])

    def test_raw_message_is_stored_exactly_as_typed(self):
        self.talk("wah is price doing rn?")
        self.assertEqual(self.types("MESSAGE_RECEIVED")[0]["payload"]["text"], "wah is price doing rn?")

    # offering to save a statement ----------------------------------------------------------------
    def test_a_statement_is_offered_not_saved_until_you_say_yes(self):
        said = "buh it broke the high tho, im looking for a retracement to support"
        q = self.talk(said)
        self.assertIn("save it", q.lower()); self.assertEqual(self.types("KNOWLEDGE_STATED"), [])
        a = self.talk("yes")
        k = self.types("KNOWLEDGE_STATED")
        self.assertEqual(len(k), 1); self.assertEqual(k[0]["payload"]["text"], said)               # word for word
        rd = k[0]["payload"]["reading"]
        self.assertEqual(rd["status"], "interpreted_not_verified")
        self.assertEqual({s["stance"] for s in rd["stances"]}, {"observation", "expectation"})
        self.assertIn("not something I've checked", a)

    def test_saying_no_saves_nothing_and_closes_the_question(self):
        self.talk("buh it broke the high tho, im looking for a retracement to support")
        self.assertIn("left it", self.talk("nah"))
        self.assertEqual(self.types("KNOWLEDGE_STATED"), [])
        self.assertIn("/help", self.talk("yes") or "")                 # a later "yes" is not an answer to anything

    def test_a_stale_question_is_not_answered_by_a_much_later_yes(self):
        self.talk("buh it broke the high tho, im looking for a retracement to support")
        out = self.talk("yes", at=self.now + br.PENDING_TTL + timedelta(minutes=5))
        self.assertEqual(self.types("KNOWLEDGE_STATED"), []); self.assertNotIn("Noted", out)

    def test_another_message_in_between_closes_the_question(self):
        self.talk("buh it broke the high tho, im looking for a retracement to support")
        self.talk("/now")
        self.talk("yes")
        self.assertEqual(self.types("KNOWLEDGE_STATED"), [])

    def test_a_note_is_stamped_with_when_you_sent_it_not_when_it_was_read(self):
        sent = self.now
        self.talk("buh it broke the high tho, im looking for a retracement to support", at=sent)
        self.talk("yes", at=sent + timedelta(minutes=1), now=sent + timedelta(hours=2))
        k = self.types("KNOWLEDGE_STATED")[0]
        self.assertEqual(ledger_t(k["ts"]), sent)

    # /ask clarification --------------------------------------------------------------------------
    def test_ask_with_a_right_now_question_asks_which_you_mean(self):
        q = self.talk("/ask what are u looking at rn")
        self.assertIn("Answer it now", q); self.assertIn("research question", q)
        self.assertEqual(self.types("RESEARCH_QUESTION_CAPTURED"), [])
        self.assertIn("Watching", self.talk("1"))

    def test_ask_second_option_captures_the_words_exactly(self):
        self.talk("/ask what are u looking at rn")
        self.talk("2")
        cap = [r for r in self.records() if r["event_type"].startswith("RESEARCH") or r["event_type"].startswith("QUESTION")]
        self.assertTrue(cap)
        self.assertIn("what are u looking at rn", json.dumps(cap))

    def test_ask_with_a_real_research_question_is_just_captured(self):
        out = self.talk("/ask does the second leg of a sweep usually retrace deeper")
        self.assertNotIn("Answer it now", out)

    # market reconstruction -----------------------------------------------------------------------
    def test_market_window_uses_saved_candles_and_says_what_is_missing(self):
        day = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
        self.candles(day + timedelta(minutes=30), 12)                     # 06:30-07:30 UTC = 07:30-08:30 Lagos
        out = self.talk("what happened by 8:30")
        self.assertIn("07:30", out); self.assertIn("08:30", out)
        self.assertIn("Nothing recorded from you", out)                  # honest about the rest
        self.assertNotIn("candle archive to look in yet", out)

    def test_no_archive_is_said_plainly(self):
        out = self.talk("what happened by 8:30")
        self.assertIn("don't have a candle archive", out)

    def test_followup_continues_from_the_end_of_the_last_stretch(self):
        t0 = self.now
        self.talk("what happened by 8:30", at=t0, now=t0)
        out = self.talk("and after that?", at=t0 + timedelta(minutes=1), now=t0 + timedelta(minutes=1))
        self.assertIn("08:30", out); self.assertIn("09:30", out)

    def test_followup_with_no_context_asks_after_what(self):
        self.assertIn("After what?", self.talk("and after that?"))

    def test_asking_to_reconstruct_without_a_time_asks_for_one(self):
        self.assertIn("Which stretch", self.talk("what happened"))

    # conversation reconstruction -----------------------------------------------------------------
    def test_a_day_with_several_topics_asks_which_one_then_replays_it(self):
        tue = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
        self.talk("/logtrade GBPUSD sell 1.3200 sl 1.3260 tp 1.3100", at=tue)
        self.talk("/know the protected high got swept then price dropped", at=tue + timedelta(minutes=5))
        self.talk("/now", at=tue + timedelta(minutes=10))
        q = self.talk("what did we talk about tuesday", at=self.now)
        self.assertIn("1.", q); self.assertIn("2.", q)                    # more than one topic: it asks, with options
        self.assertIn("notes", q.lower())
        n_before = len(self.records())
        out = self.talk("the notes")
        self.assertIn("protected high got swept", out)                   # your words, quoted from what was stored
        self.assertGreaterEqual(len(self.records()), n_before)

    def test_a_day_with_nothing_says_where_the_record_begins(self):
        out = self.talk("what did we talk about last friday")
        self.assertIn("don't have any conversation stored", out)

    def test_reconstruct_command_takes_a_window_and_a_case_number(self):
        self.talk("/logtrade GBPUSD sell 1.3200 sl 1.3260 tp 1.3100")
        self.assertIn("CASE-0001", self.talk("/reconstruct 1"))
        self.assertIn("Lagos", self.talk("/reconstruct between 8 and 8:30"))

    def test_reconstruct_is_read_only(self):
        self.talk("/logtrade GBPUSD sell 1.3200 sl 1.3260 tp 1.3100")
        keep = ("TRADE_LOGGED", "BOT_PLAN_ATTACHED", "OUTCOME_RECORDED", "REASONING_LOGGED", "KNOWLEDGE_STATED")
        before = [r["hash"] for r in self.records() if r["event_type"] in keep]
        self.talk("/reconstruct 1"); self.talk("/reconstruct between 8 and 8:30"); self.talk("what happened by 8:30")
        self.assertEqual(before, [r["hash"] for r in self.records() if r["event_type"] in keep])

    # slang command -------------------------------------------------------------------------------
    def test_slang_teaches_routing_only_and_keeps_raw_words(self):
        self.assertIn("I'll read it as", self.talk("/slang zork = right now"))
        out = self.talk("zork what is price doing")
        self.assertIn("HTF bias", out)
        self.assertEqual(self.types("MESSAGE_RECEIVED")[-1]["payload"]["text"], "zork what is price doing")

    # the delay guard -----------------------------------------------------------------------------
    def test_a_question_read_hours_late_says_so(self):
        out = self.talk("/now", at=self.now, now=self.now + timedelta(hours=1))
        self.assertIn("so this is how things look as of", out)

    def test_a_prompt_question_has_no_late_note(self):
        self.assertNotIn("so this is how things look", self.talk("/now"))

    def test_reading_saved_records_late_has_no_late_note(self):
        self.talk("/logtrade GBPUSD sell 1.3200 sl 1.3260 tp 1.3100")
        self.assertNotIn("so this is how things look", self.talk("/review 1", at=self.now, now=self.now + timedelta(hours=2)))

    # help ----------------------------------------------------------------------------------------
    def test_help_mentions_the_new_ways_to_talk(self):
        h = bus.HELP_TEXT
        for w in ("/slang", "/reconstruct", "plain questions", "detail"):
            self.assertIn(w, h)


def ledger_t(v):
    import ledger
    return ledger.to_utc(v)


class Boundary(unittest.TestCase):
    def imports(self, name):
        with open(os.path.join(HERE, name + ".py")) as f:
            tree = ast.parse(f.read())
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import): found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module: found.add(n.module.split(".")[0])
        return found

    def test_leaves_import_nothing_first_party(self):
        std = {"re", "difflib", "datetime", "zoneinfo"}
        for m in ("informal", "timeref", "voice"):
            self.assertLessEqual(self.imports(m), std, m)

    def test_the_readers_see_only_what_they_are_handed(self):
        self.assertLessEqual(self.imports("market_reconstruct"), {"datetime", "ledger", "timeref", "voice"})
        self.assertLessEqual(self.imports("conversation_search"), {"re", "datetime", "ledger", "timeref", "voice"})
        self.assertLessEqual(self.imports("converse"), {"re", "informal", "timeref", "voice"})


if __name__ == "__main__":
    unittest.main()
