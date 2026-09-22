"""
set_group_commands.py

Scopes Telegram's own "/" autocomplete menu for GROUP_CHAT_ID down to the
beginner-friendly command set — separate from, and in addition to, the
code-level restriction already enforced in scanner_live.py.

WHY THIS FILE EXISTS (read this before touching the group feature):
---------------------------------------------------------------------
The group's *behavior* is already correctly locked down in code: any
message from GROUP_CHAT_ID is routed through the group-only branch in
scanner_live.py's command dispatch (search for `GROUP_CHAT_ID and chat_id
== GROUP_CHAT_ID`), which only recognizes /status, /watch, /report, /help
— see format_group_help() for the canonical list and copy.

But Telegram's tap-to-autocomplete "/" menu (what you see when you type
"/" in a chat) is a SEPARATE, Telegram-side setting, set via the Bot API's
setMyCommands method. Set with no `scope`, it applies globally to every
chat the bot is in — so without this script, the group would show the
exact same full command list as the personal/DM chat, even though typing
any of those extra commands there does nothing (the code-level routing
above silently ignores anything not in format_group_help()'s list).

This script sets a CHAT-SCOPED command list (scope={"type": "chat",
"chat_id": GROUP_CHAT_ID}) so the group's own menu only shows what
actually works there. It does NOT touch the default/global scope, so the
personal chat's full menu is untouched.

WHEN TO RE-RUN THIS:
---------------------------------------------------------------------
- If the group-allowed command list ever changes (keep GROUP_COMMANDS
  below and format_group_help() in scanner_live.py in sync manually —
  nothing enforces that at runtime, they're two independent surfaces
  describing the same intended list).
- If GROUP_CHAT_ID is ever rotated (a new group created).
- This is idempotent — safe to re-run any time with no side effects
  beyond refreshing the menu Telegram shows.

This is NOT part of the scan loop and is never imported by
scanner_live.py/min_scanner.py — run it manually, once, whenever the
above applies:

    python3 set_group_commands.py

Requires TELEGRAM_TOKEN and GROUP_CHAT_ID to already be set in the
environment (same variables scanner_common.py reads).
"""
import os
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROUP_CHAT_ID = os.environ["GROUP_CHAT_ID"]

# Keep this in sync with format_group_help() in scanner_live.py by hand —
# see the module docstring above.
GROUP_COMMANDS = [
    {"command": "status", "description": "What's happening right now"},
    {"command": "watch", "description": "What the bot is waiting to see"},
    {"command": "report", "description": "Current hourly report"},
    {"command": "help", "description": "This list"},
]


def main():
    if not GROUP_CHAT_ID:
        raise SystemExit(
            "GROUP_CHAT_ID is not set — nothing to scope. Set it in the "
            "environment first (same variable scanner_common.py reads)."
        )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands"
    resp = requests.post(url, json={
        "commands": GROUP_COMMANDS,
        "scope": {"type": "chat", "chat_id": int(GROUP_CHAT_ID)},
    })
    print(resp.status_code, resp.json())
    resp.raise_for_status()
    if not resp.json().get("ok"):
        raise SystemExit("Telegram reported ok=false — see response above.")
    print("Group menu scoped successfully. Reopen the group chat in "
          "Telegram (fully close/reopen if it doesn't refresh right away) "
          "and check that '/' only shows: "
          + ", ".join("/" + c["command"] for c in GROUP_COMMANDS))


if __name__ == "__main__":
    main()
