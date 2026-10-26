"""
plainwords.py — the engine's closed vocabulary, in ordinary words. Pure stdlib; imports nothing from this codebase; holds no facts.

WHY IT EXISTS: the status answer used to print engine labels as they are ("AGING_CAMPAIGN", "TIER_3_STRUCTURE", "Leg maturity (engine
label)"). A beginner cannot read that. Each entry below is a REGISTERED MEANING for one code the engine can emit, written to say no more
than the engine's own definition says (the engine's wording is in scanner_observation.py next to each code).

THE RULES THAT KEEP IT HONEST
  * No prices, times of day, counts, or directions are ever written here: those can only come from a recorded claim. (The only digits
    allowed are the names of timeframes, "15-minute" and "1-hour"; a test checks that.)
  * A meaning explains what the engine is saying, never what price will do. No advice, no prediction.
  * A code with no registered meaning is NOT guessed at: plain() returns None and the caller says the engine raised a warning it has no
    plain description for yet, and quotes the engine's own sentence. A test fails if a code the engine defines has no entry here.
"""
import re

# CautionCode (scanner_observation.CautionCode): things the engine is deliberately NOT interested in right now.
CAUTIONS = {
    "AGING_CAMPAIGN": "this move has already gone on for a long time, so buying or selling it outright now is the kind of trade the engine is wary of",
    "UNSWEPT_CHOCH": "the engine sees a change of character on the 15-minute chart, but no liquidity sweep came before it, "
                     "so it is not treating it as strong proof of a reversal",
    "ATR_INCOMPATIBLE": "price is moving less than usual right now, so a stop wide enough to be sensible would be big compared with the normal movement",
    "CHASING_MID_RANGE": "price is in the middle of its range, away from any zone the engine cares about, so chasing it here is not something the engine likes",
}

# WatchCode (scanner_observation.WatchCode): what the engine says it is waiting to see.
WATCH = {
    "PULLBACK_TO_OB": "price pulling back into an order block (a zone where it may react)",
    "PULLBACK_TO_FIB_POCKET": "price pulling back into the Fibonacci pullback zone of the current leg, where the engine looks for a rejection candle",
    "SWEEP_AND_RECLAIM": "price sweeping the nearest untaken high or low and then coming back, before any reversal is treated as meaningful",
    "FRESH_DISPLACEMENT": "a new break of structure in the current direction, to confirm the move is still healthy",
    "FIVE_M_ALIGNMENT": "the 5-minute chart lining up with the 15-minute read before any reaction is treated as confirmed",
}

# Market phase (scanner_observation.Phase)
PHASES = {
    "expansion": "pushing in one direction with a new leg",
    "exhaustion": "still pushing the same way, but the move has run for a while and looks stretched",
    "transition": "without a confirmed leg, so the engine has no clear direction",
    "manipulation": "sweeping past a recent high or low and coming back, with no clear direction",
}

# Leg age labels the engine uses (market_leg_age_status.status), lower-cased with underscores turned into spaces.
LEG_AGE = {
    "fresh": "the current leg is young",
    "developing": "the current leg is still developing",
    "mature": "the current leg is getting old",
    "mature and extended": "the current leg is old and stretched",
    "extended": "the current leg is stretched",
    "aging": "the current leg is ageing",
    "stale": "the current leg looks stale",
}

# Failure risk bands (thesis.failure_risk.level)
RISK = {
    "low": "the engine sees little sign that this move is failing",
    "medium": "the engine sees some signs that this move could fail",
    "moderate": "the engine sees some signs that this move could fail",
    "high": "the engine sees real signs that this move could fail",
}

# Volatility hint (volatility.state)
VOLATILITY = {
    "expanding": "moves are getting bigger",
    "contracting": "moves are getting smaller",
    "flat": "moves are staying about the same size",
    "normal": "moves are a normal size",
}

