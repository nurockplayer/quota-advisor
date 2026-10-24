"""End-to-end CLI behaviour: output, decision log, done, next-peak, status, config errors."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import (
    FIXTURE_NOW,
    MON_0940_JST,
    MON_1455_JST,
    SAT_0930_JST,
    SYRTIS_FIXTURE,
    WED_1100_JST,
    fetcher_for,
    fixture_payloads,
    qa,
    syrtis_doc,
    write_config,
    write_json,
)

UTC = timezone.utc


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "state"
        self.cfg = write_config(self.dir, SYRTIS_FIXTURE)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, argv, *, now=FIXTURE_NOW, table=None, cfg=None):
        table = fixture_payloads(now) if table is None else table
        out, err = io.StringIO(), io.StringIO()
        code = qa.main(
            argv,
            now=now,
            config_path=cfg or self.cfg,
            state_dir=self.state,
            fetcher=fetcher_for(table),
            out=out,
            err=err,
        )
        return code, out.getvalue(), err.getvalue()

    def log_records(self) -> list[dict]:
        return qa.read_log(self.state)[0]


class DecisionOutputTests(CliTestCase):
    def test_plain_output_is_worker_name_only(self) -> None:
        code, out, err = self.run_cli(["--tier", "hard"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "sol\n")
        self.assertEqual(err, "")

    def test_routine_plain_output(self) -> None:
        code, out, _ = self.run_cli(["--tier", "routine", "--est", "1h"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "luna\n")

    def test_json_output_has_brief_fields_and_id(self) -> None:
        code, out, _ = self.run_cli(["--tier", "hard", "--est", "90m", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        for key in ("id", "worker", "mode", "reason", "est_minutes", "codex", "claude", "signals", "data_age"):
            self.assertIn(key, data)
        self.assertEqual(data["est_minutes"], 90)
        self.assertEqual(data["signals"]["est_source"], "--est")
        self.assertEqual(data["mode"], "PRIME")
        self.assertNotIn("start_after", data)

    def test_decision_is_logged_with_id(self) -> None:
        code, out, _ = self.run_cli(["--tier", "hard", "--json"])
        data = json.loads(out)
        records = self.log_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "decision")
        self.assertEqual(records[0]["id"], data["id"])
        self.assertEqual(records[0]["worker"], "sol")

    def test_tier_is_required_for_a_decision(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli([])
        self.assertEqual(ctx.exception.code, 2)

    def test_invalid_est_is_an_argument_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--tier", "hard", "--est", "soon"])
        self.assertEqual(ctx.exception.code, 2)

    def test_config_error_exits_2(self) -> None:
        bad = write_config(self.dir, SYRTIS_FIXTURE, extra="bogus_key = 1")
        code, out, err = self.run_cli(["--tier", "hard"], cfg=bad)
        self.assertEqual(code, 2)
        self.assertIn("unknown config keys: bogus_key", err)
        self.assertEqual(out, "")

    def test_now_flag_sets_the_clock(self) -> None:
        # Wed 11:00 JST is peak; with a fresh Syrtis sample at that moment, codex is on pace.
        now = WED_1100_JST
        doc_path = write_json(self.dir / "fresh.json", syrtis_doc(now, codex_weekly=40))
        cfg = write_config(self.dir, doc_path)
        code, out, _ = self.run_cli(
            ["--tier", "hard", "--json", "--now", "2026-10-28T02:00:00Z"],
            now=FIXTURE_NOW,
            table=fixture_payloads(now),
            cfg=cfg,
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data["signals"]["in_peak"])
        self.assertEqual(data["worker"], "sol")


class DoneAndEstimateTests(CliTestCase):
    def decision_id(self, now, est=None) -> str:
        argv = ["--tier", "hard", "--json"] + (["--est", est] if est else [])
        _, out, _ = self.run_cli(argv, now=now)
        return json.loads(out)["id"]

    def test_done_with_minutes_is_recorded(self) -> None:
        rid = self.decision_id(FIXTURE_NOW)
        code, out, err = self.run_cli(["done", rid, "--minutes", "95"], now=FIXTURE_NOW + timedelta(hours=3))
        self.assertEqual(code, 0, err)
        self.assertIn("recorded 95 min", out)
        done = [r for r in self.log_records() if r["kind"] == "done"]
        self.assertEqual(done[0]["minutes"], 95)
        self.assertEqual(done[0]["tier"], "hard")

    def test_done_defaults_to_elapsed_time_since_decision(self) -> None:
        rid = self.decision_id(FIXTURE_NOW)
        code, out, _ = self.run_cli(["done", rid], now=FIXTURE_NOW + timedelta(minutes=42))
        self.assertEqual(code, 0)
        self.assertIn("recorded 42 min", out)

    def test_done_unknown_id_fails(self) -> None:
        self.decision_id(FIXTURE_NOW)
        code, out, err = self.run_cli(["done", "nope", "--minutes", "10"])
        self.assertEqual(code, 1)
        self.assertIn("no decision with id 'nope'", err)

    def test_median_of_three_recorded_runs_drives_next_estimate(self) -> None:
        for minutes, offset in ((100, 0), (120, 1), (140, 2)):
            rid = self.decision_id(FIXTURE_NOW + timedelta(hours=offset))
            self.run_cli(["done", rid, "--minutes", str(minutes)], now=FIXTURE_NOW + timedelta(hours=offset, minutes=5))
        _, out, _ = self.run_cli(["--tier", "hard", "--json"], now=FIXTURE_NOW + timedelta(hours=5))
        data = json.loads(out)
        self.assertEqual(data["est_minutes"], 120)
        self.assertEqual(data["signals"]["est_source"], "median of 3 recorded runs")

    def test_routine_actuals_do_not_feed_hard_estimate(self) -> None:
        for offset in range(3):
            now = FIXTURE_NOW + timedelta(hours=offset)
            _, out, _ = self.run_cli(["--tier", "routine", "--json"], now=now)
            rid = json.loads(out)["id"]
            self.run_cli(["done", rid, "--minutes", "20"], now=now + timedelta(minutes=1))
        _, out, _ = self.run_cli(["--tier", "hard", "--json"], now=FIXTURE_NOW + timedelta(hours=5))
        self.assertEqual(json.loads(out)["est_minutes"], 150)


class NowOffsetTests(CliTestCase):
    def test_naive_now_is_rejected_with_exit_2(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--tier", "hard", "--now", "2026-10-28T02:00:00"])
        self.assertEqual(ctx.exception.code, 2)

    def test_now_with_offset_or_z_is_accepted(self) -> None:
        for value in ("2026-10-28T11:00:00+09:00", "2026-10-28T02:00:00Z"):
            with self.subTest(value=value):
                code, out, _ = self.run_cli(["--now", value, "next-peak"])
                self.assertEqual(code, 0)


class NextPeakTests(CliTestCase):
    def test_next_peak_at_0940_on_a_weekday(self) -> None:
        code, out, _ = self.run_cli(["next-peak"], now=MON_0940_JST)
        self.assertEqual(code, 0)
        self.assertIn("now: off-peak", out)
        self.assertIn("next peak start: 2026-10-26T10:00+09:00 (Mon)", out)
        self.assertIn("wrap-up by:      2026-10-26T09:50+09:00 (Mon)", out)

    def test_next_peak_at_1455_on_a_weekday(self) -> None:
        code, out, _ = self.run_cli(["next-peak"], now=MON_1455_JST)
        self.assertIn("next peak start: 2026-10-26T15:00+09:00 (Mon)", out)
        self.assertIn("wrap-up by:      2026-10-26T14:50+09:00 (Mon)", out)

    def test_next_peak_on_saturday_skips_to_monday(self) -> None:
        code, out, _ = self.run_cli(["next-peak"], now=SAT_0930_JST)
        self.assertIn("now: off-peak", out)
        self.assertIn("next peak start: 2026-10-26T10:00+09:00 (Mon)", out)

    def test_next_peak_during_peak_reports_current_window(self) -> None:
        code, out, _ = self.run_cli(["next-peak"], now=WED_1100_JST)
        self.assertIn("now: peak until 2026-10-28T13:00+09:00", out)
        self.assertIn("next peak start: 2026-10-28T15:00+09:00 (Wed)", out)

    def test_next_peak_does_not_write_the_log(self) -> None:
        self.run_cli(["next-peak"], now=MON_0940_JST)
        self.assertEqual(self.log_records(), [])


class StatusTests(CliTestCase):
    def test_status_shows_both_tiers_and_sources(self) -> None:
        code, out, err = self.run_cli(["status"])
        self.assertEqual(code, 0, err)
        self.assertIn("hard     -> sol", out)
        self.assertIn("routine  -> luna", out)
        self.assertIn("feed", out)
        self.assertIn("next peak start", out)
        self.assertEqual(self.log_records(), [])


class DecisionLogFailureTests(CliTestCase):
    def test_invalid_utf8_history_is_noted_and_decision_continues(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "decisions.log").write_bytes(b"\xff\xfe not utf-8 \x80\n")
        code, out, err = self.run_cli(["--tier", "hard", "--json"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertIn("decision history unavailable", data["reason"])
        self.assertEqual(data["worker"], "sol")

    def test_log_path_is_directory_still_prints_worker_and_exits_3(self) -> None:
        (self.state / "decisions.log").mkdir(parents=True)
        code, out, err = self.run_cli(["--tier", "hard"])
        self.assertEqual(code, 3)
        self.assertEqual(out, "sol\n")
        self.assertIn("NOT recorded", err)

    def test_done_fails_when_history_is_unreadable(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "decisions.log").write_bytes(b"\xff\xfe\n")
        code, out, err = self.run_cli(["done", "abc", "--minutes", "5"])
        self.assertEqual(code, 1)
        self.assertIn("decision history unavailable", err)

    def test_status_reports_unreadable_history(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "decisions.log").write_bytes(b"\xff\xfe\n")
        code, out, _ = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertIn("history    decision history unavailable", out)


class OverlappingConfigCliTests(CliTestCase):
    def test_overlapping_peak_windows_exit_2(self) -> None:
        bad = write_config(self.dir, SYRTIS_FIXTURE, extra='peak_windows = [["10:00", "13:00"], ["12:00", "15:00"]]')
        code, out, err = self.run_cli(["--tier", "hard"], cfg=bad)
        self.assertEqual(code, 2)
        self.assertIn("overlap", err)


class ConfigAndRecordingCliTests(CliTestCase):
    def test_nan_and_inf_config_values_exit_2(self) -> None:
        for extra in ("codex_session_limit = nan", "stale_minutes = inf", "margin_minutes = -inf"):
            with self.subTest(extra=extra):
                bad = write_config(self.dir, SYRTIS_FIXTURE, extra=extra)
                code, out, err = self.run_cli(["--tier", "hard"], cfg=bad)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")

    def test_percent_limit_above_100_exits_2(self) -> None:
        bad = write_config(self.dir, SYRTIS_FIXTURE, extra="codex_session_limit = 101")
        code, out, err = self.run_cli(["--tier", "hard"], cfg=bad)
        self.assertEqual(code, 2)
        self.assertIn("at most 100", err)

    def test_json_reports_null_id_when_log_append_fails(self) -> None:
        (self.state / "decisions.log").mkdir(parents=True)
        code, out, err = self.run_cli(["--tier", "hard", "--json"])
        self.assertEqual(code, 3)
        data = json.loads(out)
        self.assertIsNone(data["id"])
        self.assertFalse(data["recorded"])
        self.assertEqual(data["worker"], "sol")
        self.assertIn("NOT recorded", err)

    def test_json_reports_recorded_id_on_success(self) -> None:
        code, out, _ = self.run_cli(["--tier", "hard", "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(data["recorded"])
        self.assertIsNotNone(data["id"])


class RoundFourCliTests(CliTestCase):
    def test_margin_of_one_trillion_minutes_exits_2(self) -> None:
        bad = write_config(self.dir, SYRTIS_FIXTURE, extra="margin_minutes = 1e12")
        code, out, err = self.run_cli(["--tier", "hard"], cfg=bad)
        self.assertEqual(code, 2)
        self.assertIn("margin_minutes", err)

    def test_invalid_utf8_config_exits_2(self) -> None:
        bad = self.dir / "bad.toml"
        bad.write_bytes(b"syrtis_path = \"\xff\xfe\"\n")
        code, out, err = self.run_cli(["--tier", "hard"], cfg=bad)
        self.assertEqual(code, 2)
        self.assertIn("UnicodeDecodeError", err)

    def test_est_beyond_one_week_is_an_argument_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--tier", "hard", "--est", "99999m"])
        self.assertEqual(ctx.exception.code, 2)

    def test_done_minutes_beyond_one_week_exits_1(self) -> None:
        code, out, _ = self.run_cli(["--tier", "hard", "--json"])
        rid = json.loads(out)["id"]
        code, out, err = self.run_cli(["done", rid, "--minutes", "20000"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
