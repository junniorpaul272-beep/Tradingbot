#!/usr/bin/env python3
"""
blind_eval.py — a runner for an INDEPENDENT test of the conversation layer. It does not judge anything. It runs someone else's prompts through the real bot,
keeps the first answers untouched and read-only, records what each message changed, and leaves the marking to a person.

WHO DOES WHAT
  The test author (not the builder) writes the prompts file and keeps it private. The builder never needs to open it: `run` prints only counts unless --show is given.
  The author marks the results (`sheet` makes the form) and `summarize` turns the marked form into the two-gate verdict.

COMMANDS (run from the repo folder)
  python comms/blind_eval.py run prompts.json            run every case once, against a FROZEN copy of the records, and save the evidence
  python comms/blind_eval.py run prompts.json --rerun-of runs/<first run>     the same cases after a fix; the first run is never touched
  python comms/blind_eval.py sheet runs/<run>            write scoring.csv for the author to fill in (expected / observed / category / severity)
  python comms/blind_eval.py summarize runs/<run>        read the marked scoring.csv, print results by category and by tag, and the two gates
  python comms/blind_eval.py compare runs/<fixed> runs/<first>      per test: did the answer change since the first run?
  python comms/blind_eval.py verify runs/<run>           check that the saved evidence has not been edited since it was written

PROMPTS FILE (JSON list). One entry per case:
  {"id": "T001", "prompt": "what is the price", "expected": "what a correct bot does, in the author's words", "tags": ["price", "plain"]}
  a conversation:  {"id": "T002", "turns": ["how many scans today", "and yesterday?"], "expected": "...", "tags": ["followup"]}
  Optional per case: "now": "2026-10-30T11:22:00+00:00" (when the message arrives; default is the run's fixed time).
  Every case gets its OWN fresh copy of the records, so cases cannot affect each other; the turns of one conversation share one copy.

HOW THE BOT IS RUN
  Each message goes through the real inbox runner (bus_runner.run): the owner check, the bot, the guard, the ledger and delivery, exactly as a Telegram message would.
  Nothing is sent anywhere: delivery is captured. The route the bot chose is read afterwards by calling the (pure) router on the same text; it never changes how the message is handled.

WHAT IS RECORDED PER TURN
  test id, exact input, the complete first reply as sent, status (completed / crashed / timed out), the route chosen, the build id, and the SIDE EFFECTS:
  every ledger event the message caused (by type), notes saved, trades logged, a pending offer created, any file other than the ledger that changed, and the messages sent.
  A good-looking answer does not excuse a side effect: `unexpected_side_effects` lists anything beyond the normal "message received / reply recorded / delivered".
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import traceback
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

CATEGORIES = ("correct_answer", "good_clarification", "safe_refusal", "safe_but_unhelpful", "wrong_answer", "critical_failure")
CAPABILITY_FAILURES = ("safe_but_unhelpful", "wrong_answer", "critical_failure")
NORMAL_EVENTS = {"MESSAGE_RECEIVED", "BUS_REPLY", "BUS_DELIVERED", "BUS_STATEMENT"}     # anything else a message causes is reported, including a proactive announcement
SKIP_COPY = {"user_ledger.jsonl", "inbox.jsonl"}
TIMEOUT_S = 60


# ---------------------------------------------------------------- evidence plumbing
def sha(b):
    return hashlib.sha256(b).hexdigest()


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def build_id():
    """A fingerprint of the bot's own source files (comms/*.py except tests and this runner) plus the git commit when there is one, and whether the tree was dirty."""
    h = hashlib.sha256()
    n = 0
    for name in sorted(os.listdir(HERE)):
        if name.endswith(".py") and not name.startswith(("test_", "stress_", "blind_eval")):
            h.update(name.encode()); h.update(sha_file(os.path.join(HERE, name)).encode()); n += 1
    info = {"source_hash": h.hexdigest()[:16], "files": n, "git_commit": None, "git_dirty": None}
    try:
        import subprocess
        info["git_commit"] = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip() or None
        info["git_dirty"] = bool(subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip())
    except Exception:
        pass
    return info


def make_world(src_root):
    """A fresh private copy of the records (top-level files only, as the bot reads them), without the ledger and inbox."""
    d = tempfile.mkdtemp(prefix="blind_world_")
    for f in os.listdir(src_root):
        p = os.path.join(src_root, f)
        if os.path.isfile(p) and f not in SKIP_COPY:
            shutil.copy2(p, d)
    return d


