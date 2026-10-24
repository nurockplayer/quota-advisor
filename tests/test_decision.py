"""Decision rules, reset detection, staleness and network handling (fixed clock, no network)."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import (
    FIXTURE_NOW,
    FIXTURES,
    SAT_1100_JST,
    SYRTIS_FIXTURE,
    WED_1100_JST,
    WED_1330_JST,
    evaluate_now,
    fetcher_for,
    fixture_payloads,
    iso,
    qa,
    syrtis_doc,
    write_config,
    write_json,
)

UTC = timezone.utc
NEXT_PEAK_FROM_1330_WED = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)  # Wed 15:00 JST


def announcing(now: datetime, signal_at: datetime) -> dict:
    """Fixture payloads, with the feed signal replaced by an active one at `signal_at`."""
    table = fixture_payloads(now)
    table[qa.FEED_URL]["signal"] = {
        "tweet_id": "2113000000000000001",
        "summary": "Landing tomorrow before 10am PST.",
        "at": iso(signal_at),
        "url": "https://x.com/thsottiaux/status/2113000000000000001",
        "kind": "signal",
        "is_reply": True,
        "replying_to": "thsottiaux",
        "active": True,
    }
    return table


class DecisionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config_with_syrtis(self, doc: dict | None = None, extra: str = "") -> Path:
        path = self.dir / "syrtis.json"
        if doc is None:
            syrtis = SYRTIS_FIXTURE
        else:
            syrtis = write_json(path, doc)
        return write_config(self.dir, syrtis, extra)

    def decide(self, now, table, *, tier="hard", est=150, doc=None, extra="", fail=()):
        cfg_path = self.config_with_syrtis(doc, extra)
        return evaluate_now(now, cfg_path, self.state, fetcher_for(table, fail), tier=tier, est=est)


class RealFixtureTests(DecisionTestCase):
    def test_spent_signal_does_not_burn(self) -> None:
        # feed.signal.active is true, but it is dated 2026-10-22 before the 2026-10-23 reset.
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertFalse(result["signals"]["announced"])
        self.assertEqual(result["signals"]["spent_announcements"], 1)
        self.assertIn("predates the last reset", result["reason"])

    def test_codex_weekly_zero_is_prime_sol(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), tier="hard")
        self.assertEqual(result["mode"], "PRIME")
        self.assertEqual(result["worker"], "sol")
        self.assertEqual(result["codex"]["weekly_used"], 0.0)
        self.assertEqual(result["signals"]["last_reset_source"], "lunarwerx")

    def test_codex_weekly_zero_is_prime_luna_for_routine(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), tier="routine", est=60)
        self.assertEqual(result["mode"], "PRIME")
        self.assertEqual(result["worker"], "luna")


class BurnTests(DecisionTestCase):
    def test_fresh_announcement_burns_off_peak(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        result = self.decide(now, table)
        self.assertEqual(result["mode"], "BURN")
        self.assertEqual(result["worker"], "sol")
        self.assertTrue(result["signals"]["announced"])
        self.assertEqual(result["signals"]["announcement_source"], "feed.signal")

    def test_fresh_announcement_burns_at_peak(self) -> None:
        now = WED_1100_JST
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        result = self.decide(now, table, tier="routine", est=60, doc=syrtis_doc(now))
        self.assertEqual(result["mode"], "BURN")
        self.assertEqual(result["worker"], "luna")
        self.assertTrue(result["signals"]["in_peak"])

    def test_burn_with_codex_session_at_96_falls_through_to_save(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        result = self.decide(now, table, doc=syrtis_doc(now, codex_session=96))
        self.assertEqual(result["mode"], "SAVE")
        self.assertEqual(result["worker"], "deepseek")
        self.assertIn("Reset announced", result["reason"])
        self.assertIn("Codex is not usable", result["reason"])

    def test_burn_with_codex_stale_is_not_usable(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        result = self.decide(now, table, doc=syrtis_doc(now, age_minutes=45))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("quota unknown", result["reason"])

    def test_announcement_older_than_reset_is_spent(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 17, 0, tzinfo=UTC))  # before 17:42 reset
        result = self.decide(now, table)
        self.assertNotEqual(result["mode"], "BURN")

    def test_announced_but_latest_reset_unknown_does_not_burn(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        table[qa.LUNARWERX_URL]["lastResetAt"] = None
        table[qa.FORECAST_URL]["last_reset_at"] = None
        table[qa.FORECAST_URL]["latest_alert"] = None
        result = self.decide(now, table)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("reset ordering uncertain", result["reason"])


class SaveTests(DecisionTestCase):
    def test_off_peak_fitting_task_goes_to_deepseek(self) -> None:
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), est=60, doc=syrtis_doc(WED_1330_JST, codex_weekly=70))
        self.assertEqual(result["mode"], "SAVE")
        self.assertEqual(result["worker"], "deepseek")
        self.assertTrue(result["signals"]["deepseek_fits"])

    def test_fit_boundary_is_inclusive(self) -> None:
        # Wed 13:30 JST + 60 min + 30 min margin = 15:00, exactly the peak start.
        doc = syrtis_doc(WED_1330_JST, codex_weekly=40)
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), est=60, doc=doc)
        self.assertEqual(result["worker"], "deepseek")
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), est=61, doc=doc)
        self.assertNotEqual(result["worker"], "deepseek")

    def test_task_running_into_peak_uses_codex_when_on_pace(self) -> None:
        # Weekly 0% against 50% elapsed: on pace.
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), est=120, doc=syrtis_doc(WED_1330_JST, codex_weekly=40))
        self.assertEqual(result["mode"], "SAVE")
        self.assertEqual(result["worker"], "sol")
        self.assertFalse(result["signals"]["deepseek_fits"])

    def test_task_running_into_peak_uses_luna_for_routine(self) -> None:
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), tier="routine", est=120, doc=syrtis_doc(WED_1330_JST, codex_weekly=40))
        self.assertEqual(result["worker"], "luna")

    def test_task_running_into_peak_uses_haiku_when_codex_ahead(self) -> None:
        # Weekly 70% against 50% elapsed + 14.3% allowance: ahead of pace.
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), est=120, doc=syrtis_doc(WED_1330_JST, codex_weekly=70))
        self.assertEqual(result["worker"], "haiku")

    def test_peak_with_codex_on_pace_goes_to_codex(self) -> None:
        result = self.decide(WED_1100_JST, fixture_payloads(WED_1100_JST), doc=syrtis_doc(WED_1100_JST, codex_weekly=40))
        self.assertEqual(result["mode"], "SAVE")
        self.assertEqual(result["worker"], "sol")
        self.assertTrue(result["signals"]["in_peak"])

    def test_peak_with_codex_ahead_goes_to_haiku(self) -> None:
        result = self.decide(WED_1100_JST, fixture_payloads(WED_1100_JST), doc=syrtis_doc(WED_1100_JST, codex_weekly=70))
        self.assertEqual(result["worker"], "haiku")

    def test_peak_with_claude_also_over_90_goes_to_deepseek_with_start_after(self) -> None:
        doc = syrtis_doc(WED_1100_JST, codex_weekly=70, claude_session=95)
        result = self.decide(WED_1100_JST, fixture_payloads(WED_1100_JST), doc=doc)
        self.assertEqual(result["worker"], "deepseek")
        self.assertIn("peak price", result["reason"])
        # Next off-peak start after Wed 11:00 JST is 13:00 JST.
        self.assertEqual(result["start_after"], qa.iso(datetime(2026, 10, 28, 4, 0, tzinfo=UTC)))

    def test_weekend_is_always_off_peak(self) -> None:
        result = self.decide(SAT_1100_JST, fixture_payloads(SAT_1100_JST), est=300, doc=syrtis_doc(SAT_1100_JST, codex_weekly=70))
        self.assertFalse(result["signals"]["in_peak"])
        self.assertEqual(result["worker"], "deepseek")
        self.assertTrue(result["signals"]["deepseek_fits"])
        self.assertNotIn("start_after", result)

    def test_peak_window_check_on_weekday_at_0950(self) -> None:
        # Mon 09:50 JST: off-peak, but the 300-minute task cannot finish before the 10:00 peak.
        now = datetime(2026, 10, 26, 0, 50, tzinfo=UTC)
        result = self.decide(now, fixture_payloads(now), est=300, doc=syrtis_doc(now))
        self.assertNotEqual(result["worker"], "deepseek")
        self.assertEqual(result["worker"], "sol")


class UnknownDataTests(DecisionTestCase):
    def test_stale_syrtis_makes_both_providers_unknown(self) -> None:
        result = self.decide(WED_1100_JST, fixture_payloads(WED_1100_JST), doc=syrtis_doc(WED_1100_JST, age_minutes=45))
        self.assertFalse(result["codex"]["ok"])
        self.assertFalse(result["claude"]["ok"])
        self.assertIn("45 min old", result["reason"])
        self.assertIn("Codex quota unknown", result["reason"])
        self.assertEqual(result["worker"], "deepseek")
        self.assertEqual(result["start_after"], qa.iso(datetime(2026, 10, 28, 4, 0, tzinfo=UTC)))

    def test_stale_syrtis_off_peak_still_picks_deepseek(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), doc=syrtis_doc(FIXTURE_NOW, age_minutes=31))
        self.assertEqual(result["worker"], "deepseek")
        self.assertNotEqual(result["mode"], "PRIME")

    def test_stale_syrtis_cannot_prime(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), doc=syrtis_doc(FIXTURE_NOW, age_minutes=31))
        self.assertEqual(result["mode"], "SAVE")

    def test_missing_window_is_unknown_and_named(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), doc=syrtis_doc(FIXTURE_NOW, drop=("main.weekly.v1",)))
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("main.weekly.v1 missing", result["reason"])
        self.assertTrue(result["claude"]["ok"])

    def test_unknown_schema_version_makes_everything_unknown(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), doc=syrtis_doc(FIXTURE_NOW, schema=5))
        self.assertFalse(result["codex"]["ok"])
        self.assertFalse(result["claude"]["ok"])
        self.assertIn("schemaVersion 5", result["reason"])

    def test_window_whose_reset_has_passed_is_unknown(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), doc=syrtis_doc(FIXTURE_NOW, weekly_reset_days=-1))
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("has passed", result["reason"])

    def test_missing_syrtis_file_is_unknown(self) -> None:
        cfg_path = write_config(self.dir, self.dir / "absent.json")
        result = evaluate_now(FIXTURE_NOW, cfg_path, self.state, fetcher_for(fixture_payloads(FIXTURE_NOW)))
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("not found", result["reason"])


class NetworkTests(DecisionTestCase):
    def test_network_failure_means_no_burn_but_decision_still_made(self) -> None:
        table = announcing(FIXTURE_NOW, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        result = self.decide(FIXTURE_NOW, table, fail=(qa.FEED_URL, qa.FORECAST_URL, qa.LUNARWERX_URL))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("codex-reset feed unavailable", result["reason"])
        self.assertIn("lunarwerx unavailable", result["reason"])
        self.assertIsNone(result["data_age"]["feed_seconds"])

    def test_response_older_than_two_hours_is_no_announcement(self) -> None:
        table = announcing(FIXTURE_NOW, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        table[qa.FEED_URL]["fetched_at"] = iso(FIXTURE_NOW - timedelta(hours=3))
        result = self.decide(FIXTURE_NOW, table)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("2 h", result["reason"])

    def test_missing_lunarwerx_falls_back_to_forecast_reset(self) -> None:
        table = fixture_payloads(FIXTURE_NOW)
        result = self.decide(FIXTURE_NOW, table, fail=(qa.LUNARWERX_URL,))
        self.assertEqual(result["signals"]["last_reset_source"], "codex-reset forecast")
        self.assertEqual(result["signals"]["last_reset_at"], qa.iso(datetime(2026, 10, 23, 17, 40, 51, tzinfo=UTC)))

    def test_cache_avoids_refetch_within_five_minutes(self) -> None:
        table = fixture_payloads(FIXTURE_NOW)
        fetcher = fetcher_for(table)
        cfg_path = self.config_with_syrtis()
        evaluate_now(FIXTURE_NOW, cfg_path, self.state, fetcher)
        evaluate_now(FIXTURE_NOW + timedelta(minutes=4), cfg_path, self.state, fetcher)
        self.assertEqual(fetcher.calls.count(qa.FEED_URL), 1)
        evaluate_now(FIXTURE_NOW + timedelta(minutes=6), cfg_path, self.state, fetcher)
        self.assertEqual(fetcher.calls.count(qa.FEED_URL), 2)

    def test_failed_fetch_does_not_use_expired_cache(self) -> None:
        table = fixture_payloads(FIXTURE_NOW)
        cfg_path = self.config_with_syrtis()
        evaluate_now(FIXTURE_NOW, cfg_path, self.state, fetcher_for(table))
        result = evaluate_now(
            FIXTURE_NOW + timedelta(minutes=10), cfg_path, self.state,
            fetcher_for(table, fail=(qa.FEED_URL,)),
        )
        self.assertIsNone(result["data_age"]["feed_seconds"])


class EstimateTests(unittest.TestCase):
    def test_parse_est_forms(self) -> None:
        self.assertEqual(qa.parse_est("90m"), 90)
        self.assertEqual(qa.parse_est("2h"), 120)
        self.assertEqual(qa.parse_est("1h30m"), 90)
        self.assertEqual(qa.parse_est("1.5h"), 90)
        self.assertEqual(qa.parse_est("45"), 45)

    def test_parse_est_accepts_uppercase_and_trailing_minutes(self) -> None:
        self.assertEqual(qa.parse_est("1H30M"), 90)
        self.assertEqual(qa.parse_est("2H"), 120)
        self.assertEqual(qa.parse_est("1h30"), 90)
        self.assertEqual(qa.parse_est(" 45M "), 45)

    def test_parse_est_rejects_bad_input(self) -> None:
        for bad in ("", "abc", "0m", "h", "-5m", "2x", "h30"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                qa.parse_est(bad)

    def test_default_est_when_no_actuals(self) -> None:
        cfg = qa.Config()
        self.assertEqual(qa.resolve_est("hard", None, [], cfg), (150, "default"))
        self.assertEqual(qa.resolve_est("routine", None, [], cfg), (60, "default"))

    def test_median_of_actuals_needs_three_records(self) -> None:
        cfg = qa.Config()
        records = [
            {"kind": "decision", "id": "a", "tier": "hard"},
            {"kind": "decision", "id": "b", "tier": "hard"},
            {"kind": "decision", "id": "c", "tier": "hard"},
            {"kind": "decision", "id": "d", "tier": "routine"},
            {"kind": "done", "id": "a", "minutes": 100},
            {"kind": "done", "id": "b", "minutes": 120},
            {"kind": "done", "id": "d", "minutes": 20},
        ]
        self.assertEqual(qa.resolve_est("hard", None, records, cfg), (150, "default"))
        records.append({"kind": "done", "id": "c", "minutes": 140})
        self.assertEqual(qa.resolve_est("hard", None, records, cfg), (120, "median of 3 recorded runs"))
        self.assertEqual(qa.resolve_est("routine", None, records, cfg), (60, "default"))

    def test_explicit_est_wins(self) -> None:
        self.assertEqual(qa.resolve_est("hard", 75, [], qa.Config()), (75, "--est"))


class ConfigTests(DecisionTestCase):
    def test_prime_threshold_override_disables_prime(self) -> None:
        result = self.decide(FIXTURE_NOW, fixture_payloads(FIXTURE_NOW), extra="prime_threshold = 0")
        self.assertNotEqual(result["mode"], "PRIME")

    def test_margin_override_changes_fit(self) -> None:
        # With a 120-minute margin, 13:30 + 60 + 120 runs past the 15:00 peak.
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), est=60, extra="margin_minutes = 120", doc=syrtis_doc(WED_1330_JST, codex_weekly=40))
        self.assertNotEqual(result["worker"], "deepseek")

    def test_peak_window_override(self) -> None:
        # Only 10:00-12:00 is peak, so Wed 13:30 JST is off-peak with a long runway.
        result = self.decide(WED_1330_JST, fixture_payloads(WED_1330_JST), est=60, extra='peak_windows = [["10:00", "12:00"]]', doc=syrtis_doc(WED_1330_JST, codex_weekly=40))
        self.assertEqual(result["worker"], "deepseek")
        self.assertEqual(result["signals"]["next_peak_start"], qa.iso(datetime(2026, 10, 29, 1, 0, tzinfo=UTC)))

    def test_default_est_override(self) -> None:
        cfg = qa.config_from_dict({"default_est_minutes": {"hard": 30}})
        self.assertEqual(qa.resolve_est("hard", None, [], cfg), (30, "default"))
        self.assertEqual(qa.resolve_est("routine", None, [], cfg), (60, "default"))

    def test_unknown_key_is_rejected(self) -> None:
        with self.assertRaises(qa.ConfigError):
            qa.config_from_dict({"prime_threshhold": 10})

    def test_bad_peak_window_is_rejected(self) -> None:
        with self.assertRaises(qa.ConfigError):
            qa.config_from_dict({"peak_windows": [["13:00", "10:00"]]})

    def test_network_urls_are_overridable(self) -> None:
        cfg = qa.config_from_dict({"feed_url": "https://example.invalid/feed"})
        self.assertEqual(cfg.feed_url, "https://example.invalid/feed")
        self.assertEqual(cfg.forecast_url, qa.FORECAST_URL)


class ReviewFollowUpTests(DecisionTestCase):
    def test_lagging_lunarwerx_does_not_make_spent_announcement_live(self) -> None:
        # lunarwerx lags at 10:00Z; the forecast knows a reset at 17:40:51Z. The signal at
        # 13:00Z is before that reset, so it is spent even though it is after lunarwerx's value.
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 13, 0, tzinfo=UTC))
        table[qa.LUNARWERX_URL]["lastResetAt"] = "2026-10-23T10:00:00.000Z"
        result = self.decide(now, table)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertFalse(result["signals"]["announced"])
        self.assertEqual(result["signals"]["last_reset_source"], "codex-reset forecast")
        self.assertEqual(result["signals"]["last_reset_at"], qa.iso(datetime(2026, 10, 23, 17, 40, 51, tzinfo=UTC)))

    def test_later_lunarwerx_reset_wins_and_is_named(self) -> None:
        table = announcing(FIXTURE_NOW, datetime(2026, 10, 23, 13, 0, tzinfo=UTC))
        result = self.decide(FIXTURE_NOW, table)
        self.assertEqual(result["signals"]["last_reset_source"], "lunarwerx")

    def test_feed_marked_stale_is_unavailable_and_noted(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        table[qa.FEED_URL]["stale"] = True
        result = self.decide(now, table)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("codex-reset feed unavailable: source reports stale: true", result["reason"])

    def test_feed_timestamp_in_future_is_unavailable(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        table[qa.FEED_URL]["fetched_at"] = iso(now + timedelta(minutes=10))
        result = self.decide(now, table)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("in the future", result["reason"])

    def test_small_future_skew_is_tolerated(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        table[qa.FEED_URL]["fetched_at"] = iso(now + timedelta(minutes=3))
        result = self.decide(now, table)
        self.assertEqual(result["mode"], "BURN")

    def test_syrtis_sample_in_future_is_unknown(self) -> None:
        now = FIXTURE_NOW
        result = self.decide(now, fixture_payloads(now), doc=syrtis_doc(now, age_minutes=-10))
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("in the future", result["reason"])

    def test_syrtis_sample_slightly_ahead_is_fine(self) -> None:
        now = FIXTURE_NOW
        result = self.decide(now, fixture_payloads(now), doc=syrtis_doc(now, age_minutes=-3))
        self.assertTrue(result["codex"]["ok"])


class BoundaryTests(DecisionTestCase):
    """Peak edges, judged in JST (UTC+9). Codex weekly 40 keeps PRIME from firing."""

    def at(self, now, est=60, tier="hard", **doc_kwargs):
        doc_kwargs.setdefault("codex_weekly", 40)
        return self.decide(now, fixture_payloads(now), tier=tier, est=est, doc=syrtis_doc(now, **doc_kwargs))

    def test_friday_1900_rolls_to_off_peak_and_weekend_runway(self) -> None:
        now = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)  # Fri 19:00 JST
        result = self.at(now)
        self.assertFalse(result["signals"]["in_peak"])
        self.assertTrue(result["signals"]["deepseek_fits"])
        self.assertEqual(result["worker"], "deepseek")
        self.assertEqual(result["signals"]["next_peak_start"], qa.iso(datetime(2026, 10, 26, 1, 0, tzinfo=UTC)))

    def test_friday_1859_is_still_peak(self) -> None:
        now = datetime(2026, 10, 23, 9, 59, tzinfo=UTC)  # Fri 18:59 JST
        result = self.at(now)
        self.assertTrue(result["signals"]["in_peak"])
        self.assertFalse(result["signals"]["deepseek_fits"])
        self.assertEqual(result["worker"], "sol")

    def test_monday_1000_is_peak_start(self) -> None:
        now = datetime(2026, 10, 26, 1, 0, tzinfo=UTC)  # Mon 10:00 JST
        result = self.at(now, codex_weekly=70, claude_session=95)
        self.assertTrue(result["signals"]["in_peak"])
        self.assertEqual(result["worker"], "deepseek")
        self.assertEqual(result["start_after"], qa.iso(datetime(2026, 10, 26, 4, 0, tzinfo=UTC)))  # 13:00 JST

    def test_monday_0959_is_off_peak_but_task_cannot_fit(self) -> None:
        now = datetime(2026, 10, 26, 0, 59, tzinfo=UTC)  # Mon 09:59 JST
        result = self.at(now)
        self.assertFalse(result["signals"]["in_peak"])
        self.assertFalse(result["signals"]["deepseek_fits"])
        self.assertEqual(result["worker"], "sol")

    def test_wednesday_1300_is_off_peak_and_fits_until_1500(self) -> None:
        now = datetime(2026, 10, 28, 4, 0, tzinfo=UTC)  # Wed 13:00 JST
        result = self.at(now)
        self.assertFalse(result["signals"]["in_peak"])
        self.assertTrue(result["signals"]["deepseek_fits"])  # 13:00 + 60 + 30 == 14:30 <= 15:00
        self.assertEqual(result["worker"], "deepseek")
        self.assertEqual(result["signals"]["next_peak_start"], qa.iso(datetime(2026, 10, 28, 6, 0, tzinfo=UTC)))

    def test_wednesday_1259_is_peak(self) -> None:
        now = datetime(2026, 10, 28, 3, 59, tzinfo=UTC)  # Wed 12:59 JST
        result = self.at(now)
        self.assertTrue(result["signals"]["in_peak"])
        self.assertEqual(result["worker"], "sol")

    def test_wednesday_1459_is_off_peak_but_task_cannot_fit(self) -> None:
        now = datetime(2026, 10, 28, 5, 59, tzinfo=UTC)  # Wed 14:59 JST
        result = self.at(now)
        self.assertFalse(result["signals"]["in_peak"])
        self.assertFalse(result["signals"]["deepseek_fits"])  # 14:59 + 90 runs past 15:00
        self.assertEqual(result["worker"], "sol")

    def test_wednesday_1500_is_peak_start_and_next_peak_is_thursday(self) -> None:
        now = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)  # Wed 15:00 JST
        result = self.at(now)
        self.assertTrue(result["signals"]["in_peak"])
        self.assertFalse(result["signals"]["deepseek_fits"])
        self.assertEqual(result["signals"]["next_peak_start"], qa.iso(datetime(2026, 10, 29, 1, 0, tzinfo=UTC)))

    def test_final_branch_start_after_in_peak_is_next_off_peak(self) -> None:
        now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)  # Fri 18:00 JST, in peak
        result = self.at(now, claude_session=95, codex_session=96)
        self.assertEqual(result["worker"], "deepseek")
        self.assertEqual(result["start_after"], qa.iso(datetime(2026, 10, 23, 10, 0, tzinfo=UTC)))  # 19:00 JST

    def test_final_branch_off_peak_overlap_start_after_is_end_of_next_peak(self) -> None:
        now = datetime(2026, 10, 28, 4, 30, tzinfo=UTC)  # Wed 13:30 JST, off-peak
        result = self.at(now, est=120, claude_session=95, codex_session=96)
        self.assertEqual(result["worker"], "deepseek")
        self.assertFalse(result["signals"]["in_peak"])
        self.assertEqual(result["start_after"], qa.iso(datetime(2026, 10, 28, 10, 0, tzinfo=UTC)))  # 19:00 JST

    def test_final_branch_wednesday_peak_exact_start_after(self) -> None:
        now = datetime(2026, 10, 28, 2, 0, tzinfo=UTC)  # Wed 11:00 JST
        result = self.at(now, codex_weekly=70, claude_session=95)
        self.assertEqual(result["start_after"], qa.iso(datetime(2026, 10, 28, 4, 0, tzinfo=UTC)))  # 13:00 JST



def timeline_official(timeline: dict, date: str) -> dict:
    """Build an official_signal from a real timeline event that carries an official_window."""
    event = next(e for e in timeline["events"] if e["date"] == date and e.get("official_window"))
    return {
        "tweet_id": event["id"],
        "at": event["announced_at"],
        "kind": "signal",
        "signal_type": "dated_commitment",
        "signal_tier": None,
        "summary": event["summary"],
        "window": event["official_window"],
    }


def window(start: datetime, end: datetime, label: str = "by end of Monday", target=None) -> dict:
    out = {"label": label, "start_at": iso(start), "end_at": iso(end), "time_zone": "Asia/Tokyo"}
    if target is not None:
        out["target_kind"] = "deadline"
        out["target_at"] = iso(target)
    return out


class AnnouncementShapeTests(DecisionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.timeline = json.loads((FIXTURES / "codex-reset-timeline.json").read_text(encoding="utf-8"))

    def build(self, now, *, official=None, teased=None, commitment=None, probabilities_commitment=None,
              lunar_last=None, forecast_last=None, alert=None):
        """Forecast/lunarwerx payloads. Reset times default to the fixture values; a forecast reset
        defaults to the lunarwerx one when only lunarwerx is given."""
        table = fixture_payloads(now)
        table[qa.FEED_URL]["signal"]["active"] = False  # isolate forecast shapes
        fc = table[qa.FORECAST_URL]
        fc["official_signal"] = official
        fc["teased_window"] = teased
        fc["commitment"] = commitment
        fc["probabilities"]["commitment"] = probabilities_commitment
        lunar_time = lunar_last or datetime(2026, 10, 23, 17, 42, 26, tzinfo=UTC)
        fc["last_reset_at"] = iso(forecast_last or lunar_last or datetime(2026, 10, 23, 17, 40, 51, tzinfo=UTC))
        fc["latest_alert"] = alert
        table[qa.LUNARWERX_URL]["lastResetAt"] = iso(lunar_time)
        return table

    def run_decision(self, now, table, **kwargs):
        return self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40), **kwargs)

    def test_promise_without_window_burns(self) -> None:
        now = FIXTURE_NOW
        official = {
            "tweet_id": "2113000000000000002", "at": iso(datetime(2026, 10, 23, 18, 0, tzinfo=UTC)),
            "kind": "signal", "signal_type": "promise", "signal_tier": "likely",
            "summary": "Landing soon.", "window": None,
        }
        table = self.build(now, official=official, lunar_last=datetime(2026, 10, 23, 17, 42, 26, tzinfo=UTC))
        result = self.run_decision(now, table)
        self.assertEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["announcement_source"], "official_signal (promise)")
        self.assertEqual(result["signals"]["reset_signals"]["official_signal"]["signal_tier"], "likely")
        self.assertIsNone(result["signals"]["reset_signals"]["official_signal"]["window"])

    def test_dated_commitment_with_window_burns_and_records_window(self) -> None:
        now = FIXTURE_NOW
        end = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
        official = {
            "tweet_id": "2113000000000000003", "at": iso(datetime(2026, 10, 23, 18, 0, tzinfo=UTC)),
            "kind": "signal", "signal_type": "dated_commitment", "signal_tier": "likely",
            "summary": "Reset by Monday.", "window": window(now, end, target=end),
        }
        table = self.build(now, official=official, lunar_last=datetime(2026, 10, 23, 17, 42, 26, tzinfo=UTC))
        result = self.run_decision(now, table)
        self.assertEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["reset_signals"]["official_signal"]["window"], "by end of Monday")

    def test_probability_commitment_float_is_not_an_announcement(self) -> None:
        now = FIXTURE_NOW
        table = self.build(now, probabilities_commitment=0.83, lunar_last=datetime(2026, 10, 23, 17, 42, 26, tzinfo=UTC))
        result = self.run_decision(now, table)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertFalse(result["signals"]["announced"])
        self.assertNotIn("commitment", result["reason"])

    def test_top_level_commitment_dict_with_time_counts(self) -> None:
        now = FIXTURE_NOW
        commitment = {"at": iso(datetime(2026, 10, 23, 18, 0, tzinfo=UTC)), "summary": "committed"}
        table = self.build(now, commitment=commitment, lunar_last=datetime(2026, 10, 23, 17, 42, 26, tzinfo=UTC))
        result = self.run_decision(now, table)
        self.assertEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["announcement_source"], "forecast.commitment")

    def test_official_signal_spent_by_confirmed_alert(self) -> None:
        now = FIXTURE_NOW
        official = {
            "tweet_id": "2113000000000000005", "at": iso(datetime(2026, 10, 23, 18, 0, tzinfo=UTC)),
            "kind": "signal", "signal_type": "promise", "signal_tier": "likely", "summary": "x", "window": None,
        }
        alert = {"id": "1", "kind": "reset", "state": "confirmed",
                 "source_at": iso(datetime(2026, 10, 23, 19, 0, tzinfo=UTC)), "summary": "reset", "url": None, "window": None}
        table = self.build(now, official=official, lunar_last=datetime(2026, 10, 23, 17, 42, 26, tzinfo=UTC), alert=alert)
        result = self.run_decision(now, table)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["spent_announcements"], 1)

    def test_aug31_trap_spent_by_each_reset_source(self) -> None:
        # Real case: official_signal pointed at a post that already said the reset had happened.
        now = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
        reset_at = datetime(2026, 8, 31, 2, 34, 27, tzinfo=UTC)  # timeline 2026-08-31 reset
        official = {
            "tweet_id": "2094000000000000001", "at": iso(datetime(2026, 8, 30, 18, 0, tzinfo=UTC)),
            "kind": "signal", "signal_type": "dated_commitment", "signal_tier": "likely",
            "summary": "reset promised for 6pm PT", "window": window(now, datetime(2026, 8, 31, 23, 59, tzinfo=UTC), label="end of Monday"),
        }
        alert = {"id": "2", "kind": "reset", "state": "confirmed", "source_at": iso(reset_at), "summary": "", "url": None, "window": None}
        old = datetime(2026, 8, 1, tzinfo=UTC)  # the other sources predate the announcement
        cases = {
            "lunarwerx": {"lunar_last": reset_at, "forecast_last": old},
            "forecast": {"lunar_last": old, "forecast_last": reset_at},
            "confirmed alert": {"lunar_last": old, "forecast_last": old, "alert": alert},
        }
        for index, (name, kwargs) in enumerate(cases.items()):
            with self.subTest(source=name):
                self.state = self.dir / f"state-{index}"  # responses are cached per state dir
                table = self.build(now, official=official, **kwargs)
                result = self.run_decision(now, table)
                self.assertNotEqual(result["mode"], "BURN")
                self.assertEqual(result["signals"]["spent_announcements"], 1)

    def test_expired_window_gives_no_burn_beyond_six_hours(self) -> None:
        now = FIXTURE_NOW
        at = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
        cases = [
            (now - timedelta(hours=7), False),  # past the 6 h grace: expired
            (now - timedelta(hours=5), True),   # within grace: still live
        ]
        for index, (end, live) in enumerate(cases):
            with self.subTest(end=iso(end)):
                self.state = self.dir / f"state-{index}"
                official = {
                    "tweet_id": "2113000000000000006", "at": iso(at), "kind": "signal",
                    "signal_type": "dated_commitment", "signal_tier": "likely", "summary": "x",
                    "window": window(at, end, target=end),
                }
                table = self.build(now, official=official, lunar_last=datetime(2026, 10, 23, 8, 0, tzinfo=UTC))
                result = self.run_decision(now, table)
                if live:
                    self.assertEqual(result["mode"], "BURN")
                else:
                    self.assertNotEqual(result["mode"], "BURN")
                    self.assertIn("treated as expired", result["reason"])

    def test_timeline_later_today_window_burns_before_its_end(self) -> None:
        now = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)
        table = self.build(now, official=timeline_official(self.timeline, "2026-07-10"), lunar_last=datetime(2026, 7, 10, 10, 0, tzinfo=UTC))
        result = self.run_decision(now, table)
        self.assertEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["reset_signals"]["official_signal"]["window"], "later today")

    def test_timeline_within_an_hour_deadline_expires_after_six_hours(self) -> None:
        for index, (now, expect_burn) in enumerate((
            (datetime(2026, 6, 30, 3, 0, tzinfo=UTC), True),   # 2 h20 past the 00:39 deadline
            (datetime(2026, 6, 30, 7, 0, tzinfo=UTC), False),  # 6 h20 past the deadline
        )):
            with self.subTest(now=iso(now)):
                self.state = self.dir / f"state-{index}"
                table = self.build(now, official=timeline_official(self.timeline, "2026-06-29"), lunar_last=datetime(2026, 6, 29, 0, 0, tzinfo=UTC))
                result = self.run_decision(now, table)
                if expect_burn:
                    self.assertEqual(result["mode"], "BURN")
                else:
                    self.assertNotEqual(result["mode"], "BURN")
                    self.assertIn("treated as expired", result["reason"])


class ReviewRound2Tests(DecisionTestCase):
    def test_unsorted_peak_windows_are_sorted_and_next_peak_is_correct(self) -> None:
        now = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)  # Wed 09:00 JST, off-peak
        extra = 'peak_windows = [["15:00", "19:00"], ["10:00", "13:00"]]'
        result = self.decide(now, fixture_payloads(now), est=90, extra=extra, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["worker"], "deepseek")  # 09:00 + 90 + 30 runs past 10:00
        self.assertEqual(result["worker"], "sol")
        self.assertEqual(result["signals"]["next_peak_start"], qa.iso(datetime(2026, 10, 28, 1, 0, tzinfo=UTC)))

    def test_overlapping_peak_windows_are_rejected(self) -> None:
        with self.assertRaises(qa.ConfigError) as ctx:
            qa.config_from_dict({"peak_windows": [["10:00", "13:00"], ["12:00", "15:00"]]})
        self.assertIn("overlap", str(ctx.exception))

    def test_adjacent_peak_windows_are_allowed(self) -> None:
        cfg = qa.config_from_dict({"peak_windows": [["15:00", "19:00"], ["10:00", "13:00"]]})
        self.assertEqual(cfg.peak_windows, (("10:00", "13:00"), ("15:00", "19:00")))
        cfg = qa.config_from_dict({"peak_windows": [["10:00", "13:00"], ["13:00", "15:00"]]})
        self.assertEqual(len(cfg.peak_windows), 2)

    def test_feed_with_invalid_timestamp_gives_no_burn(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        table[qa.FEED_URL]["fetched_at"] = "invalid"
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("source timestamp 'fetched_at' missing or invalid", result["reason"])

    def test_feed_with_missing_timestamp_gives_no_burn(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 18, 0, tzinfo=UTC))
        del table[qa.FEED_URL]["fetched_at"]
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("missing or invalid", result["reason"])

    def test_usedpercent_outside_range_or_nan_makes_window_unknown(self) -> None:
        now = FIXTURE_NOW
        for value in (-10.0, 101.0, float("nan")):
            with self.subTest(value=value):
                self.state = self.dir / f"state-{value}"
                result = self.decide(now, fixture_payloads(now), doc=syrtis_doc(now, codex_session=value))
                self.assertFalse(result["codex"]["ok"])
                self.assertIn("outside 0-100", result["reason"])
                self.assertTrue(result["claude"]["ok"])

    def test_usedpercent_bounds_are_inclusive(self) -> None:
        now = FIXTURE_NOW
        for value in (0.0, 100.0):
            with self.subTest(value=value):
                self.state = self.dir / f"bound-{value}"
                result = self.decide(now, fixture_payloads(now), doc=syrtis_doc(now, codex_session=value))
                self.assertTrue(result["codex"]["ok"])

    def test_cache_from_another_url_is_ignored(self) -> None:
        now = FIXTURE_NOW
        table = fixture_payloads(now)
        cfg1 = self.config_with_syrtis()
        evaluate_now(now, cfg1, self.state, fetcher_for(table))
        mirror = "https://mirror.example.invalid/api/feed"
        table[mirror] = table[qa.FEED_URL]
        cfg2 = self.config_with_syrtis(extra=f'feed_url = "{mirror}"')
        fetcher = fetcher_for(table)
        evaluate_now(now + timedelta(minutes=1), cfg2, self.state, fetcher)
        self.assertIn(mirror, fetcher.calls)


class ReviewRound3Tests(DecisionTestCase):
    def mutated_doc(self, now, mutate, **kwargs):
        doc = syrtis_doc(now, codex_weekly=40, **kwargs)
        mutate(doc)
        return doc

    @staticmethod
    def sample_of(doc, pid, wk):
        entry = next(e for e in doc["series"] if e["providerId"] == pid and e["windowKey"] == wk)
        return entry["samples"][0]

    def test_confirmed_alert_without_source_at_makes_ordering_uncertain(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 21, 26, 48, tzinfo=UTC))
        table[qa.LUNARWERX_URL]["lastResetAt"] = "2026-10-23T17:42:26Z"
        table[qa.FORECAST_URL]["latest_alert"] = {"kind": "reset", "state": "confirmed", "source_at": None}
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("confirmed reset alert source_at", result["reason"])
        self.assertIn("reset ordering uncertain", result["reason"])

    def test_same_signal_burns_when_alert_is_readable(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 21, 26, 48, tzinfo=UTC))
        table[qa.LUNARWERX_URL]["lastResetAt"] = "2026-10-23T17:42:26Z"
        table[qa.FORECAST_URL]["latest_alert"] = {"kind": "reset", "state": "confirmed", "source_at": "2026-10-23T17:40:51Z"}
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertEqual(result["mode"], "BURN")

    def test_unreadable_lunarwerx_reset_makes_ordering_uncertain(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, datetime(2026, 10, 23, 21, 26, 48, tzinfo=UTC))
        table[qa.LUNARWERX_URL]["lastResetAt"] = "not a time"
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("lunarwerx lastResetAt", result["reason"])

    def test_future_feed_signal_is_ignored_with_note(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, now + timedelta(minutes=10))
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("feed.signal time", result["reason"])
        self.assertIn("more than 5 min in the future", result["reason"])

    def test_small_future_feed_signal_within_tolerance_burns(self) -> None:
        now = FIXTURE_NOW
        table = announcing(now, now + timedelta(minutes=3))
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertEqual(result["mode"], "BURN")

    def test_future_official_signal_is_ignored_with_note(self) -> None:
        now = FIXTURE_NOW
        official = {"tweet_id": "1", "at": qa.iso(now + timedelta(minutes=10)), "kind": "signal",
                    "signal_type": "promise", "signal_tier": "likely", "summary": "x", "window": None}
        table = AnnouncementShapeTests.build(self, now, official=official)
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("official_signal (promise) time", result["reason"])

    def test_future_commitment_is_ignored_with_note(self) -> None:
        now = FIXTURE_NOW
        commitment = {"at": qa.iso(now + timedelta(minutes=10)), "summary": "x"}
        table = AnnouncementShapeTests.build(self, now, commitment=commitment)
        result = self.decide(now, table, doc=syrtis_doc(now, codex_weekly=40))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("forecast.commitment time", result["reason"])

    def test_nan_duration_is_unknown_not_a_crash(self) -> None:
        now = FIXTURE_NOW
        doc = self.mutated_doc(now, lambda d: self.sample_of(d, "codex", "main.session.v1").__setitem__("durationSeconds", float("nan")))
        result = self.decide(now, fixture_payloads(now), doc=doc)
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("durationSeconds", result["reason"])

    def test_non_string_provider_id_is_skipped_not_a_crash(self) -> None:
        now = FIXTURE_NOW
        doc = self.mutated_doc(now, lambda d: d["series"][0].__setitem__("providerId", []))
        result = self.decide(now, fixture_payloads(now), doc=doc)
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("missing", result["reason"])

    def test_non_list_samples_is_skipped_not_a_crash(self) -> None:
        now = FIXTURE_NOW
        doc = self.mutated_doc(now, lambda d: d["series"][0].__setitem__("samples", "x"))
        result = self.decide(now, fixture_payloads(now), doc=doc)
        self.assertFalse(result["codex"]["ok"])

    def test_huge_reset_time_is_unknown_not_a_crash(self) -> None:
        now = FIXTURE_NOW
        for value in (1e20, 1e300):
            with self.subTest(value=value):
                self.state = self.dir / f"huge-{value}"
                doc = self.mutated_doc(now, lambda d, v=value: self.sample_of(d, "codex", "main.weekly.v1").__setitem__("resetAt", v))
                result = self.decide(now, fixture_payloads(now), doc=doc)
                self.assertFalse(result["codex"]["ok"])
                self.assertIn("resetAt", result["reason"])

    def test_huge_sample_time_is_unknown_not_a_crash(self) -> None:
        now = FIXTURE_NOW
        doc = self.mutated_doc(now, lambda d: self.sample_of(d, "codex", "main.session.v1").__setitem__("sampledAt", 1e300))
        result = self.decide(now, fixture_payloads(now), doc=doc)
        self.assertFalse(result["codex"]["ok"])

    def test_claude_at_100_percent_never_gets_haiku(self) -> None:
        now = WED_1100_JST
        doc = syrtis_doc(now, codex_weekly=70, claude_session=100, claude_weekly=100)
        for extra in ("", "claude_limit = 100"):
            with self.subTest(extra=extra):
                self.state = self.dir / f"claude-{extra or 'default'}"
                result = self.decide(now, fixture_payloads(now), doc=doc, extra=extra)
                self.assertNotEqual(result["worker"], "haiku")
                self.assertFalse(result["claude"]["usable"])

    def test_wrap_up_must_also_clear_the_estimate(self) -> None:
        # Wed 09:00 JST, margin 0: 09:00 + 59 min runs past the 09:50 wrap-up.
        now = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
        doc = syrtis_doc(now, codex_weekly=40)
        result = self.decide(now, fixture_payloads(now), est=59, doc=doc, extra="margin_minutes = 0")
        self.assertNotEqual(result["worker"], "deepseek")
        self.assertFalse(result["signals"]["deepseek_fits"])
        self.state = self.dir / "fit50"
        result = self.decide(now, fixture_payloads(now), est=50, doc=doc, extra="margin_minutes = 0")
        self.assertEqual(result["worker"], "deepseek")


class ConfigRound3Tests(unittest.TestCase):
    def test_percent_thresholds_must_be_at_most_100(self) -> None:
        for key in ("codex_session_limit", "claude_limit", "prime_threshold"):
            with self.subTest(key=key), self.assertRaises(qa.ConfigError):
                qa.config_from_dict({key: 101})

    def test_percent_threshold_of_100_is_allowed(self) -> None:
        cfg = qa.config_from_dict({"codex_session_limit": 100})
        self.assertEqual(cfg.codex_session_limit, 100.0)

    def test_non_finite_numbers_are_rejected(self) -> None:
        for key in ("margin_minutes", "stale_minutes", "codex_session_limit", "prime_threshold"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(key=key, value=value), self.assertRaises(qa.ConfigError):
                    qa.config_from_dict({key: value})


class ReviewRound4Tests(DecisionTestCase):
    @staticmethod
    def sample_of(doc, pid, wk):
        entry = next(e for e in doc["series"] if e["providerId"] == pid and e["windowKey"] == wk)
        return entry["samples"][0]

    def test_weekly_window_with_wrong_duration_is_not_codex(self) -> None:
        # Weekly 90% used over a 400-day "window": the pace allowance cannot be trusted.
        now = WED_1100_JST
        doc = syrtis_doc(now, codex_weekly=90)
        self.sample_of(doc, "codex", "main.weekly.v1")["durationSeconds"] = 400 * 86400
        result = self.decide(now, fixture_payloads(now), doc=doc)
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("not within 10% of 604800", result["reason"])
        self.assertNotEqual(result["worker"], "sol")

    def test_session_duration_tolerance_is_ten_percent(self) -> None:
        now = FIXTURE_NOW
        for seconds, usable in ((18000 * 1.09, True), (18000 * 1.11, False)):
            with self.subTest(seconds=seconds):
                self.state = self.dir / f"sess-{seconds}"
                doc = syrtis_doc(now, codex_weekly=40)
                self.sample_of(doc, "codex", "main.session.v1")["durationSeconds"] = seconds
                result = self.decide(now, fixture_payloads(now), doc=doc)
                self.assertEqual(result["codex"]["ok"], usable)

    def test_weekly_duration_tolerance_is_ten_percent(self) -> None:
        now = FIXTURE_NOW
        for seconds, usable in ((604800 * 0.91, True), (604800 * 1.11, False)):
            with self.subTest(seconds=seconds):
                self.state = self.dir / f"week-{seconds}"
                doc = syrtis_doc(now, codex_weekly=40)
                self.sample_of(doc, "codex", "main.weekly.v1")["durationSeconds"] = seconds
                result = self.decide(now, fixture_payloads(now), doc=doc)
                self.assertEqual(result["codex"]["ok"], usable)

    def test_oversized_integers_make_windows_unknown_without_overflow(self) -> None:
        now = FIXTURE_NOW
        huge = 10 ** 310
        for field in ("usedPercent", "durationSeconds", "resetAt", "sampledAt"):
            with self.subTest(field=field):
                self.state = self.dir / f"big-{field}"
                doc = syrtis_doc(now, codex_weekly=40)
                self.sample_of(doc, "codex", "main.session.v1")[field] = huge
                result = self.decide(now, fixture_payloads(now), doc=doc)
                self.assertFalse(result["codex"]["ok"])
                self.assertIn("main.session.v1", result["reason"])
                self.assertTrue(result["claude"]["ok"])

    def test_malformed_log_records_are_ignored(self) -> None:
        now = FIXTURE_NOW
        bad_lines = [
            {"kind": "done", "id": [], "minutes": 5},
            {"kind": "done", "id": "abc", "minutes": 1e400},
            {"kind": "done", "id": "abc", "minutes": 10 ** 310},
            {"kind": "done", "id": "abc", "minutes": 20000},
            {"kind": "done", "id": "abc", "minutes": -4},
        ]
        self.state.mkdir(parents=True, exist_ok=True)
        text = "\n".join(json.dumps(r) for r in bad_lines) + "\n"
        (self.state / "decisions.log").write_text(text, encoding="utf-8")
        # None of these count as actuals, so the default estimate applies and the decision still works.
        records, note = qa.read_log(self.state)
        self.assertIsNone(note)
        self.assertEqual(qa.resolve_est("hard", None, records, qa.Config()), (150, "default"))

    def test_non_string_id_in_decision_record_does_not_crash_estimates(self) -> None:
        records = [
            {"kind": "decision", "id": [], "tier": "hard"},
            {"kind": "done", "id": [], "minutes": 5},
            {"kind": "decision", "id": "a", "tier": "hard"},
            {"kind": "done", "id": "a", "minutes": 100},
            {"kind": "done", "id": "a", "minutes": 10 ** 310},
        ]
        self.assertEqual(qa.recorded_minutes(records, "hard"), [100.0])
        self.assertEqual(qa.resolve_est("hard", None, records, qa.Config()), (150, "default"))


class ConfigRound4Tests(unittest.TestCase):
    def test_duration_like_config_values_are_bounded(self) -> None:
        for key in ("margin_minutes", "stale_minutes", "wrapup_minutes"):
            with self.subTest(key=key), self.assertRaises(qa.ConfigError):
                qa.config_from_dict({key: 1e12})
        with self.assertRaises(qa.ConfigError):
            qa.config_from_dict({"default_est_minutes": {"hard": 10 ** 9}})
        with self.assertRaises(qa.ConfigError):
            qa.config_from_dict({"default_est_minutes": {"routine": 0}})

    def test_parse_est_is_bounded_and_never_raises(self) -> None:
        for bad in ("99999m", "9" * 400, "0.001h", "10081", "1000000000h"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                qa.parse_est(bad)
        self.assertEqual(qa.parse_est("10080"), 10080)


def tease(at: datetime, start: datetime, end: datetime) -> dict:
    return {
        "tweet_id": "2113000000000000004", "summary": "a hint", "url": "https://x.com/t/1",
        "at": iso(at), "window": window(start, end, label="today"),
    }


class TeaseBurnTests(DecisionTestCase):
    def build(self, now, **kwargs):
        return AnnouncementShapeTests.build(self, now, **kwargs)

    def run_tease(self, now, **kwargs):
        return self.decide(now, self.build(now, **kwargs), doc=syrtis_doc(now, codex_weekly=40))

    def test_fresh_tease_burns(self) -> None:
        now = FIXTURE_NOW
        result = self.run_tease(now, teased=tease(now - timedelta(hours=1), now, now + timedelta(hours=10)))
        self.assertEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["announcement_source"], "inferred tease (codex-reset)")
        self.assertIn("treated as an announcement", result["reason"])

    def test_tease_spent_by_later_lunarwerx_reset_does_not_burn(self) -> None:
        now = FIXTURE_NOW
        result = self.run_tease(now, teased=tease(now - timedelta(hours=1), now, now + timedelta(hours=10)),
                                lunar_last=now - timedelta(minutes=30))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["spent_announcements"], 1)

    def test_tease_spent_by_later_confirmed_alert_does_not_burn(self) -> None:
        now = FIXTURE_NOW
        alert = {"kind": "reset", "state": "confirmed", "source_at": iso(now - timedelta(minutes=30))}
        result = self.run_tease(now, teased=tease(now - timedelta(hours=1), now, now + timedelta(hours=10)), alert=alert)
        self.assertNotEqual(result["mode"], "BURN")
        self.assertEqual(result["signals"]["spent_announcements"], 1)

    def test_tease_whose_window_ended_over_six_hours_ago_does_not_burn(self) -> None:
        now = FIXTURE_NOW
        result = self.run_tease(now, teased=tease(now - timedelta(hours=11), now - timedelta(hours=10), now - timedelta(hours=7)),
                                lunar_last=now - timedelta(hours=12))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("treated as expired", result["reason"])

    def test_tease_starting_30_hours_away_does_not_burn(self) -> None:
        now = FIXTURE_NOW
        result = self.run_tease(now, teased=tease(now - timedelta(hours=1), now + timedelta(hours=30), now + timedelta(hours=31)))
        self.assertNotEqual(result["mode"], "BURN")
        self.assertIn("more than 24 h away", result["reason"])


class FreshnessRound5Tests(DecisionTestCase):
    @staticmethod
    def sample_of(doc, pid, wk):
        entry = next(e for e in doc["series"] if e["providerId"] == pid and e["windowKey"] == wk)
        return entry["samples"][0]

    def test_quiet_weekly_window_is_usable_when_provider_is_fresh(self) -> None:
        now = FIXTURE_NOW
        doc = syrtis_doc(now, codex_weekly=2)
        self.sample_of(doc, "codex", "main.weekly.v1")["sampledAt"] = int((now - timedelta(minutes=40)).timestamp())
        result = self.decide(now, fixture_payloads(now), doc=doc)
        self.assertTrue(result["codex"]["ok"])
        self.assertEqual(result["codex"]["weekly_used"], 2.0)
        self.assertEqual(result["mode"], "PRIME")
        self.assertEqual(result["worker"], "sol")

    def test_both_windows_40_min_old_is_unknown(self) -> None:
        now = FIXTURE_NOW
        result = self.decide(now, fixture_payloads(now), doc=syrtis_doc(now, codex_weekly=2, age_minutes=40))
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("min old", result["reason"])

    def test_future_dated_quiet_window_is_still_invalid(self) -> None:
        now = FIXTURE_NOW
        doc = syrtis_doc(now, codex_weekly=2)
        self.sample_of(doc, "codex", "main.weekly.v1")["sampledAt"] = int((now + timedelta(minutes=10)).timestamp())
        result = self.decide(now, fixture_payloads(now), doc=doc)
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("in the future", result["reason"])

    def test_passed_weekly_reset_is_unknown_even_when_fresh(self) -> None:
        now = FIXTURE_NOW
        result = self.decide(now, fixture_payloads(now), doc=syrtis_doc(now, codex_weekly=2, weekly_reset_days=-1))
        self.assertFalse(result["codex"]["ok"])
        self.assertIn("has passed", result["reason"])


if __name__ == "__main__":
    unittest.main()
