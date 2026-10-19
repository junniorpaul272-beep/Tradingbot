#!/usr/bin/env python3
"""
comms_inspector.py — the Comms Integrity Inspector. READ-ONLY.

It checks how the communication layer's pieces connect and whether what the bot SAYS is sound. It does not judge any market
reading, never edits a file, never writes to a real ledger (every probe runs in a throwaway temp folder) and never sends a
real message. It is a different tool from inspector.py (which audits the ENGINE's state); this one audits `comms/`.

    python comms/comms_inspector.py [repo-root] [--json report.json] [--previous old_report.json] [--quick] [--full-checkout]

Exit 0: no defect found in what it checked.  Exit 1: at least one CODE_DEFECT / CONTRACT_MISMATCH / RESPONSE_LEAK /
AUTHORITY_VIOLATION.  MISSING_EVIDENCE and STALE_EVIDENCE are reported but are NOT defects: a trade with no outcome yet is
the user's evidence gap, not a bug, and the bot is supposed to say so.

WHAT IT CHECKS
  A. Structure   what each file is (production / test / fixture / doc / tooling / config), who imports whom, import cycles,
                 retired root-level copies of moved modules, workflow entry points that point at nothing, and the layer rules
                 (by running check_layer_imports.py: there is ONE set of import rules, not a second one here).
  B. Contracts   every command the bot answers, whether /help and the menu know it, how many test files mention it, and
                 whether a read-only command wrote anything.
  C. Probes      every command, in four situations (no engine state / fresh / after a logged trade / stale state), with four
                 argument shapes, run through the real runner on a fake inbox. Each reply is checked for response leaks,
                 invented market claims when there is no state, wrong case numbers, internal errors shown as chat,
                 duplicate replies and duplicate processing.

WHAT IT DOES NOT CHECK (always printed): market interpretations; real Telegram delivery; anything the probes do not reach;
whether the existing tests are adequate. "No findings" means "none of THESE checks fired", never "the bot is healthy".
Request-path tracing with correlation IDs across every stage is a later stage; today each finding names the first stage the
probe can observe, and says when it cannot tell which module is at fault.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bus                      # noqa: E402
import bus_runner as br         # noqa: E402
import ledger                   # noqa: E402

DEFECT_CATEGORIES = ("CODE_DEFECT", "CONTRACT_MISMATCH", "RESPONSE_LEAK", "AUTHORITY_VIOLATION")
CATEGORIES = DEFECT_CATEGORIES + ("MISSING_EVIDENCE", "STALE_EVIDENCE", "INTEGRITY_UNEVALUABLE", "VERIFIED")
STAGES = ("incoming message", "routing", "parameter resolution", "retrieval", "response construction", "final reply", "static structure")

RUNNER_ONLY = ("/taken", "/skip", "/quiet", "/loud", "/reconstruct")
WRITERS = set(bus.ACT_COMMANDS) | {"/taken", "/skip", "/quiet", "/loud"}
LOG_ONLY = {"MESSAGE_RECEIVED", "BUS_REPLY", "BUS_DELIVERED", "BUS_DELIVERY_FAILED", "BUS_STATEMENT"}
FIXTURE_NAME = "fixture_real_state.json"


# ---------------------------------------------------------------------------------------------------------------------
# findings
def finding(rule, category, module, stage, expected, observed, evidence, repro, likely_cause, fix, regression, severity=None):
    assert category in CATEGORIES and stage in STAGES
    fid = "CI-" + hashlib.sha256(f"{rule}|{module}|{repro.get('key', '')}".encode()).hexdigest()[:8]
    return {"id": fid, "rule": rule, "severity": severity or ("high" if category in DEFECT_CATEGORIES else "info"),
            "category": category, "module": module, "stage": stage, "expected": expected, "observed": observed,
            "evidence": evidence, "reproduction": {k: v for k, v in repro.items() if k != "key"},
            "likely_cause": likely_cause + " (likely, not confirmed)", "suggested_fix": fix, "regression_test": regression,
            "status": "new"}


# ---------------------------------------------------------------------------------------------------------------------
# A. structure
def comms_dir(root):
    d = os.path.join(root, "comms")
    return d if os.path.isdir(d) else root


def classify(name):
    if name.startswith("test_") and name.endswith(".py"):
        return "test"
    if name.endswith(".py"):
        return "diagnostic/tooling" if name in ("comms_inspector.py", "inspector.py", "check_layer_imports.py", "set_bot_commands.py") else "production"
    if name.endswith((".yml", ".yaml")):
        return "integration/configuration"
    if name.endswith(".md"):
        return "documentation"
    if name.endswith((".json", ".jsonl")):
        return "fixture"
    return "other"


def _imports(path):
    try:
        with open(path) as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return None
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module.split(".")[0])
    return out


def inventory(root):
    cdir = comms_dir(root)
    files = sorted(f for f in os.listdir(cdir) if os.path.isfile(os.path.join(cdir, f)) and not f.startswith("."))
    rootfiles = sorted(f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f)) and f.endswith(".py")) if cdir != root else []
    mods = {f[:-3] for f in files if f.endswith(".py")}
    imp, unreadable = {}, []
    for f in files:
        if f.endswith(".py"):
            got = _imports(os.path.join(cdir, f))
            if got is None:
                unreadable.append(f)
            else:
                imp[f[:-3]] = got & mods
    importers = {m: sorted(k + ".py" for k, v in imp.items() if m in v and k != m) for m in mods}
    tests = {m: sorted(k + ".py" for k in mods if k.startswith("test_") and m in imp.get(k, ())) for m in mods}
    inv = [{"file": f, "kind": classify(f), "imports": sorted(imp.get(f[:-3], ())) if f.endswith(".py") else [],
            "imported_by": importers.get(f[:-3], []) if f.endswith(".py") else [],
            "tests_importing_it": tests.get(f[:-3], []) if classify(f) == "production" else []} for f in files]
    return {"dir": cdir, "files": inv, "root_python": rootfiles, "unreadable": unreadable, "graph": imp, "mods": mods}


def import_cycles(graph):
    """Cycles among comms modules, found by DFS. Returns each distinct cycle once as a list of module names."""
    cycles, seen = [], set()
    def dfs(node, path, onpath):
        for nxt in sorted(graph.get(node, ())):
            if nxt in onpath:
                cyc = path[path.index(nxt):] + [nxt]
                key = frozenset(cyc)
                if key not in seen:
                    seen.add(key); cycles.append(cyc)
            elif nxt not in done:
                dfs(nxt, path + [nxt], onpath | {nxt})
        done.add(node)
    done = set()
    for n in sorted(graph):
        if n not in done:
            dfs(n, [n], {n})
    return cycles


def static_findings(root, inv, run_layer=True, full_checkout=False):
    out = []
    for m in sorted(inv["mods"]):
        if os.path.exists(os.path.join(root, m + ".py")) and inv["dir"] != root:
            out.append(finding("retired-root-copy", "CODE_DEFECT", m + ".py", "static structure",
                               f"{m}.py exists only in comms/", f"{m}.py also exists in the repo root",
                               [os.path.join(root, m + ".py"), os.path.join(inv["dir"], m + ".py")],
                               {"key": m, "check": "file exists in both places"},
                               "the old copy was not deleted after the move; Python may import either one depending on where it is run from",
                               f"delete the root copy of {m}.py", "test: no module name appears in both the root and comms/"))
    for cyc in import_cycles(inv["graph"]):
        out.append(finding("import-cycle", "CODE_DEFECT", cyc[0] + ".py", "static structure", "no import cycle", " -> ".join(cyc),
                           cyc, {"key": "->".join(cyc)}, "two modules depend on each other", "move the shared piece to a third module",
                           "test: import graph is acyclic"))
    for f in inv["unreadable"]:
        out.append(finding("unreadable-source", "INTEGRITY_UNEVALUABLE", f, "static structure", "file parses", "file could not be parsed",
                           [f], {"key": f}, "syntax error or unreadable file", "fix the file", "none"))
    for yml, run in workflow_commands(root):
        target = run.split()[1] if len(run.split()) > 1 else None
        if target and target.startswith(("/", "$", "~")):       # a script the workflow writes at run time (e.g. /tmp/x.py), not a repo file
            continue
        if target and target.endswith(".py") and not os.path.exists(os.path.join(root, target)):
            moved = os.path.exists(os.path.join(inv["dir"], os.path.basename(target)))
            if not (moved or full_checkout):                     # a partial copy of the repo cannot say whether the file exists elsewhere
                out.append(finding("workflow-entry-not-in-this-copy", "INTEGRITY_UNEVALUABLE", yml, "static structure",
                                   f"'{run}' points at an existing file", f"{target} is not in this copy of the repo",
                                   [yml, run], {"key": yml + run + "partial"}, "this is not a full checkout, or the file is missing",
                                   "run with --full-checkout from a clean checkout to treat this as a defect", "none", severity="low"))
                continue
            out.append(finding("workflow-entry-missing", "CONTRACT_MISMATCH", yml, "static structure",
                               f"'{run}' points at an existing file", f"{target} does not exist relative to the repo root",
                               [yml, run], {"key": yml + run, "check": "workflow command target"},
                               "a file was moved or deleted without updating the workflow", "update the path in the workflow",
                               "test: every 'python X.py' in the workflows exists"))
    if run_layer:
        chk = os.path.join(root, "check_layer_imports.py")
        if os.path.exists(chk):
            r = subprocess.run([sys.executable, chk, root], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                bad = [l.strip() for l in r.stdout.splitlines() if "✗" in l or "violat" in l.lower()]
                out.append(finding("layer-rules", "CONTRACT_MISMATCH", "check_layer_imports.py", "static structure", "layer checker exits 0",
                                   f"exit {r.returncode}", bad[:10] or r.stdout.splitlines()[-5:], {"key": "layers"},
                                   "a forbidden import was added", "remove the import or change the layer on purpose", "the layer checker itself"))
        else:
            out.append(finding("layer-checker-absent", "INTEGRITY_UNEVALUABLE", "check_layer_imports.py", "static structure",
                               "layer checker present", "not found next to the repo root", [], {"key": "layers-absent"},
                               "run from outside a full checkout", "run from a clean checkout", "none"))
    return out


def workflow_commands(root):
    found = []
    for d in (root, os.path.join(root, ".github", "workflows")):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith((".yml", ".yaml")):
                with open(os.path.join(d, f)) as fh:
                    for line in fh:
                        m = re.search(r"run:\s*(python3?\s+[\w./-]+\.py[^\n]*)", line)
                        if m:
                            found.append((os.path.relpath(os.path.join(d, f), root), re.sub(r"^python3", "python", m.group(1)).strip()))
    return found


# ---------------------------------------------------------------------------------------------------------------------
# B. contracts
def command_table(root, inv):
    cdir = inv["dir"]
    tests_text = {}
    for f in inv["files"]:
        if f["kind"] == "test":
            with open(os.path.join(cdir, f["file"])) as fh:
                tests_text[f["file"]] = fh.read()
    cmds = {}
    for c in bus.ACT_COMMANDS:
        cmds[c] = "act"
    for c in bus.COMMAND_INTENTS:
        cmds.setdefault(c, "intent")
    for c in RUNNER_ONLY:
        cmds.setdefault(c, "runner")
    for c in bus.HELP_COMMANDS:
        cmds.setdefault(c, "help")
    try:
        import set_bot_commands as sbc
        menu = {"/" + c for c, _d in sbc.MENU}
    except Exception:
        menu = set()
    rows = []
    for c, kind in sorted(cmds.items()):
        n = sum(1 for t in tests_text.values() if c in t)
        rows.append({"command": c, "kind": kind, "writes": c in WRITERS, "in_help": c in bus.HELP_TEXT, "in_menu": c in menu,
                     "test_files_mentioning": n})
    return rows


def contract_findings(rows):
    out = []
    aliases = {"/change", "/state", "/bushelp", "/start", "/commands", "/menu"}
    for r in rows:
        if not r["in_help"] and r["command"] not in aliases:
            out.append(finding("command-not-in-help", "CONTRACT_MISMATCH", "bus.py", "routing", f"{r['command']} appears in /help",
                               "answered by the bot but missing from /help", [r["command"]], {"key": r["command"]},
                               "a command was added without updating HELP_TEXT", "add it to HELP_TEXT", "test_bot_menu"))
        if r["test_files_mentioning"] == 0 and r["command"] not in aliases:
            out.append(finding("command-untested", "INTEGRITY_UNEVALUABLE", "tests", "routing", f"{r['command']} named in at least one test",
                               "no test file mentions it", [r["command"]], {"key": "untested" + r["command"]},
                               "never covered", f"add a test for {r['command']}", "the new test", severity="low"))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# C. probes
SETUPS = ("no_state", "fresh", "after_trade", "stale_state")
ARGS = ("", "1", "99", "abc")
PLAIN = ("wah is price doing rn?", "what are u looking at rn", "abeg wetin dey happen", "buh it broke the high tho, im looking for a retracement to support",
         "what happened by 8:30", "what did we talk about tuesday", "and after that?", "what happened", "hmm", "yes", "1", "why?")
TRADE = "/logtrade GBPUSD sell 1.3200 sl 1.3260 tp 1.3100"
MARKET_WORDS = re.compile(r"\b(bullish|bearish)\b|HTF bias is", re.I)
LEAKS = (
    ("unresolved-placeholder", re.compile(r"\{[A-Za-z_][\w.]*\}"), "an unfilled template placeholder"),
    ("python-repr-leak", re.compile(r"\{'|\bNone\b|\bnull\b|<[a-z_.]+ object|\bTraceback\b|\bKeyError\b|\bTypeError\b|\bAttributeError\b"), "Python internals"),
    ("nan-leak", re.compile(r"\bnan\b|\binf\b", re.I), "a raw number-formatting artefact"),
)


class Probe:
    def __init__(self, root, setup):
        self.root, self.setup = root, setup
        self.dir = tempfile.mkdtemp(prefix="comms_probe_")
        self.sent, self.uid = [], 1000
        fix = find_fixture(root)
        self.have_fixture = fix is not None
        self.persisted = None
        if setup != "no_state" and self.have_fixture:
            shutil.copy(fix, os.path.join(self.dir, "state.json"))
            with open(fix) as f:
                self.persisted = br_t(json.load(f)["timestamp"])
        self.now = (self.persisted + timedelta(minutes=2)) if self.persisted else datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
        if setup == "stale_state" and self.persisted:
            self.now = self.persisted + timedelta(hours=3)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def say(self, text):
        self.uid += 1
        with open(os.path.join(self.dir, "inbox.jsonl"), "a") as f:
            f.write(json.dumps({"update_id": self.uid, "chat_id": "1", "text": text, "received_at": self.now.isoformat()}) + "\n")
        before = len(self.sent)
        summary = br.run(self.dir, now=self.now, send=lambda t: self.sent.append(t) or True)
        return self.sent[before:], summary, self.uid

    def records(self):
        recs = ledger.read(os.path.join(self.dir, br.LEDGER_NAME))[0] if os.path.exists(os.path.join(self.dir, br.LEDGER_NAME)) else []
        return recs


def find_fixture(root):
    """fixture_real_state.json may sit in the repo root or inside comms/."""
    for d in (root, os.path.join(root, "comms")):
        p = os.path.join(d, FIXTURE_NAME)
        if os.path.exists(p):
            return p
    return None


def br_t(v):
    return ledger.to_utc(v)


def _case_numbers_logged(recs):
    return {r["payload"].get("case_id") for r in recs if r["event_type"] == "TRADE_LOGGED"}


def check_reply(text, request, setup, recs, replies_for_update, summary_errors):
    """-> list of (rule, category, stage, expected, observed, likely_cause, fix, regression)."""
    hits = []
    if not text or not text.strip():
        hits.append(("empty-reply", "CODE_DEFECT", "final reply", "a non-empty reply", "empty reply", "a handler returned no text",
                     "return a plain sentence", "test: every command replies"))
        return hits
    for rule, rx, what in LEAKS:
        m = rx.search(text)
        if m:
            hits.append((rule, "RESPONSE_LEAK", "response construction", "no internal syntax in a reply", f"{what}: {m.group(0)!r}",
                         "a template or value was not resolved before the reply was built, or a missing value was printed raw",
                         "resolve or omit the value; say plainly that it is not recorded", "test: reply contains no " + rule))
    if request.startswith("/") and "I need a bit more to answer that" in text:
        hits.append(("safety-net-fired", "CONTRACT_MISMATCH", "response construction", "a command with a missing or odd argument gets a specific reply",
                     "the generic 'which trade do you mean?' fallback", "a handler built an answer with an unresolved reference and the audit "
                     "safety net replaced it; the net hides the cause and its wording is about trades", "give this command its own reply for a missing or unrecognised argument",
                     "test: this command, with this argument, gets its own specific reply"))
    if re.search(r"I hit an error", text):
        hits.append(("internal-error-as-chat", "RESPONSE_LEAK", "final reply", "a handled, specific reply", "the generic error reply",
                     "an exception escaped the handler for this input", "handle this input explicitly", "test: this input gets a normal reply"))
    wanted = {f"CASE-{int(n):04d}" for n in re.findall(r"\b(\d{1,4})\b", request)}
    logged = _case_numbers_logged(recs)
    for cid in set(re.findall(r"CASE-\d{4}", text)):
        if cid not in logged and cid not in wanted:
            hits.append(("wrong-case-id", "CODE_DEFECT", "retrieval", "only cases that exist, or the one the user named",
                         f"reply mentions {cid}, which was neither logged nor asked for", "a case number was invented or taken from the wrong place",
                         "derive case ids only from the ledger or the request", "test: reply case ids exist"))
    if setup == "no_state" and request.split(" ", 1)[0] in ("/now", "/thesis", "/watch", "/story", "/changed", "/flip", "/state") and MARKET_WORDS.search(text):
        hits.append(("market-claim-without-state", "AUTHORITY_VIOLATION", "response construction", "no market statement when no engine state exists",
                     "reply states a market view with no state.json present", "an answer was built from something other than recorded evidence",
                     "answer only from claims; otherwise say there is nothing recorded", "test: no-state replies carry no market view"))
    return hits


def evidence_note(text, setup):
    """Not a defect: a reply that honestly says something is missing or stale is the bot doing its job."""
    low = text.lower()
    if "too old to call current" in low or "stale" in low:
        return "STALE_EVIDENCE"
    if re.search(r"not recorded|haven't logged|don't have|nothing (recorded|stored)|no result is recorded|can't answer", low):
        return "MISSING_EVIDENCE"
    return None


def run_probes(root, quick=False, extra_requests=(), only=None, setups_only=None):
    findings, ran, notes = [], 0, {"MISSING_EVIDENCE": 0, "STALE_EVIDENCE": 0}
    cmds = sorted(set(bus.ACT_COMMANDS) | set(bus.COMMAND_INTENTS) | set(RUNNER_ONLY) | set(bus.HELP_COMMANDS))
    skip_args = {"/quiet", "/loud"} if False else set()
    setups = setups_only or (SETUPS if not quick else ("fresh", "after_trade"))
    args = ARGS if not quick else ("", "1", "abc")
    if only:
        cmds = [c for c in cmds if c in only]
    unevaluable = set()
    for setup in setups:
        for c in cmds:
            for a in args:
                req = (c + " " + a).strip()
                if a in skip_args:
                    continue
                reqs = [req]
                findings += _probe_one(root, setup, reqs, req, unevaluable, notes)
                ran += 1
        for p in (() if only else PLAIN) + tuple(extra_requests):
            findings += _probe_one(root, setup, [p], p, unevaluable, notes)
            ran += 1
    for s in sorted(unevaluable):
        findings.append(finding("no-fixture", "INTEGRITY_UNEVALUABLE", "comms_inspector.py", "incoming message",
                                f"{FIXTURE_NAME} available to build the '{s}' situation", "fixture not found",
                                [], {"key": "nofix" + s}, "run outside a full checkout", f"run from a checkout that has {FIXTURE_NAME}", "none"))
    return findings, ran, notes


def _probe_one(root, setup, reqs, label, unevaluable, notes):
    pr = Probe(root, setup)
    out = []
    try:
        if setup != "no_state" and not pr.have_fixture:
            unevaluable.add(setup)
            return out
        if setup == "after_trade":
            pr.say(TRADE)
            pr.say("/reason 1 expected the order block to hold")
        recs_before = pr.records()
        n_before = len(recs_before)
        replies, summary, uid = [], None, None
        for r in reqs:
            replies, summary, uid = pr.say(r)
        recs = pr.records()
        new = recs[n_before:]
        key = f"{setup}|{label}"
        repro = {"key": key, "situation": setup, "setup_messages": ([TRADE, "/reason 1 expected the order block to hold"] if setup == "after_trade" else []),
                 "message": label}
        if len(replies) != 1:
            out.append(finding("reply-count", "CODE_DEFECT", "bus_runner.py", "final reply", "exactly one reply per message",
                               f"{len(replies)} replies", replies[:3], repro, "a handler answered twice or not at all", "answer once",
                               "test: one message, one reply"))
            return out
        text = replies[0]
        for rule, cat, stage, exp, obs, cause, fix, reg in check_reply(text, label, setup, recs, replies, summary):
            module = "bus.py" if stage in ("response construction", "retrieval") else "bus_runner.py"
            out.append(finding(rule, cat, module, stage, exp, obs, [text[:300]], repro, cause, fix, reg))
        note = evidence_note(text, setup)
        if note:
            notes[note] += 1
        cmd = label.split(" ", 1)[0].lower()
        if cmd in bus.COMMAND_INTENTS or cmd in ("/reconstruct",) or cmd in bus.HELP_COMMANDS:
            wrote = [r["event_type"] for r in new if r["event_type"] not in LOG_ONLY]
            if wrote and cmd not in WRITERS:
                out.append(finding("read-command-wrote", "CONTRACT_MISMATCH", "bus_runner.py", "retrieval", f"{cmd} only reads",
                                   f"it appended {sorted(set(wrote))}", wrote[:5], repro, "a read path calls a writer", "remove the write",
                                   "test: read-only commands append nothing but log records"))
        again = pr.say  # second run must not answer again
        before = len(pr.sent)
        br.run(pr.dir, now=pr.now, send=lambda t: pr.sent.append(t) or True)
        if len(pr.sent) != before:
            out.append(finding("duplicate-processing", "CODE_DEFECT", "bus_runner.py", "incoming message", "a message is answered once",
                               "running the runner again answered it again", pr.sent[before:][:2], repro, "the handled-update record was not honoured",
                               "mark updates handled before sending", "test: re-running sends nothing"))
        types = [r["event_type"] for r in recs]
        if types.count("BUS_REPLY") != types.count("MESSAGE_RECEIVED") and cmd not in ("/quiet",):
            out.append(finding("reply-record-mismatch", "CODE_DEFECT", "bus_runner.py", "final reply", "one BUS_REPLY per MESSAGE_RECEIVED",
                               f"{types.count('MESSAGE_RECEIVED')} received vs {types.count('BUS_REPLY')} replies", [], repro,
                               "a message was received but never recorded as answered", "record every answer", "test: counts match"))
    except Exception as e:                                       # the inspector must never crash the CI job
        out.append(finding("probe-crashed", "CODE_DEFECT", "bus_runner.py", "routing", "the runner handles any message without raising",
                           f"{type(e).__name__}: {e}", [repr(e)], {"key": f"{setup}|{label}|crash", "situation": setup, "message": label},
                           "an exception escaped the runner", "catch it where the message is handled", "test: this message does not raise"))
    finally:
        pr.close()
    return out


# ---------------------------------------------------------------------------------------------------------------------
# report
def dedupe(findings):
    seen, out = set(), []
    for f in findings:
        if f["id"] not in seen:
            seen.add(f["id"]); out.append(f)
    return out


def apply_previous(findings, previous):
    prev = {f["id"] for f in (previous or {}).get("findings", [])}
    for f in findings:
        f["status"] = "recurring" if f["id"] in prev else "new"
    cur = {f["id"] for f in findings}
    resolved = [f for f in (previous or {}).get("findings", []) if f["id"] not in cur]
    for f in resolved:
        f["status"] = "resolved"
    return resolved


def build_report(root, quick=False, run_layer=True, previous=None, extra_requests=(), full_checkout=False, only=None, setups_only=None):
    inv = inventory(root)
    rows = command_table(root, inv)
    fs = static_findings(root, inv, run_layer, full_checkout) + contract_findings(rows)
    pf, ran, notes = run_probes(root, quick, extra_requests, only, setups_only)
    fs = sorted(dedupe(fs + pf), key=lambda f: (DEFECT_CATEGORIES.index(f["category"]) if f["category"] in DEFECT_CATEGORIES else 9, f["id"]))
    resolved = apply_previous(fs, previous)
    kinds = {}
    for f in inv["files"]:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    defects = [f for f in fs if f["category"] in DEFECT_CATEGORIES]
    return {"tool": "comms_inspector", "read_only": True, "checked": {"files": len(inv["files"]), "by_kind": kinds, "commands": len(rows),
                                                                    "probes_run": ran, "situations": list(SETUPS if not quick else ("fresh", "after_trade"))},
            "inventory": [{k: v for k, v in f.items()} for f in inv["files"]], "commands": rows,
            "evidence_notes": notes, "findings": fs, "resolved_since_previous": resolved,
            "summary": {"defects": len(defects), "unevaluable": sum(1 for f in fs if f["category"] == "INTEGRITY_UNEVALUABLE"),
                        "by_category": {c: sum(1 for f in fs if f["category"] == c) for c in CATEGORIES if any(f["category"] == c for f in fs)}},
            "limits": ["Market interpretations are not judged.", "Real Telegram delivery is not tested (a fake sender is used).",
                       "Only the listed commands, situations and argument shapes were probed.",
                       "Whether the existing tests are adequate is not decided here.",
                       "No findings means none of these checks fired; it does not certify the bot."]}


def render(rep):
    L = []
    c = rep["checked"]
    L.append("COMMS INTEGRITY INSPECTOR (read-only)")
    L.append(f"Checked {c['files']} files ({', '.join(f'{v} {k}' for k, v in sorted(c['by_kind'].items()))}), {c['commands']} commands, "
             f"{c['probes_run']} probes across {len(c['situations'])} situations.")
    s = rep["summary"]
    L.append(f"Defects: {s['defects']}.  Could not evaluate: {s['unevaluable']}.  "
             f"Honest 'missing' or 'stale' replies seen (not defects): {sum(rep['evidence_notes'].values())}.")
    for f in rep["findings"]:
        L.append("")
        L.append(f"[{f['category']}] {f['id']}  {f['rule']}  ({f['severity']}, {f['status']})")
        L.append(f"  where:    {f['module']} at stage '{f['stage']}'")
        L.append(f"  expected: {f['expected']}")
        L.append(f"  observed: {f['observed']}")
        if f["evidence"]:
            L.append(f"  evidence: {f['evidence'][0] if isinstance(f['evidence'][0], str) else f['evidence'][0]}")
        L.append(f"  repro:    {json.dumps(f['reproduction'])}")
        L.append(f"  cause:    {f['likely_cause']}")
        L.append(f"  fix:      {f['suggested_fix']}")
        L.append(f"  test:     {f['regression_test']}")
    if rep["resolved_since_previous"]:
        L.append("")
        L.append(f"Resolved since the previous report: {', '.join(f['id'] for f in rep['resolved_since_previous'])}")
    L.append("")
    L.append("LIMITS OF THIS REPORT")
    L += ["  - " + x for x in rep["limits"]]
    return "\n".join(L)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quick = "--quick" in argv
    full = "--full-checkout" in argv
    out = argv[argv.index("--json") + 1] if "--json" in argv else None
    prev = None
    if "--previous" in argv:
        try:
            with open(argv[argv.index("--previous") + 1]) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
    skip = {out, argv[argv.index("--previous") + 1] if "--previous" in argv else None, "--json", "--previous", "--quick", "--full-checkout"}
    pos = [a for a in argv if a not in skip]
    root = os.path.abspath(pos[0]) if pos else os.path.dirname(HERE) if os.path.basename(HERE) == "comms" else HERE
    rep = build_report(root, quick=quick, previous=prev, full_checkout=full)
    print(render(rep))
    if out:
        with open(out, "w") as f:
            json.dump(rep, f, indent=2, sort_keys=True, default=str)
    return 1 if rep["summary"]["defects"] else 0


if __name__ == "__main__":
    sys.exit(main())
