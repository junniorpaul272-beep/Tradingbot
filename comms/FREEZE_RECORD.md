# Freeze record (build fingerprint for the blind baseline)

Created 2026-11-02T22:55:41Z UTC. This records what is being frozen; it does not change any code.

## Identity
- Code tree fingerprint (SHA-256 over every .py file under comms/ and the repo root, plus the CI workflow, in sorted order): `906a90f223a3e7cb105653e9f2c6306f7c8384e5f36043c2ae69c643ae1767a9`
- Delivered archive `valleybot_round4.zip` SHA-256: `4b28ae8266983987f06cbd1d50b35136c9c7bf3e38eb82d51684abe3eea9cef8` (the 21 code, test and report files delivered at round 4; the zip does not contain the whole repo)
- Files fingerprinted: 107
- Repository HEAD and branch (read-only look):
```
b6f2a894bb921bbe7895e38993b3217111df2d2d
```
- Uncommitted changes at freeze time (nothing has been committed or pushed by the assistant):
```
M .github/workflows/tests.yml
 M comms/bus.py
 M comms/bus_adapters.py
 M comms/bus_contract.py
 M comms/bus_runner.py
 M comms/converse.py
 M comms/guard.py
 M comms/informal.py
 M comms/inspector.py
 M comms/market_reconstruct.py
 M comms/test_bus.py
 M comms/test_bus_adapters.py
 M comms/test_bus_runner.py
 M comms/test_bus_signals.py
 M comms/test_bus_taken.py
 M comms/test_converse.py
 M comms/test_inspector.py
 M comms/user_catalog.py
 M comms/voice.py
 M min_scanner.py
 M scanner_live.py
?? comms/BLIND_TEST_GUIDE.md
?? comms/STRESS_CHAT_REPORT.md
?? comms/SYNTHESIS_DESIGN.md
?? comms/blind_eval.py
?? comms/chat.py
?? comms/chat_policy.py
?? comms/chat_records.py
?? comms/chat_wording_samples.py
?? comms/clarify.py
?? comms/stress_chat.py
?? comms/test_blind_eval.py
?? comms/test_chat.py
?? comms/test_clarify.py
?? comms/test_tradetalk.py
?? comms/tradetalk.py
```
Because these changes are uncommitted, the real baseline identity is the fingerprint above, not the commit. If Nelly commits first, record the new commit id next to the fingerprint.

## Coherence (this exact tree, tested together)
```
Ran 400 tests in 12.384s

OK (skipped=3)
```
`stress_chat.py`: 0 wrong, 0 leak, 0 crash on the same tree. Known pre-existing failure, not related: `test_stress_fixes` needs `tests_data_live/state_history_log.jsonl`, which is not in this copy.

## Settings that can change behaviour (values withheld where secret)
- `TELEGRAM_CHAT_ID`, `TELEGRAM_TOKEN`, `TWELVE_DATA_KEY`: must exist; the CI uses dummy values (`1`, `dummy`, `dummy`).
- Data the bot reads: the recorded candles, signal log, state files, `account_profiles.json` (risk profile) in the working folder. The blind runner works on a read-only copy.
- Time: the tests and the runner pass an explicit "now"; nothing depends on the wall clock.

## Isolation
- `blind_eval.py` blocks sockets during the run, uses a fresh copy of the world per case, records every attempted side effect, and runs a control case to prove an integrity announcement is independent of the message. Real Telegram sending is not reachable from the runner.
- Before the real run: re-run `python3 comms/blind_eval.py` self-check (`test_blind_eval`), confirm the fingerprint still matches, then run once and preserve `results.jsonl` and `MANIFEST.json` unchanged.

## Rule
No code change after the fingerprint is recorded until the baseline results are saved. If anything must change, record a new fingerprint and treat it as a different build.
