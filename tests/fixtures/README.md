# Test fixtures

These files are test data only. The code never reads them at runtime.

| File | What it is | Source |
| --- | --- | --- |
| `codex-reset-feed.json` | Trimmed `/api/feed` response: `signal` (an active signal dated 2026-10-22, before the reset it predates) plus `fetched_at`, `stale`, `version`. | `https://codex-reset.com/api/feed` |
| `codex-reset-forecast.json` | Trimmed `/api/forecast` response: `mode`, timestamps, `last_reset_at`, `probabilities.commitment` (null), null `official_signal` and `teased_window`, and a confirmed reset `latest_alert`. | `https://codex-reset.com/api/forecast` |
| `codex-reset-timeline.json` | Three events from `/api/timeline`: 2026-06-29 ("within an hour" deadline), 2026-07-10 ("later today" window) and 2026-08-31 (reset; its time is the reference for the trap-case test, whose values are hard-coded). | `https://codex-reset.com/api/timeline` |
| `lunarwerx-stats.json` | `/api/v1/stats` response, already small: `lastResetAt`, `generatedAt`, counters. | `https://codex.lunarwerx.com/api/v1/stats` |
| `syrtis-quota-pace-history-v3.json` | Sample of the Syrtis app's local quota cache with account scope `fixture-account`. It is not a public endpoint. | Local file, not fetched |

The API captures were taken on 2026-10-23 and 2026-10-24 and trimmed on 2026-10-24. Only the fields and records the tests use were kept; real field names and values are unchanged.

Credit and affiliation: codex-reset.com and codex.lunarwerx.com are independent community projects. This project is not affiliated with OpenAI. The tweet excerpts in `codex-reset-feed.json` and `codex-reset-timeline.json` are by @thsottiaux (Tibo), quoted for testing only.
