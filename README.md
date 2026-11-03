# quota-advisor

Answers one question before each dispatch: which worker takes this task now?
The answer is `sol`, `luna`, `deepseek`, `haiku` or `none` (queue the task). Python 3.11+, standard library only.

## Usage

```
quota-advisor --tier hard|routine [--est 90m] [--json]
quota-advisor status
quota-advisor next-peak
quota-advisor done <id> [--minutes N]
```

- Plain output is the worker name only. `--json` adds `id`, `mode`, `reason`, `start_after`, `est_minutes`, `codex`, `claude`, `signals` and `data_age`.
- `--est` accepts `90m`, `2h`, `1h30m`, `1.5h`, or a bare number of minutes. Without it, the median of recorded actuals for the tier is used (at least 3 records), else 150 min for hard and 60 min for routine.
- `done <id>` records the actual duration (default: minutes since the decision) so future estimates improve.
- `--config PATH`, `--state-dir PATH` and `--now ISO8601` (what-if checks) are global options.

Codex means `sol` for `--tier hard` and `luna` for `--tier routine`.

## Rules (first match wins)

Codex is the default worker at every hour. Fresh watcher account state makes Codex usable when `ordinary_usage_allowed` is true or `credits_balance > 0`, including after the weekly allowance is exhausted. Without fresh readable account state, use the existing TokenBar/Syrtis rule: weekly under 100%, and the 5 h window under `codex_session_limit` (95%) when reported. A missing 5 h window does not block fresh weekly data.

1. **BURN**: a live announcement exists and Codex is usable. A live announcement is `forecast.official_signal` with a readable `at`, an active feed signal, or a qualifying teased window (below), that is not spent, not more than 5 minutes in the future, and not expired. An announcement is spent when any trusted reset (lunarwerx `lastResetAt`, forecast `last_reset_at`, or a confirmed reset `latest_alert`) is at or after its time. If a trusted reset time is unreadable, the ordering is uncertain and there is no BURN. An official window that ended more than 6 hours ago with no reset is expired. A `teased_window` counts as a live announcement, labelled "inferred tease (codex-reset), treated as an announcement", when its `at` is readable and not more than 5 minutes ahead, it is later than every known reset, its window starts within the next 24 hours (or already started), and its window end is not more than 6 hours in the past. Codex, any hour.
2. **OFFPEAK_DEEPSEEK**: Weekly Codex use is strictly above today's target, it is off-peak, and the task fits before the next peak: `now + est + margin_minutes <= next peak start`, with the task finished by the wrap-up time. Today's target is `min(daily_target_pct × day, 100)%`, where `day` is day 1–7 of the Codex weekly window (default 18% per day). A zero daily target permits this diversion for any weekly use above zero. The JSON `signals.daily_target` retains the day and target percentage.
3. **CODEX**: Otherwise, when usable, use Codex any hour, including above today's target.
4. **FALLBACK / WAIT**: Codex is unusable. When the task cannot fit off-peak (it is peak now or the task would overlap the next peak), `peak_fallback = "stop"` (default) returns worker `none`, mode `WAIT`. Set `peak_fallback = "haiku"` to permit Haiku when Claude 5 h and weekly are both under `claude_limit` (90%); otherwise return `none`/`WAIT`. A WAIT decision queues the task until `start_after`: the earlier of the next off-peak start and a known future Codex reset. For an off-peak task that overlaps the next peak, use that peak's end. Re-evaluate then. When the task fits off-peak but does not qualify for rule 2, the existing usable-Claude fallback remains available; otherwise wait.

DeepSeek is never selected during peak or for a task that would run into peak. Peak is Mon-Fri 10:00-13:00 and 15:00-19:00 Asia/Tokyo. Weekends are always off-peak. Wrap up a DeepSeek task at 10 minutes before a peak start (`next-peak` prints it).

## Data sources

- **Codex watcher account state**: `~/.local/state/codex-quota-watch/account.json`, configurable through `account_state_path`. A sample no older than `stale_minutes` (30 by default) controls usability. Its `used_percent` and `resets_at` replace TokenBar weekly data when the watcher weekly sample is fresher; otherwise TokenBar weekly data wins. JSON output includes `codex.credits_balance` and `codex.usable_tickets` (null without fresh account state). Tickets are reported, not automatically redeemed. A missing, stale or unreadable file adds a note and falls back to TokenBar logic.
- **Syrtis quota cache**: `~/Library/Application Support/com.nyanako.tokenbar/quota-pace-history-v3.json` (schemaVersion 4 only). Uses the last sample of `main.session.v1`, `main.weekly.v1` (Codex) and `session.v1`, `weekly.v1` (Claude). A provider whose newest sample (across its windows) is older than 30 min, a missing window, a window whose reset has passed, a duration not within 10% of 5 h (session) or 7 d (weekly), a non-finite or out-of-range value, or an unknown schema makes that provider's quota unknown. Unknowns are named in `reason`.
- **codex-reset.com** `/api/feed` and `/api/forecast`: reset announcements.
- **lunarwerx** `/api/v1/stats`: `lastResetAt`, the latest verified reset. Falls back to the forecast's `last_reset_at`.

Network responses are cached for 5 minutes under `<state-dir>/cache/`. A network error, or a response older than 2 hours, means no announcement is known, so BURN does not fire.

Credits and affiliation: reset announcements come from [codex-reset.com](https://codex-reset.com) and the verified reset timeline from [codex.lunarwerx.com](https://codex.lunarwerx.com). Both are independent community projects. Quota numbers come from the watcher account state and the Syrtis app's local cache. This project is not affiliated with OpenAI, codex-reset.com, lunarwerx or Syrtis.

## Files

- Config (optional): `~/.config/quota-advisor/config.toml`. Keys: `daily_target_pct = 18.0`, `peak_fallback = "stop"` (`"stop"` or `"haiku"`), `account_state_path`, `codex_session_limit`, `claude_limit`, `stale_minutes`, `margin_minutes`, `wrapup_minutes`, `default_est_minutes = {hard = 150, routine = 60}`, `peak_windows = [["10:00", "13:00"], ["15:00", "19:00"]]`, `timezone`, `syrtis_path`, `feed_url`, `forecast_url`, `lunarwerx_url`. `prime_threshold` and `reset_tickets` are accepted for old configs, ignored, and named in deprecation notes. Other unknown keys are an error, as are non-finite numbers, percentages above 100, minute values above 10080 (one week), and invalid UTF-8 or TOML.
- State (written by decisions and `done`): `~/.local/state/quota-advisor/decisions.log`, one JSON line per decision and per `done`, plus `cache/`.

## Tests

```
orb-test -- python3 -m unittest discover -s tests
```

Tests use a fixed clock, fixture data and a fake fetcher. They never touch the network or the real home directory.

## Install

```
install -m 755 quota_advisor.py ~/.local/bin/quota-advisor
```

## License

MIT. See [LICENSE](LICENSE).
