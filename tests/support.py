"""Shared helpers for the quota-advisor tests. No network, no real home directory."""

from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quota_advisor as qa  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
FIXTURE_NOW = datetime(2026, 10, 23, 21, 56, 48, tzinfo=timezone.utc)
SYRTIS_FIXTURE = FIXTURES / "syrtis-quota-pace-history-v3.json"

# Local reference times (JST = UTC+9).
SAT_0930_JST = datetime(2026, 10, 24, 0, 30, tzinfo=timezone.utc)  # Sat 09:30 JST
MON_0940_JST = datetime(2026, 10, 26, 0, 40, tzinfo=timezone.utc)  # Mon 09:40 JST
MON_1455_JST = datetime(2026, 10, 26, 5, 55, tzinfo=timezone.utc)  # Mon 14:55 JST
WED_1330_JST = datetime(2026, 10, 28, 4, 30, tzinfo=timezone.utc)  # Wed 13:30 JST (off-peak)
WED_1100_JST = datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)  # Wed 11:00 JST (peak)
SAT_1100_JST = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)  # Sat 11:00 JST


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def fixture_payloads(now: datetime = FIXTURE_NOW) -> dict[str, dict]:
    """Fixture payloads with their timestamps moved to `now` so they count as fresh."""
    feed = load_fixture("codex-reset-feed.json")
    forecast = load_fixture("codex-reset-forecast.json")
    lunar = load_fixture("lunarwerx-stats.json")
    feed["fetched_at"] = iso(now)
    forecast["updated_at"] = iso(now)
    lunar["generatedAt"] = iso(now)
    return {qa.FEED_URL: feed, qa.FORECAST_URL: forecast, qa.LUNARWERX_URL: lunar}


def fetcher_for(table: dict[str, dict], fail: tuple[str, ...] = ()):
    """Fake fetcher: serves `table`, raises OSError for `fail` URLs and unknown URLs."""

    def fetch(url: str) -> dict:
        fetch.calls.append(url)
        if url in fail or url not in table:
            raise OSError(f"network unreachable: {url}")
        return copy.deepcopy(table[url])

    fetch.calls = []
    return fetch


def syrtis_doc(
    now: datetime,
    *,
    codex_session: float = 12.0,
    codex_weekly: float = 0.0,
    claude_session: float = 13.0,
    claude_weekly: float = 69.0,
    age_minutes: float = 2,
    weekly_reset_days: float = 3.5,
    schema: int = 4,
    drop: tuple[str, ...] = (),
) -> dict:
    """Synthetic Syrtis cache. Weekly window is `weekly_reset_days` from its reset (50% elapsed at 3.5)."""

    def sample(duration: int, used: float, reset_in: timedelta) -> dict:
        return {
            "resetAt": int((now + reset_in).timestamp()),
            "durationSeconds": duration,
            "usedPercent": used,
            "sampledAt": int((now - timedelta(minutes=age_minutes)).timestamp()),
            "origin": "test",
        }

    rows = [
        ("codex", "main.session.v1", 18000, codex_session, timedelta(hours=2)),
        ("codex", "main.weekly.v1", 604800, codex_weekly, timedelta(days=weekly_reset_days)),
        ("claude", "session.v1", 18000, claude_session, timedelta(hours=2)),
        ("claude", "weekly.v1", 604800, claude_weekly, timedelta(days=weekly_reset_days)),
    ]
    series = [
        {"providerId": pid, "windowKey": wk, "samples": [sample(d, u, r)]}
        for pid, wk, d, u, r in rows
        if wk not in drop
    ]
    return {"schemaVersion": schema, "series": series}


def write_json(path: Path, doc: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def write_config(directory: Path, syrtis_path: Path, extra: str = "") -> Path:
    path = directory / "config.toml"
    path.write_text(f'syrtis_path = "{syrtis_path}"\n{extra}\n', encoding="utf-8")
    return path


def evaluate_now(now, cfg_path, state_dir, fetcher, tier="hard", est=150, est_source="test"):
    cfg = qa.load_config(cfg_path, required=True)
    g = qa.gather(now, cfg, state_dir, fetcher)
    return qa.evaluate(now, cfg, g, tier, est, est_source)