# Plain definitions of the trading terms the bot uses, for a beginner. Definitions only: no market facts.
TERMS = {
    "choch": "CHoCH (change of character) is when price breaks past the level that was holding the trend, which can be the first sign the trend is changing",
    "bos": "BOS (break of structure) is when price pushes past the last important high or low in the direction it was already going",
    "fvg": "FVG (fair value gap) is a gap left by a fast move, where price barely traded; some traders watch it as an area price may come back to",
    "order block": "an order block is the last candle before a strong move; some traders watch it as an area price may react to",
    "liquidity sweep": "a liquidity sweep is when price pokes past a recent high or low, often grabbing stop orders, and then comes back",
    "htf": "HTF means higher timeframe, such as the 1-hour chart, which shows the bigger picture",
    "bias": "bias is the direction the engine thinks the bigger picture is leaning",
    "atr": "ATR is a measure of how far price normally moves; low ATR means quiet",
    "phase": "phase is the engine's label for what stage the current move is in",
    "displacement": "displacement is a strong, fast move in one direction that leaves little overlap behind it; traders read it as a sign of real commitment",
    "protected level": "a protected high or low is a swing point that, if broken, would change the structure; traders watch it as the level that has to hold",
    "retracement": "a retracement is a pullback against the main move, before price carries on or turns",
    "exhaustion": "exhaustion means a move has gone on a long time and may be running out of steam; it does not mean it has reversed",
}

# What the engine SAVES for each concept, so "do you track X?" gets an honest answer. Written from what scanner_live / scanner_observation
# actually write to disk; no facts about the market, and a concept the engine does not record says so.
RECORDED = {
    "choch": "Every scan the engine saves a yes/no flag saying whether its current 15-minute leg began with a change of character (and the same for the "
             "5-minute), along with that leg's direction, and the direction of the 1-hour leg. The flag describes the current leg, so it stays on while the leg "
             "stands. The exact candle isn't saved, only the scan that first saw it. There is no separate 1-hour change-of-character flag.",
    "bos": "Every scan the engine saves the direction of its latest 15-minute break, whether that was a break of structure or a change of character. I can tell "
           "you when that direction changed, but a break that carried on the same way isn't saved as its own event.",
    "sweep": "Every scan the engine saves the result of its sweep check against the current swing high and swing low (price poked past the level and closed "
             "back, judged in the direction of the bias), and how far. It does not save which level it was, or a list of every sweep, so I can only say when "
             "the check first came up true.",
    "fvg": "The engine saves each 15-minute fair value gap once, when it first sees it, with its prices and direction, and each scan notes whether price is inside one.",
    "ob": "The engine saves each 15-minute order block once, when it first sees it, with its prices and direction, and each scan notes whether price is inside one "
          "and whether it has been mitigated.",
    "displacement": "No. The engine does not measure or save displacement. The nearest thing is that it watches for a fresh break of structure in the current "
                    "direction, but it keeps no history of one, so I can't tell you when displacement happened.",
    "protected level": "No. The engine does not label or save protected highs or lows. It keeps the current swing high and low as the edges of its range, "
                       "but nothing says which of them is protected, so I can't tell you.",
    "retracement": "Only the current one: the engine works out the Fibonacci pullback zone of the current leg and notes each scan whether price is inside it. "
                   "It does not keep a history of past retracements.",
    "htf": "Every scan the engine's reading of the bigger picture is saved, including the bias and the current leg, and snapshots of it are kept over time.",
    "bias": "The engine's bias is saved with each snapshot of its reading, so I can say what it was at a given time.",
    "phase": "The engine saves its market phase each scan, along with what caused the last change of phase.",
}

# The engine's own display names for its tiers (scanner_observation.TIER_DISPLAY_NAMES). A test fails if the two ever differ.
TIERS = {
    "TIER_1_POI": "Tier 1 (Order Block reaction)",
    "TIER_2_FIB": "Tier 2 (HTF Fib pullback)",
    "TIER_3_STRUCTURE": "Tier 3 (CHoCH structure)",
}

_CODE = re.compile(r"[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+")


def plain(code):
    """-> the registered meaning for an engine caution code, or None when it has none (never guessed)."""
    return CAUTIONS.get(str(code or "").strip().upper())


def watch(code):
    """-> the registered meaning for an engine watch code, or None when it has none (never guessed)."""
    return WATCH.get(str(code or "").strip().upper())


def words(label):
    """An engine label in lower-case words (underscores to spaces): the last resort when no meaning is registered. Never raw CODE form."""
    return str(label or "").strip().lower().replace("_", " ")


def tier(label):
    """-> the engine's display name for a tier code, or the code in lower-case words when it is not registered (never the raw CODE)."""
    return TIERS.get(str(label or "").strip().upper()) or words(label)


def phase(label):
    return PHASES.get(words(label))


def leg_age(label):
    return LEG_AGE.get(words(label))


