"""
btc_bank.py: the Bank. Account settings, position sizing and an append-only ledger. No wording, no network.

Design rules (poka-yoke: a wrong input should be impossible, not just discouraged):
  * Accounts live in their OWN settings file (btc_accounts.json). At most MAX_ACCOUNTS (5). An unknown key
    (a typo such as "risk_precent") is an error, never silently ignored; every number has a sane range.
  * Position size = (balance x risk%) / (distance from entry to the active exit x contract size), rounded DOWN to
    the lot step, then limited by the account's max lot and its margin cap. If even the minimum lot would risk
    more than the account allows, the trade is SKIPPED and says why (or rounded up, if the account says so).
  * The ledger (bank_ledger.jsonl) is append-only. Balances and open positions are never stored anywhere else:
    they are re-derived by replaying the ledger, and every posting has a unique id, so replaying or retrying an
    event can never post it twice.
  * Balance is REALISED balance (closed trades only). P&L uses the signal's idealised fills (band, open, stop)
    and includes no spread, commission, swap or slippage, so real demo results will be a little worse.

The contract size, lot step and minimum lot defaults below are a guess at a typical MT5 BTCUSD symbol
(1 lot = 1 BTC, 0.01 minimum). Check them against your broker's symbol specification and set them per account.
"""
import json
import math
import os
from datetime import datetime, timezone

MAX_ACCOUNTS = 5
MAX_RISK_PCT = 10.0

_REQUIRED = {"name", "starting_balance"}
_DEFAULTS = {
    "enabled": True,
    "risk_pct": 1.0,          # % of the balance risked per trade
    "contract_size": 1.0,     # BTC per 1.00 lot
    "min_lot": 0.01,
    "max_lot": 100.0,
    "lot_step": 0.01,
    "leverage": 100.0,
    "max_margin_pct": 50.0,   # at most this % of the balance may be used as margin
    "sides": "both",          # both | long | short
    "min_lot_policy": "skip", # skip | round_up
}
_KNOWN = _REQUIRED | set(_DEFAULTS)


class AccountsError(ValueError):
    pass


