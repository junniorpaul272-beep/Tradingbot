"""assemble.py - layer 4, assembly. Calls facts -> interpret -> render -> guard and returns a Report. Never raises."""
import os
import pandas as pd
from . import source as S,  facts as F, interpret as I, render as RD, guard as G
from .contracts import BiasInput, Report, ContractError

NEEDS_BIAS = ("technical", "levels", "now", "thesis_gen", "thesis_1h", "thesis_15m", "thesis_5m")      # the live, intraday reports
REFUSAL = "I can't give you the {kind} read right now: {why}.\nI'd rather say that than guess."


def build(kind, now, bias_in, d5, d15, d1):
    now = pd.Timestamp(now)
    if now.tzinfo is None: now = now.tz_localize("UTC")
    try:
        if kind in NEEDS_BIAS and bool(F.market_closed(pd.DatetimeIndex([now.tz_convert("UTC")]))[0]):
            raise F.FactError("the market is closed. It reopens Sunday 22:00 UTC (23:00 Lagos)")
        if kind in ("technical", "levels", "now"):
            if not isinstance(bias_in, BiasInput): raise F.FactError("the 1H bias isn't available")
            t1, t15, t5 = F.tf_facts(d1, "1H", now), F.tf_facts(d15, "15M", now), F.tf_facts(d5, "5M", now)
            b = F.bias_facts(bias_in, d1, now, t1)
            obs = {"1H": I.observe(t1), "15M": I.observe(t15), "5M": I.observe(t5)}
            levels = I.select_levels(b, t15, t5.close, detail=(kind == "levels"))
            if kind == "now":
                st = F.story_facts(d5, now, F.STORY_MIN, d15, d1)
                inter = [F.interaction(d5, now, it.price, st.minutes) for it in levels]
                text, r, trace = RD.now_report(now.to_pydatetime(), b, t1, t15, t5, obs, levels, st, I.label_story(st), inter)
            else:
                text, r, trace = RD.technical(now.to_pydatetime(), b, t1, t15, t5, obs, levels)
            newest = t5.as_of
        elif kind == "thesis":
            text, r, trace = RD.thesis_menu(now.to_pydatetime()); newest = now.to_pydatetime()
        elif kind in ("thesis_gen", "thesis_1h", "thesis_15m", "thesis_5m"):
            if not isinstance(bias_in, BiasInput): raise F.FactError("the 1H bias isn't available")
            t1, t15, t5 = F.tf_facts(d1, "1H", now), F.tf_facts(d15, "15M", now), F.tf_facts(d5, "5M", now)
            b = F.bias_facts(bias_in, d1, now, t1)
            obs = {"1H": I.observe(t1), "15M": I.observe(t15), "5M": I.observe(t5)}
            if kind == "thesis_gen":
                levels = I.select_levels(b, t15, t5.close, detail=False)
                text, r, trace = RD.technical(now.to_pydatetime(), b, t1, t15, t5, obs, levels)
            else:
                tf = kind.split("_")[1].upper(); f = {"1H": t1, "15M": t15, "5M": t5}[tf]
                text, r, trace = RD.thesis_tf(now.to_pydatetime(), b, f, *obs[tf], tf)
            newest = t5.as_of
        elif kind == "quantitative":
            m = F.measure_facts(d1, now); lab = I.label_measures(m)
            text, r, trace = RD.measures(now.to_pydatetime(), m, lab, I.plain_labels(m)); newest = m.as_of
        else:
            return Report(kind, (REFUSAL.format(kind=kind, why="that report doesn't exist"),), (), "refused")
        G.check(text, r, trace)
        age = now.to_pydatetime() - newest
        if age.total_seconds() > 4 * 3600:
            text = f"<i>Heads up: the newest candles are {int(age.total_seconds() // 3600)} hours old (market closed or data delayed).</i>\n\n" + text
        return Report(kind, (text,), tuple(trace), "ok")
    except (F.FactError, ContractError) as e:
        return Report(kind, (REFUSAL.format(kind=kind, why=str(e)),), (), "refused")
    except G.GuardError as e:
        return Report(kind, (REFUSAL.format(kind=kind, why="the wording check failed, so it was held back"),), (), "guard_failed:" + str(e)[:300])
    except Exception as e:  # noqa: BLE001
        return Report(kind, (REFUSAL.format(kind=kind, why="an internal check failed"),), (), "error:" + repr(e)[:150])


def build_from_repo(kind, now, state, repo_dir="."):
    """The one call the scanner will make (Stage B): kind, the time of the request, state.json's contents, the folder holding the candle logs.
    Never raises; any trouble reading or validating becomes a plain refusal."""
    try:
        d5, d15, d1 = (S.read_candles(os.path.join(repo_dir, f"ohlc_{n}_log.jsonl")) for n in ("5m", "15m", "1h"))
        bias = S.bias_from_state(state, now) if kind in NEEDS_BIAS else None
    except ContractError as e:
        return Report(kind, (REFUSAL.format(kind=kind, why=str(e)),), (), "refused")
    except Exception as e:  # noqa: BLE001
        return Report(kind, (REFUSAL.format(kind=kind, why="the data couldn't be read"),), (), "error:" + repr(e)[:150])
    return build(kind, now, bias, d5, d15, d1)
