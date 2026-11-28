# BTC bot: setting it up in the same repo as the GBPUSD scanner

Same repo is the right choice. The Telegram and Twelve Data secrets already live there (secrets belong to a repo), the
scanner's command handler needs to read the BTC files, and one chat/one bot token is shared.

## 1. Add the files (no coding, in the GitHub website)
Unzip btc_bot_package.zip on your computer. You get three things: `btc_bot/`, `router/`, `.github/`.

1. Open your repo on github.com -> **Add file -> Upload files**.
2. Drag the **btc_bot** folder and the **router** folder in. GitHub keeps the folder structure. Commit to `main`.
3. The workflow file is in a hidden folder (`.github`), which many computers refuse to drag. Do this instead:
   **Add file -> Create new file**, type the name `.github/workflows/btc_bot.yml` (typing the slashes makes the
   folders), open `btc_bot/btc_bot.yml` from the unzipped folder in any text editor, copy everything, paste, commit.
4. If your repo already has a folder called `router`, tell me before uploading so nothing is overwritten.

## 2. Check the connections
Actions tab -> **BTC bot** -> Run workflow -> mode **ping**. It tests the Twelve Data key and the Telegram link and sends
you one test message. The secrets it uses are TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. If your
existing secrets are named differently, either rename them or change those three names in the workflow.

## 3. Start it
Run the workflow once with mode **run**: you get the "online" message. Then on cron-job.org copy your scanner job,
change the file name in its URL from your scanner workflow to `btc_bot.yml`, and set it to :01 and :31 past the hour.
The URL looks like `https://api.github.com/repos/<you>/<repo>/actions/workflows/btc_bot.yml/dispatches`
(same token and body as the scanner job).

## 4. Edit the demo accounts
Open `btc_bot/accounts.json` on github.com -> pencil icon -> change numbers -> commit. It takes effect on the next run.
If an account cannot open trades (for example a very small balance with a big minimum lot) the bot tells you in
`/bank`, `/status` and the online message.

## 5. Commands (needs the router wired into the scanner's command code, see below)
`/taken btc`, `/closed btc`, `/win btc`, `/undo btc`, `/journal btc`, `/status btc`, `/stats btc`, `/bank btc`,
`/bot btc` (switch), `/cancel` (back to GBPUSD). While BTC is the active bot the word btc is not needed.

Wiring: this part touches the scanner's own command code, so it should go through your normal review (your friend, CI on
Python 3.11) and is NOT needed for the BTC alerts, which work on their own. In the scanner's Telegram command handler,
before the old command handling, add the 6 lines from the top of `router/bot_router.py`. Two things to check there:
the scanner is in engine-only mode, so a router reply must go through the same narrow "command reply" exception your
other command replies use; and the scanner workflow must commit the two files it will now write,
`btc_bot/data/journal.jsonl` and `router/state.json` (add them to its `git add` step). Send me scanner_live.py's command
dispatcher and the scanner workflow and I will write the exact patch.
