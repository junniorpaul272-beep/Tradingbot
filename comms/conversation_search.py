"""
conversation_search.py — "what did we talk about on Tuesday?" Pure: ledger + stdlib. Reads records it is handed.

WHAT IT CAN SEARCH: the exchanges the ledger holds: each message you sent that the bot handled (MESSAGE_RECEIVED) paired
with the reply it stored (BUS_REPLY). Nothing else exists to search, so coverage is always reported: the first stored
message, and the plain fact that conversation before that (or handled by the scanner's older commands) is not here.

TOPICS ARE GROUNDED: a topic is a family of the commands/questions that were actually used (a trade, your notes, research
questions, alerts, the market reads...), labelled with a short verbatim quote of what YOU wrote. Nothing is summarised or
interpreted, and a quote is your own words cut to length, never reworded.
"""
import re
from datetime import timedelta

import ledger
import timeref

NOISE = ("/help", "/start", "/commands", "/menu", "/bushelp")
_MARKET = ("/now", "/state", "/thesis", "/watch", "/changed", "/change", "/flip", "/story", "/why", "/health")
STOP = set("the a an and or but of to in on at for with about that this what was were did we you i me my our is are it its thing "
           "talk talked discuss discussed said say tell told remind when then than".split())


def _first(text):
    return (text or "").strip().split(" ", 1)[0].lower().split("@")[0]


def exchanges(records, start=None, end=None, exclude=()):
    msgs, replies = {}, {}
    for r in records or []:
        p = r.get("payload") or {}
        if r.get("event_type") == "MESSAGE_RECEIVED" and isinstance(p.get("update_id"), int):
            msgs[p["update_id"]] = r
        elif r.get("event_type") == "BUS_REPLY" and isinstance(p.get("update_id"), int):
            replies[p["update_id"]] = r
    out = []
    for uid, m in msgs.items():
        t = _t(m.get("ts"))
        if uid in exclude or t is None or (start is not None and t < start) or (end is not None and t >= end):
            continue
        text = (m["payload"].get("text") or "")
        if _first(text) in NOISE:
            continue
        rep = replies.get(uid)
        out.append({"uid": uid, "at": t, "recorded_at": _t(m.get("recorded_at")), "user": text,
                    "reply": (rep["payload"].get("text") if rep else None),
                    "reply_kind": (rep["payload"].get("kind") if rep else None)})
    out.sort(key=lambda x: (x["at"], x["uid"]))
    return out


def _t(v):
    try:
        return ledger.to_utc(v)
    except Exception:
        return None


def topic_of(ex):
    cmd = _first(ex["user"])
    case = re.search(r"CASE-(\d{4})", (ex["reply"] or "") + " " + ex["user"], re.I)
    if cmd in ("/logtrade", "/reason", "/outcome", "/review", "/botvsme", "/reconstruct", "/investigate", "/taken") and case:
        n = int(case.group(1))
        return f"case:{n}", f"your trade CASE-{n:04d}"
    if cmd == "/know" or ex.get("reply_kind") == "saved_note":
        return "notes", "the notes you gave me"
    if cmd in ("/ask", "/define", "/question", "/questions"):
        return "research", "your research questions"
    if cmd in ("/alert", "/alerts", "/thread"):
        return "alerts", "your price alerts"
    if cmd in ("/quiet", "/loud"):
        return "quiet", "quiet mode"
    if cmd in ("/taken", "/skip"):
        return "signals", "a fired signal"
    if cmd in _MARKET or ex.get("reply_kind") == "answer":
        return "market", "what I was seeing in the market"
    return "other", "other chat"


def topics(exs):
    by = {}
    for ex in exs:
        k, label = topic_of(ex)
        d = by.setdefault(k, {"key": k, "label": label, "count": 0, "first_at": ex["at"], "last_at": ex["at"], "example": None})
        d["count"] += 1
        d["last_at"] = ex["at"]
        if d["example"] is None and not ex["user"].startswith("/"):
            d["example"] = quote(ex["user"])
        if d["example"] is None and k in ("notes", "research", "market", "alerts"):
            d["example"] = quote(re.sub(r"^/\w+(@\w+)?\s*", "", ex["user"]) or ex["user"])
    return sorted(by.values(), key=lambda d: (-d["count"], d["first_at"]))


def quote(text, n=60):
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def options(tops, limit=3):
    """The small set of topics worth offering: real ones first; 'other chat' only when nothing else exists."""
    real = [t for t in tops if t["key"] != "other"]
    return (real or tops)[:limit]


def find_by_words(exs, text):
    """Topics whose messages mention what the user named ('the protected high'). Matching is on the words you used, nothing smarter."""
    words = [w for w in re.findall(r"[a-z']{4,}", (text or "").lower()) if w not in STOP]
    if not words:
        return []
    hits = [ex for ex in exs if any(w in (ex["user"] + " " + (ex["reply"] or "")).lower() for w in words)]
    return topics(hits)


def replay(exs, key, max_chars=3200):
    lines, used = [], 0
    sel = [e for e in exs if topic_of(e)[0] == key]
    for e in sel:
        reply = (e["reply"] or "").strip().split("\n")[0]
        reply = reply if len(reply) <= 170 else reply[:170].rstrip() + "…"
        block = f"{timeref.hm(e['at'])}  you: {quote(e['user'], 220)}" + (f"\n       me: {reply}" if reply else "\n       me: (no reply stored)")
        if used + len(block) > max_chars:
            lines.append(f"…and {len(sel) - len(lines)} more exchange(s) on this topic.")
            break
        lines.append(block)
        used += len(block)
    return lines, len(sel)


def coverage(records):
    ts = [x for x in (_t(r.get("ts")) for r in records or [] if r.get("event_type") == "MESSAGE_RECEIVED") if x]
    return {"first": min(ts) if ts else None, "last": max(ts) if ts else None, "messages": len(ts)}


def _cov_line(cov):
    if not cov["first"]:
        return "I have no stored messages yet, so there's no conversation to look back over."
    return (f"I can only look back over messages I stored, and the first of those is from {timeref.lagos(cov['first']):%a %d %b}. "
            "Anything before that, or handled by the scanner's older commands, isn't here.")


def render_options(tops, cov, window_label):
    """The 'which one do you mean?' message. Each option is a real topic with a short quote of your own words."""
    if not tops:
        return f"I don't have any conversation stored for {window_label}. " + _cov_line(cov)
    if len(tops) == 1:
        t = tops[0]
        q = f" (you wrote \"{t['example']}\")" if t["example"] else ""
        return f"On {window_label} the only thing we covered was {t['label']}{q}. Want me to take you through it?"
    parts = []
    for i, t in enumerate(tops, 1):
        q = f", like when you wrote \"{t['example']}\"" if t["example"] else ""
        parts.append(f"{i}. {t['label']} ({t['count']} message{'s' if t['count'] != 1 else ''}{q})")
    return f"We covered a few things on {window_label}:\n" + "\n".join(parts) + "\nWhich one do you want me to take you back through?"


def render_replay(label, lines, total, cov, window_label):
    head = f"Here's {label} from {window_label}, in order ({total} exchange{'s' if total != 1 else ''}; my replies are shortened to their first line):"
    return head + "\n\n" + "\n".join(lines) + "\n\n" + _cov_line(cov)