# --------------------------------------------------------------------------- settings file
def _num(acct, key, lo, hi, lo_open=False):
    v = acct[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise AccountsError(f"account '{acct['name']}': {key} must be a number, got {v!r}")
    ok_lo = v > lo if lo_open else v >= lo
    if not (ok_lo and v <= hi):
        raise AccountsError(f"account '{acct['name']}': {key} must be {'above' if lo_open else 'at least'} {lo:g} "
                            f"and at most {hi:g}, got {v:g}")
    return float(v)


def validate_accounts(raw):
    """Return the list of validated account dicts (defaults filled in) or raise AccountsError."""
    if not isinstance(raw, dict) or not isinstance(raw.get("accounts"), list):
        raise AccountsError('the file must look like {"accounts": [ {...}, {...} ]}')
    items = raw["accounts"]
    if len(items) > MAX_ACCOUNTS:
        raise AccountsError(f"{len(items)} accounts listed; the maximum is {MAX_ACCOUNTS}")
    out, seen = [], set()
    for i, a in enumerate(items, 1):
        if not isinstance(a, dict):
            raise AccountsError(f"account #{i} must be an object")
        missing = _REQUIRED - set(a)
        if missing:
            raise AccountsError(f"account #{i} is missing: {', '.join(sorted(missing))}")
        unknown = set(a) - _KNOWN
        if unknown:
            raise AccountsError(f"account '{a.get('name', i)}': unknown setting(s) {', '.join(sorted(unknown))} "
                                f"(allowed: {', '.join(sorted(_KNOWN))})")
        if not isinstance(a["name"], str) or not a["name"].strip() or ":" in a["name"]:
            raise AccountsError(f"account #{i}: name must be non-empty text without a colon")
        if a["name"].strip().lower() in seen:
            raise AccountsError(f"account name '{a['name']}' is used twice")
        seen.add(a["name"].strip().lower())
        c = {**_DEFAULTS, **a, "name": a["name"].strip()}
        c["starting_balance"] = _num(c, "starting_balance", 0, 1e9, lo_open=True)
        c["risk_pct"] = _num(c, "risk_pct", 0, MAX_RISK_PCT, lo_open=True)
        c["contract_size"] = _num(c, "contract_size", 0, 1e6, lo_open=True)
        c["min_lot"] = _num(c, "min_lot", 0, 1e6, lo_open=True)
        c["max_lot"] = _num(c, "max_lot", 0, 1e6, lo_open=True)
        c["lot_step"] = _num(c, "lot_step", 0, 1e6, lo_open=True)
        c["leverage"] = _num(c, "leverage", 1, 1e4)
        c["max_margin_pct"] = _num(c, "max_margin_pct", 0, 100, lo_open=True)
        if c["max_lot"] < c["min_lot"]:
            raise AccountsError(f"account '{c['name']}': max_lot is below min_lot")
        k = c["min_lot"] / c["lot_step"]
        if abs(k - round(k)) > 1e-6:
            raise AccountsError(f"account '{c['name']}': min_lot ({c['min_lot']:g}) must be a whole number of "
                                f"lot_step ({c['lot_step']:g}), as brokers have it")
        if not isinstance(c["enabled"], bool):
            raise AccountsError(f"account '{c['name']}': enabled must be true or false")
        if c["sides"] not in ("both", "long", "short"):
            raise AccountsError(f"account '{c['name']}': sides must be both, long or short")
        if c["min_lot_policy"] not in ("skip", "round_up"):
            raise AccountsError(f"account '{c['name']}': min_lot_policy must be skip or round_up")
        out.append(c)
    return out


def load_accounts(path):
    """-> (accounts, problem). A missing file is not a problem (the bot just gives signals without sizing).
    An invalid file returns ([], message) so the alerts still go out and say what is wrong."""
    if not os.path.exists(path):
        return [], None
    try:
        with open(path) as f:
            return validate_accounts(json.load(f)), None
    except json.JSONDecodeError as e:
        return [], f"{os.path.basename(path)} is not valid JSON ({e})"
    except AccountsError as e:
        return [], f"{os.path.basename(path)}: {e}"


# --------------------------------------------------------------------------- sizing (pure)
def _floor_to(x, step):
    return round(math.floor(x / step + 1e-9) * step, 8)


def size_position(acct, balance, side, entry, stop):
    """Pure sizing. Returns a dict with status, lots, actual_risk, actual_risk_pct, balance, fmt.
    Statuses: OK, MAX_LOT, MARGIN_LIMITED, MIN_LOT_ROUNDED_UP (all open a trade) and
              SIDE_OFF, NO_BALANCE, NO_DISTANCE, BELOW_MIN, MARGIN_TOO_SMALL (all skip)."""
    cs, step = acct["contract_size"], acct["lot_step"]
    res = {"status": None, "lots": 0.0, "actual_risk": 0.0, "actual_risk_pct": 0.0, "balance": balance,
           "fmt": {"side": side.lower(), "risk_pct": acct["risk_pct"], "min_lot": acct["min_lot"], "min_risk_pct": 0.0}}
    if acct["sides"] != "both" and acct["sides"] != side.lower():
        res["status"] = "SIDE_OFF"
        return res
    if balance <= 0:
        res["status"] = "NO_BALANCE"
        return res
    dist = abs(entry - stop)
    if dist <= 0:
        res["status"] = "NO_DISTANCE"
        return res
    per_lot = dist * cs                                  # USD lost per 1.00 lot if the exit is hit
    res["fmt"]["min_risk_pct"] = acct["min_lot"] * per_lot / balance * 100
    lots = _floor_to(balance * acct["risk_pct"] / 100 / per_lot, step)
    status = "OK"
    if lots > acct["max_lot"]:
        lots, status = _floor_to(acct["max_lot"], step), "MAX_LOT"
    margin_cap = _floor_to(balance * acct["max_margin_pct"] / 100 * acct["leverage"] / (entry * cs), step)
    if lots > margin_cap:
        lots, status = margin_cap, "MARGIN_LIMITED"
    if lots < acct["min_lot"]:
        if acct["min_lot"] > margin_cap:
            res["status"] = "MARGIN_TOO_SMALL"
            return res
        if acct["min_lot_policy"] == "round_up":
            lots, status = acct["min_lot"], "MIN_LOT_ROUNDED_UP"
        else:
            res["status"] = "BELOW_MIN"
            return res
    risk = lots * per_lot
    res.update(status=status, lots=lots, actual_risk=risk, actual_risk_pct=risk / balance * 100)
    return res


# --------------------------------------------------------------------------- the ledger
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Bank:
    """Accounts + ledger. `path=None` keeps everything in memory (backtests); `write=False` replays an existing
    ledger but never writes (dry runs)."""

    def __init__(self, accounts, path=None, write=True):
        self.accounts = accounts
        self.path = path
        self.write = write and path is not None
        self.entries, self.by_id = [], {}
        self.balances, self.start, self.open = {}, {}, {}
        self.counts = {}  # name -> {"trades": n, "wins": n, "skips": n}
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._apply(json.loads(line))

    # -- state is derived from entries only
    def _apply(self, e):
        self.entries.append(e)
        self.by_id[e["id"]] = e
        name, t = e.get("account"), e["type"]
        c = self.counts.setdefault(name, {"trades": 0, "wins": 0, "skips": 0})
        if t == "account_opened":
            self.balances[name] = self.start[name] = e["balance"]
        elif t == "open":
            self.open[(name, e["side"])] = e
        elif t == "close":
            self.balances[name] = e["balance_after"]
            self.open.pop((name, e["side"]), None)
            c["trades"] += 1
            c["wins"] += 1 if e["pnl"] > 0 else 0
        elif t == "skip":
            c["skips"] += 1

    def _post(self, e):
        if e["id"] in self.by_id:
            return
        e = {**e, "ts": _now()}
        if self.write:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        self._apply(e)

    def ensure_accounts(self):
        """Open any account that is in the settings file but not yet in the ledger (starting balance applies once)."""
        for a in self.accounts:
            self._post({"id": f"account:{a['name']}", "type": "account_opened", "account": a["name"],
                        "balance": a["starting_balance"]})

    # -- planning is side-effect free; commit() writes
    def plan(self, ev):
        """For one engine event: (results, entries). Nothing is written until commit(entries)."""
        results, entries = [], []
        if ev.kind in ("ENTRY", "REENTRY"):
            for a in self.accounts:
                if not a["enabled"]:
                    continue
                name = a["name"]
                oid, sid = f"open:{name}:{ev.event_id}", f"skip:{name}:{ev.event_id}"
                done = self.by_id.get(oid) or self.by_id.get(sid)
                if done:
                    results.append(done["result"])
                    continue
                if name not in self.balances:
                    continue  # not opened in the ledger yet
                if (name, ev.side) in self.open:
                    r = {"action": "skip", "account": name, "side": ev.side, "status": "ALREADY_OPEN", "fmt": {}}
                    entries.append({"id": sid, "type": "skip", "account": name, "event_id": ev.event_id, "result": r})
                    results.append(r)
                    continue
                s = size_position(a, self.balances[name], ev.side, ev.price, ev.active_stop)
                if s["lots"] > 0:
                    r = {"action": "open", "account": name, "side": ev.side, **{k: s[k] for k in (
                        "status", "lots", "actual_risk", "actual_risk_pct", "balance")}}
                    entries.append({"id": oid, "type": "open", "account": name, "side": ev.side,
                                    "event_id": ev.event_id, "kind": ev.kind, "bar_time": ev.t, "lots": s["lots"],
                                    "entry": ev.price, "stop": ev.active_stop, "contract_size": a["contract_size"],
                                    "risk": s["actual_risk"], "balance_before": s["balance"], "result": r})
                else:
                    r = {"action": "skip", "account": name, "side": ev.side, "status": s["status"], "fmt": s["fmt"]}
                    entries.append({"id": sid, "type": "skip", "account": name, "event_id": ev.event_id, "result": r})
                results.append(r)
        elif ev.kind in ("CLOSE", "EXIT") and not ev.flat:
            for (name, side), pos in list(self.open.items()):
                if side != ev.side:
                    continue
                cid = f"close:{name}:{ev.event_id}"
                if cid in self.by_id:
                    results.append(self.by_id[cid]["result"])
                    continue
                d = 1 if side == "LONG" else -1
                pnl = d * (ev.price - pos["entry"]) * pos["lots"] * pos["contract_size"]
                bal = self.balances[name] + pnl
                r = {"action": "close", "account": name, "side": side, "pnl": pnl, "balance": bal,
                     "lots": pos["lots"]}
                entries.append({"id": cid, "type": "close", "account": name, "side": side, "event_id": ev.event_id,
                                "kind": ev.kind, "bar_time": ev.t, "lots": pos["lots"], "entry": pos["entry"],
                                "exit": ev.price, "pnl": pnl, "balance_after": bal, "result": r,
                                # self-contained, so statistics can be built from close entries alone
                                "stop": pos["stop"], "risk": pos["risk"], "open_bar_time": pos["bar_time"],
                                "open_event_id": pos["event_id"], "contract_size": pos["contract_size"]})
                results.append(r)
        return results, entries

    def commit(self, entries):
        for e in entries:
            self._post(e)

    # -- views
    def closed_trades(self, account=None):
        """Every closed position as a flat dict (oldest first), ready for btc_stats. `account` filters one account."""
        out = []
        for e in self.entries:
            if e["type"] != "close" or (account and e["account"] != account):
                continue
            out.append({"account": e["account"], "side": e["side"], "open_t": e["open_bar_time"],
                        "close_t": e["bar_time"], "lots": e["lots"], "entry": e["entry"], "exit": e["exit"],
                        "stop": e["stop"], "risk": e["risk"], "pnl": e["pnl"], "balance_after": e["balance_after"],
                        "close_kind": e["kind"], "event_id": e["open_event_id"]})
        return out

    def skip_reasons(self):
        """{account: {status: count}} for every signal an account skipped."""
        out = {}
        for e in self.entries:
            if e["type"] == "skip":
                d = out.setdefault(e["account"], {})
                d[e["result"]["status"]] = d.get(e["result"]["status"], 0) + 1
        return out

    def summary(self):
        reasons = self.skip_reasons()
        out = []
        for a in self.accounts:
            n = a["name"]
            if n not in self.balances:
                continue
            c = self.counts.get(n, {"trades": 0, "wins": 0, "skips": 0})
            out.append({"name": n, "enabled": a["enabled"], "start": self.start[n], "balance": self.balances[n],
                        "return_pct": (self.balances[n] / self.start[n] - 1) * 100, **c,
                        "skip_reasons": reasons.get(n, {}),
                        "open": [p for (nm, _), p in self.open.items() if nm == n]})
        return out

    def warnings(self, price, atr, atr_mult=1.0):
        """Plain-language warnings for accounts that would skip even a TIGHT exit (1 ATR away), i.e. accounts that will
        skip almost every signal. Accounts that only skip the signals with far-away exits are not warned: their
        partial skipping is reported in the stats ('could only take N of M'). Information only; nothing changes."""
        if not price or not atr:
            return []
        out = []
        for a in self.accounts:
            if not a["enabled"]:
                continue
            bal = self.balances.get(a["name"], a["starting_balance"])
            sides = ("LONG", "SHORT") if a["sides"] == "both" else (a["sides"].upper(),)
            for side in sides[:1]:                        # the distance is the same for both sides
                dist = atr * atr_mult
                r = size_position(a, bal, side, price, price - dist if side == "LONG" else price + dist)
                if r["status"] == "BELOW_MIN":
                    need = r["fmt"]["min_risk_pct"]
                    head = (f"WARNING {a['name']}: will skip almost every BTC signal. The smallest lot "
                            f"({a['min_lot']:g}) would risk {need:.1f}% of {bal:,.0f} USD even with a tight exit, but "
                            f"risk_pct is {a['risk_pct']:g}%. ")
                    if need > MAX_RISK_PCT:
                        out.append(head + f"No risk setting can fix this (the limit is {MAX_RISK_PCT:g}%). In "
                                   f"accounts.json, set min_lot (and contract_size) to what your broker really offers "
                                   f"for BTC, or give the account a bigger balance.")
                    else:
                        out.append(head + f"In accounts.json, raise risk_pct to about {need:.1f}%, or lower min_lot.")
                elif r["status"] == "MARGIN_TOO_SMALL":
                    out.append(f"WARNING {a['name']}: will skip almost every BTC signal. The margin cap "
                               f"({a['max_margin_pct']:g}% at {a['leverage']:g}x) leaves less than the minimum lot "
                               f"({a['min_lot']:g}). Raise leverage or max_margin_pct, or lower min_lot, in accounts.json.")
        return out

    def summary_lines(self):
        if not self.accounts:
            return []
        lines = ["Demo accounts"]
        for s in self.summary():
            pos = ", ".join(f"{p['side'].lower()} {p['lots']:g} lots from {p['entry']:,.0f}" for p in s["open"])
            off = " (switched off)" if not s["enabled"] else ""
            lines.append(f"{s['name']}{off}: {s['balance']:,.0f} USD ({s['return_pct']:+.1f}%), "
                         f"{s['trades']} closed, {s['skips']} skipped" + (f", open: {pos}" if pos else ""))
        return lines
        