def risk(label):
    return RISK.get(words(label))


def volatility(label):
    return VOLATILITY.get(words(label))


def recorded(name):
    """-> what the engine saves for a concept (see RECORDED), or None for a concept it has no registered note for."""
    return RECORDED.get(str(name or "").strip().lower())


def term(name):
    return TERMS.get(str(name or "").strip().lower())


def terms_in(text):
    """Which glossary terms a piece of text mentions (word-boundary, case-insensitive), in glossary order. For 'what does that mean?'."""
    low = (text or "").lower()
    out = []
    for key in TERMS:
        pat = {"choch": r"\bchoch\b|change of character", "bos": r"\bbos\b|break of structure", "fvg": r"\bfvgs?\b|fair value gap",
               "order block": r"order ?blocks?", "liquidity sweep": r"liquidity sweep|\bsweep", "htf": r"\bhtf\b|higher.?timeframe",
               "bias": r"\bbias\b", "atr": r"\batr\b", "phase": r"\bphase\b", "exhaustion": r"exhaust", "displacement": r"displac", "protected level": r"protected|strong (?:high|low)|\bhh\b|\bll\b",
               "retracement": r"retrace|pullback|pull back"}[key]
        if re.search(pat, low):
            out.append(key)
    return out


def phrases():
    """Every phrase this module can say (for the no-digits test)."""
    return (list(CAUTIONS.values()) + list(WATCH.values()) + list(PHASES.values()) + list(LEG_AGE.values()) + list(RISK.values()) +
            list(VOLATILITY.values()) + list(TERMS.values()) + list(RECORDED.values()) + list(TIERS.values()))


# What each integrity check means in plain words. The inspector's own summary is still quoted word for word beside it (engine text is never rewritten).
PROBE_GLOSS = {
    "placeholder": "a number that looks like it was never filled in (stuck at 0.0)",
    "direction_conflict": "two parts of the engine disagree about direction",
    "timeframe_divergence": "two timeframes point different ways",
    "variant_divergence": "two versions of the same reading differ",
    "direction_copy_stale": "a copied direction looks out of date",
    "direction_unmapped": "a direction the checker doesn't recognise",
    "time": "a saved time that doesn't make sense",
    "identity": "a reference that points at nothing live",
    "growth": "a value that only ever grows",
    "schema": "the saved state's layout changed since the last run",
    "version": "the saved state doesn't match the code version",
    "domain": "a value that is impossible, or far out of line with the others (worth investigating)",
    "exercise": "a built-in self-test of the engine failed",
    "dwell": "a state has lasted unusually long",
}
SEVERITY = {"HIGH": ("\U0001F534", "High"), "MED": ("\U0001F7E0", "Medium"), "LOW": ("\U0001F7E1", "Low")}


def probe_gloss(probe):
    return PROBE_GLOSS.get(probe)


def severity(code):
    return SEVERITY.get(str(code).upper(), ("\u26AA", str(code).title()))


# What the integrity inspector looks for and what it cannot see. Plain words for questions like "why does it say clear?". These describe the
# inspector's own documented checks (inspector.py); test_plainwords fails if a check is added there without being described here.
INSPECTOR_CHECKS = (
    "a number named like a price that sits at exactly 0.0 (it looks like it was never filled in)",
    "two parts of the engine disagreeing about direction",
    "a saved time that is in the future, or later than the moment the file says it was written",
    "an id that no longer matches the numbers it was built from",
    "a saved field that disappeared, changed kind, or went empty",
    "a state that has lasted far longer than any earlier one",
    "a list that only ever grows",
    "the saved state having been written by different code than the code that is running",
    "a self-test of the belief engine's logic",
    "impossible values: a high below its low, a negative average range, a leg whose extreme is on the wrong side of its start",
    "a price level about three times bigger or smaller than the others in the same state (flagged as worth investigating, never corrected)",
)
INSPECTOR_LIMITS = (
    "whether a reading is actually right when it looks believable and sits close to the other levels (a price that is a few percent off, for example)",
    "the raw candles themselves (the engine's own data check owns those, and I only see what was saved)",
    "whether the market data is fresh (I report stale data separately, as a different warning)",
    "every source being wrong in the same way, because it looks for sources disagreeing with each other",
    "anything the engine never saved",
)


def inspector_checks():
    return INSPECTOR_CHECKS


def inspector_limits():
    return INSPECTOR_LIMITS
