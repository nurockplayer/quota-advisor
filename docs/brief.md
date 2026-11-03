# quota-advisor — implementation brief

## Goal

A local CLI that answers one question for AI coding leads: which worker should take this task right now? The answer is `codex`, `deepseek` or `haiku`. Leads run it before every dispatch, so it must be fast, honest about stale data, and conservative when unsure.

## Constraints

- One Python 3.11+ file, `quota_advisor.py`, standard library only (tomllib, json, urllib, datetime, argparse, zoneinfo). No third-party packages. Installed later as `~/.local/bin/quota-advisor`; don't install it.
- Tests use `unittest` under `tests/`, run with `python3 -m unittest discover -s tests`. Tests never touch the network or the real home directory: inject the clock, paths and HTTP fetcher.
- Read-only toward the outside: never read credentials (`~/.codex/auth.json`, Keychain), never write outside its own state and config directories.
- Work only inside this repo. Don't use a browser, `oracle`, or any external LLM or review service. Don't push or create remotes.
- Host limits: this is a light job; run tests single-process.

## Inputs

1. **Syrtis quota cache** (local file, default `~/Library/Application Support/com.nyanako.tokenbar/quota-pace-history-v3.json`). `series[]` entries have `providerId` (`codex` / `claude`), `windowKey`, and `samples[]`; each sample has `usedPercent`, `resetAt` (unix s), `sampledAt` (unix s), `durationSeconds`. The windows used are:
   - Codex: `main.session.v1` (5 h), `main.weekly.v1` (7 d)
   - Claude: `session.v1` (5 h), `weekly.v1` (7 d)
   Use each window's last sample. Example: `tests/fixtures/syrtis-quota-pace-history-v3.json`. If `schemaVersion` isn't one the code knows (currently 4), or a window is missing, treat that provider's quota as unknown and say so in the reason.
2. **codex-reset.com** `https://codex-reset.com/api/feed` and `https://codex-reset.com/api/forecast` — Tibo (@thsottiaux) reset announcements. Examples in `tests/fixtures/codex-reset-feed.json` and `codex-reset-forecast.json`. Relevant fields: `feed.signal` (`at`, `active`, `summary`, `url`), `forecast.official_signal`, `forecast.commitment`, `forecast.teased_window`, `forecast.last_reset_at`.
3. **lunarwerx** `https://codex.lunarwerx.com/api/v1/stats` — `lastResetAt` of the latest verified global reset. Example in `tests/fixtures/lunarwerx-stats.json`.

Cache network responses for 5 minutes under `~/.local/state/quota-advisor/cache/`. A network error or a response older than 2 hours means "no announcement known".

## Decision rules (first match wins)

Let `codex_ok` = Codex 5 h used < 95% and weekly used < 100%, from fresh Syrtis data.

1. **BURN** — a live announcement exists **and** Codex is usable (`codex_ok`). A live announcement is `forecast.official_signal` with a readable `at`, an active `feed.signal`, or a qualifying teased window (see below), that is not spent, not more than 5 min in the future, and not expired. An announcement is spent when any trusted reset is at or after its time: lunarwerx `lastResetAt`, `forecast.last_reset_at`, or a confirmed reset `latest_alert.source_at`. If a trusted reset time is unreadable, the ordering is uncertain and there is no BURN. An official window whose target or end passed more than 6 hours ago with no reset is expired. `forecast.teased_window` counts as a live announcement (labelled "inferred tease (codex-reset), treated as an announcement") when its `at` is readable, not more than 5 min in the future, later than every known reset (same spent rules), its window `start_at` is readable and within the next 24 h or already started, and its window `end_at` (or `start_at`) is not more than 6 h in the past. `probabilities.commitment` is a float and is not an announcement. Note the fixtures are a real case: `feed.signal.active` is true but the signal (2026-10-22T06:55Z) predates the reset (2026-10-23T17:42Z), so it must NOT trigger BURN. If BURN and `codex_ok`: Codex, any hour.
2. **PRIME** — just after a reset, spend the first part of the week: Codex weekly used < `prime_threshold` (default 35). If `codex_ok`: Codex, any hour.
3. **SAVE** — ration:
   - DeepSeek is allowed only if the task is expected to finish before the next DeepSeek peak: `now + est + margin <= next_peak_start`.
   - If DeepSeek is allowed → `deepseek`.
   - Else if `codex_ok` and Codex weekly used <= elapsed share of the weekly window + one day's share (100/7) → Codex.
   - Else if Claude 5 h used < 90% and Claude weekly used < 90% → `haiku`.
   - Else → `deepseek`, with the reason saying it runs at peak price (pay-as-you-go beats stalling), and `start_after` set to the next off-peak start.

Both tiers use the `codex` worker.

## Time

- DeepSeek peak price windows: Mon–Fri 10:00–13:00 and 15:00–19:00, Asia/Tokyo. Everything else is off-peak.
- `est`: `--est` (e.g. `90m`, `2h`), else the median of recorded actual durations for that tier (at least 3 records), else defaults hard 150 min / routine 60 min. `margin` = 30 min.
- Wrap-up: a DeepSeek task still running 10 minutes before a peak (09:50, 14:50) must wrap up. `quota-advisor next-peak` prints the next peak start and its wrap-up time.

## Stale and broken data

- Syrtis sample older than 30 min → Codex/Claude quota unknown: PRIME can't fire, BURN can't use Codex (no `codex_ok`), SAVE's Codex and Haiku branches are off. Off-peak still picks DeepSeek; peak falls to the last branch.
- Network sources unavailable → no BURN.
- Never guess: every unknown appears in the reason.

## CLI

- `quota-advisor --tier hard|routine [--est 90m] [--json]` — prints the worker name only; `--json` prints `{id, tier, worker, mode, reason, start_after?, est_minutes, codex:{...}, claude:{...}, signals:{...}, data_age:{...}}`.
- `quota-advisor status` — human summary: mode, each source and its age, the numbers, next peak.
- `quota-advisor next-peak` — next peak start and wrap-up time.
- `quota-advisor done <id> [--minutes N]` — records the actual duration for a decision (default: now minus the decision time).
- Every decision appends one JSON line to `~/.local/state/quota-advisor/decisions.log` with an `id`.

## Config

Optional `~/.config/quota-advisor/config.toml` overrides defaults: `prime_threshold`, `codex_session_limit` (95), `claude_limit` (90), `stale_minutes` (30), `margin_minutes` (30), `wrapup_minutes` (10), `default_est_minutes` (`{hard=150, routine=60}`), peak windows, source URLs, Syrtis path.

## Tests (one per case, fixed clock, fixtures)

- The real fixtures at their capture time: the spent signal does not BURN; Codex weekly 0% → PRIME → `codex`.
- A fresh announcement after the last reset → BURN at off-peak and peak.
- BURN with Codex 5 h at 96% → falls through to SAVE.
- SAVE off-peak with a task that fits before the peak → `deepseek`; one that would run into the peak → Codex/Haiku per the rules.
- SAVE at peak with Codex on pace → Codex; ahead of pace → Haiku; Claude also ≥ 90% → `deepseek` with `start_after`.
- Weekend: always off-peak.
- Stale Syrtis, missing window, unknown schemaVersion, network failure.
- `--est` parsing, median-of-actuals defaults, `done` recording, `next-peak` at 09:40 and 14:55 on a weekday and on a Saturday.
- Config overrides.

## Deliverable

`quota_advisor.py`, `tests/`, a short `README.md` (usage, rules, data sources, install line), and a `.gitignore`. Commit on `main` with clear messages. When done, report: files, test command and its result, and anything you couldn't decide (list it rather than guessing).
