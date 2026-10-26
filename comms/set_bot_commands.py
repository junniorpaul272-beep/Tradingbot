"""set_bot_commands.py — sets the "/" menu you see in Telegram's private chat with ValleyBot (Telegram's setMyCommands).

WHY A SEPARATE STEP: that menu is stored on Telegram's side. Changing the code does not change it; this script (run by
the 'Set Telegram menu' workflow, one click) is what updates it. Run it again whenever a command is added or renamed.
It touches ONLY the private-chat menu: the group's menu is set by set_group_commands.py and is left alone.

MENU is the single list. test_menu_matches_commands checks every entry is a command the bot really answers, so the menu
cannot drift from the code. Telegram limits: command names a-z 0-9 _ (max 32), descriptions 3-256 chars, max 100 commands.
"""
import json
import os
import sys
import urllib.request

# (command, description) in the order shown. Descriptions are plain words, the same ones /help uses.
MENU = [
    ("now", "What's happening right now"),
    ("thesis", "The engine's current read"),
    ("watch", "What I'm watching for"),
    ("changed", "What changed since the last look"),
    ("flip", "What would break the current read"),
    ("story", "The fuller read"),
    ("why", "The evidence behind something (/why htf.bias)"),
    ("taken", "You took the latest fired signal"),
    ("skip", "You passed on the latest fired signal"),
    ("logtrade", "Log a trade you took"),
    ("reason", "Add your reasoning to a trade"),
    ("outcome", "Record how a trade ended"),
    ("review", "Show a logged trade"),
    ("botvsme", "Bot plan vs your reasoning"),
    ("reconstruct", "What was on record at entry, and what is missing"),
    ("investigate", "Worksheet for a trade (win or loss)"),
    ("know", "Record what you know, word for word"),
    ("ask", "Capture a research question"),
    ("questions", "List your research questions"),
    ("slang", "Teach me a word you use (rn = right now)"),
    ("alert", "Set a price alert"),
    ("alerts", "List your alerts"),
    ("quiet", "Hold automated messages for a while"),
    ("loud", "End quiet mode"),
    ("health", "Data and ledger check"),
    ("stats", "Summary stats (older command)"),
    ("journal", "Trade journal (older command)"),
    ("help", "Every command, explained"),
]


def payload():
    return {"commands": [{"command": c, "description": d} for c, d in MENU],
            "scope": {"type": "all_private_chats"}}


def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_TOKEN is not set; nothing was changed.")
        return 1
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/setMyCommands",
                                 data=json.dumps(payload()).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
    except Exception as e:                      # never print the URL: it contains the token
        print(f"Telegram did not accept the menu ({type(e).__name__}).")
        return 1
    print("Menu updated." if body.get("ok") else f"Telegram refused: {body.get('description')}")
    return 0 if body.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
