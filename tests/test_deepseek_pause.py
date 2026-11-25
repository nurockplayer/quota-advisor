"""CLI routing with the Owner's DeepSeek pause in effect."""

import json

import test_cli
from support import SAT_1100_JST, syrtis_doc, write_config, write_json


class DeepSeekPauseTests(test_cli.CliTestCase):
    def decide(self, tier, **quota):
        now = SAT_1100_JST
        cfg = write_config(self.dir, write_json(self.dir / "syrtis.json", syrtis_doc(now, **quota)))
        args = ["--tier", tier]
        code, out, _ = self.run_cli(args, now=now, cfg=cfg)
        self.assertEqual(code, 0)
        json_code, json_out, _ = self.run_cli([*args, "--json"], now=now, cfg=cfg)
        self.assertEqual(json_code, 0)
        data = json.loads(json_out)
        self.assertEqual(out, f"{data['worker']}\n")
        return data

    def test_ahead_off_peak_routes_both_tiers_to_codex(self):
        for tier in ("routine", "hard"):
            with self.subTest(tier=tier):
                data = self.decide(tier, codex_weekly=85)
                self.assertEqual(data["pace"]["codex"]["state"], "ahead")
                self.assertFalse(data["signals"]["in_peak"])
                self.assertTrue(data["signals"]["deepseek_fits"])
                self.assertEqual((data["worker"], data["mode"]), ("codex", "CODEX"))

    def test_ahead_off_peak_with_exhausted_codex_uses_fallback(self):
        for claude_session, worker, mode in ((13, "haiku", "FALLBACK"), (100, "none", "WAIT")):
            with self.subTest(worker=worker):
                data = self.decide("routine", codex_weekly=85, codex_session=100,
                                   claude_session=claude_session)
                self.assertEqual(data["pace"]["codex"]["state"], "ahead")
                self.assertTrue(data["signals"]["deepseek_fits"])
                self.assertFalse(data["codex"]["usable"])
                self.assertEqual((data["worker"], data["mode"]), (worker, mode))

    def test_other_routes_are_unchanged(self):
        for worker, mode, quota in (
            ("codex", "CODEX", {}),
            ("haiku", "FALLBACK", {"codex_session": 100}),
            ("none", "WAIT", {"codex_session": 100, "claude_session": 100}),
        ):
            with self.subTest(worker=worker):
                data = self.decide("routine", **quota)
                self.assertEqual((data["worker"], data["mode"]), (worker, mode))
