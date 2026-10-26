# Defect and limitation register (26 Oct)
Open items stay visible here until a regression test pins their fix. "In scope 25/25" is not universal integrity.

| # | Item | Type | Status |
|---|---|---|---|
| 1 | Inspector does not catch a day-old market-data time marked fine (staleness is reported by the chat layer as a separate warning) | limitation / open defect | OPEN. Needs: an owner decision (inspector vs chat layer), a check, a regression test |
| 2 | Inspector misses a believable price error that sits close to the other levels | limitation | OPEN (stated in "what could still be wrong") |
| 3 | Inspector misses an absurd break count and a LOW failure risk with no reasons | limitation | OPEN |
| 4 | Scan id going backwards is only detectable with history across runs | limitation | OPEN |
| 5 | Three of 104 two-fault pairs hide one alarm (origin 0.0 + 15-minute flip; 15-minute flip + macro flip; confirmed + shadow bias flips) | defect (by design of how directions are grouped) | OPEN, not pinned by tests yet |
| 6 | Engine `data_looks_sane` does not check high<low or close outside the bar, and a whole series ten times too large passes (the spike test is relative) | limitation in the engine (not changed: engine owns raw data) | OPEN |
| 7 | Real 1-hour leg flip never observed in saved history | unverified on real data | PENDING: `audit_real_1h_flip.py` will check the first real one end to end |
| 8 | One ledger, one conversation: single owner only | limitation | OPEN. Not safe for a second user |
| 9 | No independent conversation tests exist | assurance gap | OPEN. Highest priority |
| 10 | Rule-based discussion: hint list (compression, spike, volatility, trend, pullback, session) is small; ideas far outside it get generic but honest handling | limitation | OPEN |
| 11 | Deployed system vs tested system: tests run the runner end to end on saved data with state carried through the ledger between runs, but not the live Telegram transport or the workflow itself | unverified | After pushing, check the Actions run contains the new code, and send one message from the owner chat |
| 12 | Cause of MIN scan failure run #6033 | unknown | Needs the Actions log |
| 13 | Hypothesis stages carry the time you said them (bot clock when received); the gap threshold, outcome rule and drop share are only prompted, not stored as pre-registered definitions | limitation | OPEN |
| 14 | Idea chat never runs a study; "tracking" = recounting on request; no persistent tally or alert on a user-defined idea | limitation | OPEN, by design |