def snapshot(d):
    return {f: (os.stat(os.path.join(d, f)).st_size, os.stat(os.path.join(d, f)).st_mtime_ns) for f in os.listdir(d) if os.path.isfile(os.path.join(d, f))}


class Timeout(Exception):
    pass


def _alarm(_s, _f):
    raise Timeout()


def read_ledger(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def event_type(r):
    return r.get("event_type") or r.get("type") or "?"


# ---------------------------------------------------------------- running
def run_case(case, root_src, default_now, br, converse, ledger):
    turns = case.get("turns") or ([case["prompt"]] if "prompt" in case else [])
    now0 = ledger.to_utc(case["now"]) if case.get("now") else default_now
    world = make_world(root_src)
    ledger_path = os.path.join(world, br.LEDGER_NAME)
    out_turns = []
    control = {}
    try:
        # CONTROL RUN: the bot's own scheduled pass with NO message in the inbox. In a fresh copy of the records the integrity inspector's open findings look "new"
        # (there is no earlier announcement in the empty ledger), so the bot announces them on any run. Capturing that here, before the message, shows it is independent
        # of the message; anything a message adds on top of it is then a real side effect of that message.
        csent = []
        try:
            br.run(world, now=now0 - dt.timedelta(seconds=5), send=lambda t: (csent.append(t), True)[1])
        except BaseException as e:
            control["error"] = f"{type(e).__name__}: {e}"[:200]
        control.update({"proactive_messages_without_any_user_message": len(csent), "texts": [c[:160] for c in csent],
                        "cause": "bot-run announcements (integrity findings not yet announced in this fresh ledger); not caused by the user's message" if csent else "none"})
        for i, text in enumerate(turns):
            now = now0 + dt.timedelta(seconds=45 * i)
            before_files = snapshot(world)
            before_ledger = read_ledger(ledger_path)
            sent = []
            uid = 1000 + i
            with open(os.path.join(world, "inbox.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"update_id": uid, "chat_id": "1", "text": text, "received_at": now.isoformat()}) + "\n")
            route = None
            try:                                              # read-only: what the router thinks this is. Never feeds back into handling.
                recs, _ = ledger.read(ledger_path)
                d = converse.interpret(text, now, None, ctx=br._last_context(recs, now))
                route = {"kind": d.get("kind")}
                if d.get("chat"):
                    route["intent"] = d["chat"].get("intent"); route["policy_key"] = d["chat"].get("key")
            except Exception as e:
                route = {"error": f"{type(e).__name__}"}
            status, err = "completed", None
            old = signal.signal(signal.SIGALRM, _alarm); signal.alarm(TIMEOUT_S)
            try:
                summary = br.run(world, now=now, send=lambda t: (sent.append(t), True)[1])
                if summary.get("errors"):
                    status, err = "crashed", "the bot reported an internal error and sent its error message"
            except Timeout:
                status, err = "timed_out", f"no answer within {TIMEOUT_S}s"
            except BaseException as e:
                status, err = "crashed", f"{type(e).__name__}: {e}"[:300]
            finally:
                signal.alarm(0); signal.signal(signal.SIGALRM, old)
            after_ledger = read_ledger(ledger_path)
            new = after_ledger[len(before_ledger):]
            types = Counter(event_type(r) for r in new)
            after_files = snapshot(world)
            changed = sorted(f for f in after_files if before_files.get(f) != after_files[f] and f not in (br.LEDGER_NAME, "inbox.jsonl"))
            reply_payload = next(((r.get("payload") or {}) for r in new if event_type(r) == "BUS_REPLY"), {})
            effects = {
                "ledger_events": dict(types),
                "notes_saved": types.get("KNOWLEDGE_STATED", 0),
                "trades_logged": types.get("TRADE_LOGGED", 0),
                "pending_offer_created": bool(reply_payload.get("pending")),
                "other_files_changed": changed,
                "messages_sent": len(sent),
                "proactive_alert_messages": types.get("BUS_ANNOUNCED", 0),
            }
            unexpected = [f"ledger event {t} x{n}" for t, n in types.items() if t not in NORMAL_EVENTS] + [f"file changed: {f}" for f in changed]
            if len(sent) > 1:
                unexpected.append(f"{len(sent)} messages sent for one input")
            out_turns.append({"turn": i + 1, "input": text, "first_response": sent[0] if sent else None, "all_messages_sent": sent, "status": status, "error": err,
                              "route": route, "side_effects": effects, "unexpected_side_effects": unexpected, "answered_at": now.isoformat()})
    finally:
        shutil.rmtree(world, ignore_errors=True)
    return {"test_id": case["id"], "tags": case.get("tags", []), "expected_behaviour": case.get("expected", ""), "control_run": control, "turns": out_turns}


def cmd_run(a):
    os.environ.setdefault("TELEGRAM_CHAT_ID", "1"); os.environ.setdefault("TELEGRAM_TOKEN", "dummy"); os.environ.setdefault("TWELVE_DATA_KEY", "dummy")
    import bus_runner as br, converse, ledger
    import socket
    attempts = []

    def _no_net(self, *args, **kw):                                  # nothing in a test may reach the internet: Telegram, an LLM, a price feed, anything
        attempts.append(str(args)[:80]); raise RuntimeError("network blocked by blind_eval")
    socket.socket.connect = _no_net
    with open(a.prompts, "rb") as f:
        raw = f.read()
    cases = json.loads(raw.decode("utf-8"))
    ids = [c.get("id") for c in cases]
    if len(set(ids)) != len(ids) or any(not i for i in ids):
        sys.exit("Every case needs a unique id.")
    root_src = os.path.abspath(a.root)
    now = ledger.to_utc(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    bid = build_id()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"run_{stamp}_{bid['source_hash'][:8]}"
    out_dir = os.path.join(a.out, run_id)
    if os.path.exists(out_dir):
        sys.exit(f"{out_dir} already exists. A run is never overwritten.")
    os.makedirs(out_dir)
    prof = {}
    try:
        with open(os.path.join(root_src, "account_profiles.json"), encoding="utf-8") as f:
            prof = json.load(f)
    except Exception:
        pass
    data_fp = hashlib.sha256()
    for f in sorted(os.listdir(root_src)):
        p = os.path.join(root_src, f)
        if os.path.isfile(p) and f not in SKIP_COPY and f.endswith((".json", ".jsonl")):
            data_fp.update(f.encode()); data_fp.update(str(os.stat(p).st_size).encode())
    results = []
    for i, case in enumerate(cases, 1):
        try:
            res = run_case(case, root_src, now, br, converse, ledger)
        except Exception:
            res = {"test_id": case.get("id"), "tags": case.get("tags", []), "expected_behaviour": case.get("expected", ""),
                   "turns": [{"turn": 1, "input": case.get("prompt"), "first_response": None, "status": "runner_error", "error": traceback.format_exc()[-400:],
                              "route": None, "side_effects": {}, "unexpected_side_effects": []}]}
        results.append(res)
        if not a.show:
            print(f"\r{i}/{len(cases)} cases", end="", flush=True)
        else:
            for t in res["turns"]:
                print(f"\n[{res['test_id']}.{t['turn']}] {t['input']}\n   -> {(t['first_response'] or '(none)')[:300]}\n   side effects: {t['unexpected_side_effects'] or 'none'}")
    res_path = os.path.join(out_dir, "results.jsonl")
    with open(res_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    shutil.copy2(a.prompts, os.path.join(out_dir, "prompts.json"))
    manifest = {"run_id": run_id, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "bot_time_used": now.isoformat(), "build": bid,
                "config": {"account_profile": prof, "records_fingerprint": data_fp.hexdigest()[:16], "records_root": root_src, "python": sys.version.split()[0],
                           "per_case_timeout_s": TIMEOUT_S, "delivery": "captured, nothing sent; all network connections blocked during the run", "owner_chat_env_set_by_runner": True},
                "prompts_sha256": sha(raw), "results_sha256": sha_file(res_path), "cases": len(cases), "turns": sum(len(r["turns"]) for r in results),
                "rerun_of": None}
    manifest["network_attempts_blocked"] = len(attempts)
    if a.rerun_of:
        try:
            with open(os.path.join(a.rerun_of, "MANIFEST.json"), encoding="utf-8") as f:
                m0 = json.load(f)
            manifest["rerun_of"] = {"run_id": m0["run_id"], "build": m0["build"]["source_hash"], "prompts_sha256": m0["prompts_sha256"],
                                    "same_prompts_file": m0["prompts_sha256"] == manifest["prompts_sha256"]}
        except Exception as e:
            sys.exit(f"Can't read the run to compare with ({a.rerun_of}): {e}")
    with open(os.path.join(out_dir, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    for fn in ("results.jsonl", "prompts.json", "MANIFEST.json"):
        os.chmod(os.path.join(out_dir, fn), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)       # read-only: the first run is evidence
    st = Counter(t["status"] for r in results for t in r["turns"])
    fx = sum(1 for r in results for t in r["turns"] if t["unexpected_side_effects"])
    print(f"\n{run_id}: {len(cases)} cases, {manifest['turns']} messages; status {dict(st)}; messages with an unexpected side effect: {fx}")
    print(f"Network connections attempted during the run (all blocked): {len(attempts)}.")
    print(f"Saved read-only in {out_dir}. Build {bid['source_hash']} (git {bid['git_commit']}{', with uncommitted changes' if bid['git_dirty'] else ''}).")
    print("Next: `sheet` to make the marking form. The answers are not judged by this tool.")


# ---------------------------------------------------------------- reading back
def load_run(d):
    with open(os.path.join(d, "MANIFEST.json"), encoding="utf-8") as f:
        m = json.load(f)
    rows = [json.loads(l) for l in open(os.path.join(d, "results.jsonl"), encoding="utf-8")]
    return m, rows


def cmd_verify(a):
    with open(os.path.join(a.run, "MANIFEST.json"), encoding="utf-8") as f:
        m = json.load(f)
    bad = []
    if sha_file(os.path.join(a.run, "results.jsonl")) != m["results_sha256"]:
        bad.append("results.jsonl has been changed since it was written")
    if sha_file(os.path.join(a.run, "prompts.json")) != m["prompts_sha256"]:
        bad.append("prompts.json has been changed since the run")
    print("OK: the saved evidence is exactly as written." if not bad else "NOT OK: " + "; ".join(bad))
    sys.exit(1 if bad else 0)


def cmd_sheet(a):
    m, rows = load_run(a.run)
    path = os.path.join(a.run, "scoring.csv")
    if os.path.exists(path) and not a.force:
        sys.exit(f"{path} exists (it may hold marks). Use --force to overwrite.")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["test_id", "turn", "tags", "input", "first_response", "route", "status", "unexpected_side_effects", "expected_behaviour (author)",
                    "observed_behaviour (author)", "category (" + " | ".join(CATEGORIES) + ")", "severity_flag (none | critical)", "notes"])
        for r in rows:
            for t in r["turns"]:
                w.writerow([r["test_id"], t["turn"], ",".join(r["tags"]), t["input"], t["first_response"], json.dumps(t.get("route")), t["status"],
                            "; ".join(t["unexpected_side_effects"]), r["expected_behaviour"], "", "", "", ""])
    print(f"Wrote {path}. Fill in observed_behaviour, category and severity_flag for each row, using ONLY the six category names. A reply of 'I didn't catch that' to a "
          f"reasonable question is safe_but_unhelpful, not a pass.")


def cmd_summarize(a):
    m, rows = load_run(a.run)
    path = a.sheet or os.path.join(a.run, "scoring.csv")
    with open(path, newline="", encoding="utf-8") as f:
        sheet = list(csv.reader(f))
    head, body = sheet[0], sheet[1:]
    ci = next(i for i, h in enumerate(head) if h.startswith("category"))
    si = next(i for i, h in enumerate(head) if h.startswith("severity"))
    ni = head.index("notes")
    ti = head.index("tags"); xi = head.index("unexpected_side_effects")
    unmarked = [r[0] + "." + r[1] for r in body if r[ci].strip() not in CATEGORIES]
    if unmarked:
        print(f"{len(unmarked)} rows are not marked with one of the six category names yet, e.g. {unmarked[:6]}. They are left out below and block the verdict.")
    cat = Counter(r[ci].strip() for r in body if r[ci].strip() in CATEGORIES)
    n = sum(cat.values())
    print(f"\nRun {m['run_id']}  build {m['build']['source_hash']}  ({n} marked of {len(body)} messages)\n\nFIRST-PASS RESULTS BY CATEGORY")
    for c in CATEGORIES:
        print(f"  {c:20s} {cat[c]:4d}  {100 * cat[c] / n:5.1f}%" if n else f"  {c:20s} {cat[c]:4d}")
    by_tag = defaultdict(Counter)
    for r in body:
        if r[ci].strip() in CATEGORIES:
            for tg in (r[ti].split(",") if r[ti] else ["(untagged)"]):
                by_tag[tg.strip()][r[ci].strip()] += 1
    print("\nBY TAG (the capability view: where it is weak is visible here, not hidden in the total)")
    for tg, c in sorted(by_tag.items()):
        tot = sum(c.values()); good = c["correct_answer"] + c["good_clarification"] + c["safe_refusal"]
        print(f"  {tg:22s} n={tot:3d}  right={good:3d}  " + "  ".join(f"{k}={v}" for k, v in c.items() if k in CAPABILITY_FAILURES and v))
    crit = [r for r in body if r[ci].strip() == "critical_failure" or r[si].strip().lower() == "critical"]
    side = [r for r in body if r[xi].strip()]
    cap = [r for r in body if r[ci].strip() in CAPABILITY_FAILURES[:2]]
    print("\nGATE 1: critical failures (fabricated action, unauthorised write, protected information, bypassed boundary)")
    for r in crit:
        print(f"  {r[0]}.{r[1]}  {r[3][:70]!r}  -> {r[4][:90]!r}  {r[ni][:80]}")
    for r in side:
        print(f"  side effect on {r[0]}.{r[1]}: {r[xi]}  (the author must decide whether it was authorised; if not it is a critical failure)")
    gate1 = not crit and not unmarked
    print("  " + ("PASS: no unresolved critical failure." if gate1 else "BLOCKED: readiness cannot be claimed while any of the above stands, or rows are unmarked."))
    print("\nGATE 2: general capability (answerable questions, wording, follow-ups, ambiguity). There is no pass mark; read the table above.")
    print(f"  safe_but_unhelpful {cat['safe_but_unhelpful']} and wrong_answer {cat['wrong_answer']} are capability failures, even when the bot only said it didn't catch it.")
    for r in cap[:25]:
        print(f"  {r[ci].strip():18s} {r[0]}.{r[1]}  {r[3][:80]!r}")
    print("\nThis tool does not decide readiness. Synthetic or builder-written prompts do not count toward it.")


def cmd_compare(a):
    m1, r1 = load_run(a.run); m0, r0 = load_run(a.first)
    old = {(r["test_id"], t["turn"]): t for r in r0 for t in r["turns"]}
    ch = same = missing = 0
    print(f"Fixed run {m1['run_id']} (build {m1['build']['source_hash']}) against first run {m0['run_id']} (build {m0['build']['source_hash']}).")
    if m1["prompts_sha256"] != m0["prompts_sha256"]:
        print("Note: the prompts file differs, so only cases with the same id AND the same input are compared.")
    for r in r1:
        for t in r["turns"]:
            o = old.get((r["test_id"], t["turn"]))
            if not o or o["input"] != t["input"]:
                missing += 1; continue
            if o["first_response"] == t["first_response"] and o["unexpected_side_effects"] == t["unexpected_side_effects"]:
                same += 1
            else:
                ch += 1
                print(f"\n[{r['test_id']}.{t['turn']}] {t['input']}\n   was: {(o['first_response'] or '')[:200]}\n   now: {(t['first_response'] or '')[:200]}")
    print(f"\nunchanged {same}, changed {ch}, not comparable {missing}. The first run is untouched; mark the changed ones against the author's expected behaviour.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run"); r.add_argument("prompts"); r.add_argument("--root", default=REPO); r.add_argument("--out", default=os.path.join(REPO, "runs"))
    r.add_argument("--now"); r.add_argument("--rerun-of"); r.add_argument("--show", action="store_true", help="print each prompt and answer (the builder should NOT use this on the author's file)")
    s = sp.add_parser("sheet"); s.add_argument("run"); s.add_argument("--force", action="store_true")
    z = sp.add_parser("summarize"); z.add_argument("run"); z.add_argument("--sheet")
    c = sp.add_parser("compare"); c.add_argument("run"); c.add_argument("first")
    v = sp.add_parser("verify"); v.add_argument("run")
    a = p.parse_args()
    {"run": cmd_run, "sheet": cmd_sheet, "summarize": cmd_summarize, "compare": cmd_compare, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    main()
