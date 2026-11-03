#!/usr/bin/env python3
"""quota-advisor: pick the worker (sol, luna, deepseek, haiku) for the next task.

Standard library only. The decision rules are in docs/brief.md and README.md.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import tomllib
import urllib.request
import uuid
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

UTC = dt_timezone.utc

FEED_URL = "https://codex-reset.com/api/feed"
FORECAST_URL = "https://codex-reset.com/api/forecast"
LUNARWERX_URL = "https://codex.lunarwerx.com/api/v1/stats"

DEFAULT_CONFIG_PATH = "~/.config/quota-advisor/config.toml"
DEFAULT_STATE_DIR = "~/.local/state/quota-advisor"
DEFAULT_SYRTIS_PATH = (
    "~/Library/Application Support/com.nyanako.tokenbar/quota-pace-history-v3.json"
)

SYRTIS_SCHEMA_VERSION = 4
CODEX_WINDOWS = {"session": "main.session.v1", "weekly": "main.weekly.v1"}
CLAUDE_WINDOWS = {"session": "session.v1", "weekly": "weekly.v1"}
CACHE_TTL = timedelta(minutes=5)
MAX_RESPONSE_AGE = timedelta(hours=2)
FUTURE_TOLERANCE = timedelta(minutes=5)  # timestamps further ahead than this are unreadable
WINDOW_EXPIRY_GRACE = timedelta(hours=6)
TEASE_HORIZON = timedelta(hours=24)
TEASE_LABEL = "inferred tease (codex-reset)"
WEEKLY_DAY_SHARE = 100 / 7
MIN_ACTUALS = 3
TIERS = ("hard", "routine")
WORKER_FOR_TIER = {"hard": "sol", "routine": "luna"}
PEAK_WEEKDAYS = frozenset(range(5))  # Monday..Friday
ANNOUNCEMENT_TIME_KEYS = ("at", "announced_at", "time", "source_at", "effective_at", "start")

DEFAULT_EST = {"hard": 150, "routine": 60}

NUMBER_KEYS = {
    "prime_threshold",
    "daily_target_pct",
    "codex_session_limit",
    "claude_limit",
    "stale_minutes",
    "margin_minutes",
    "wrapup_minutes",
}
STRING_KEYS = {"timezone", "syrtis_path", "feed_url", "forecast_url", "lunarwerx_url"}
PERCENT_KEYS = {"prime_threshold", "daily_target_pct", "codex_session_limit", "claude_limit"}
MINUTE_KEYS = {"stale_minutes", "margin_minutes", "wrapup_minutes"}
MAX_MINUTES = 7 * 24 * 60  # one week; bounds config values, --est and recorded actuals
SESSION_SECONDS = 5 * 3600
WEEKLY_SECONDS = 7 * 86400
DURATION_TOLERANCE = 0.10
ROLE_NAMES = {"session": "5h", "weekly": "weekly"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    prime_threshold: float = 35.0
    daily_target_pct: float = 18.0
    codex_session_limit: float = 95.0
    claude_limit: float = 90.0
    stale_minutes: float = 30.0
    margin_minutes: float = 30.0
    wrapup_minutes: float = 10.0
    default_est_minutes: dict = field(default_factory=lambda: dict(DEFAULT_EST))
    peak_windows: tuple = (("10:00", "13:00"), ("15:00", "19:00"))
    timezone: str = "Asia/Tokyo"
    syrtis_path: str = DEFAULT_SYRTIS_PATH
    feed_url: str = FEED_URL
    forecast_url: str = FORECAST_URL
    lunarwerx_url: str = LUNARWERX_URL


def _parse_hhmm(text: Any) -> str:
    if not isinstance(text, str) or not re.fullmatch(r"\d{2}:\d{2}", text):
        raise ConfigError(f"peak window time must be HH:MM, got {text!r}")
    try:
        time.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"peak window time out of range: {text!r}") from exc
    return text


def config_from_dict(data: dict) -> Config:
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key in NUMBER_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{key} must be a number")
            number = _finite(value)
            if number is None or number < 0:
                raise ConfigError(f"{key} must be a finite non-negative number")
            if key in PERCENT_KEYS and number > 100:
                raise ConfigError(f"{key} is a percentage and must be at most 100")
            if key in MINUTE_KEYS and number > MAX_MINUTES:
                raise ConfigError(f"{key} must be at most {MAX_MINUTES} (one week)")
            values[key] = number
        elif key in STRING_KEYS:
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{key} must be a non-empty string")
            values[key] = value
        elif key == "default_est_minutes":
            if not isinstance(value, dict) or not set(value) <= set(TIERS):
                raise ConfigError("default_est_minutes takes only the keys hard and routine")
            merged = dict(DEFAULT_EST)
            for tier, minutes in value.items():
                if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 < minutes <= MAX_MINUTES:
                    raise ConfigError(f"default_est_minutes.{tier} must be an integer from 1 to {MAX_MINUTES}")
                merged[tier] = minutes
            values[key] = merged
        elif key == "peak_windows":
            if not isinstance(value, list) or not value:
                raise ConfigError("peak_windows must be a non-empty list of [start, end]")
            windows = []
            for pair in value:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ConfigError("each peak window must be [start, end]")
                start, end = _parse_hhmm(pair[0]), _parse_hhmm(pair[1])
                if not start < end:
                    raise ConfigError(f"peak window {start}-{end} must end after it starts")
                windows.append((start, end))
            ordered = sorted(windows)
            for (start1, end1), (start2, end2) in zip(ordered, ordered[1:]):
                if start2 < end1:
                    raise ConfigError(
                        f"peak windows {start1}-{end1} and {start2}-{end2} overlap"
                    )
            values[key] = tuple(ordered)
    try:
        ZoneInfo(values.get("timezone", Config.timezone))
    except Exception as exc:  # zoneinfo raises several exception types
        raise ConfigError(f"unknown timezone {values.get('timezone')!r}") from exc
    return replace(Config(), **values)


def load_config(path: Path, *, required: bool) -> Config:
    if not path.exists():
        if required:
            raise ConfigError(f"config file not found: {path}")
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {type(exc).__name__}") from exc
    return config_from_dict(data)


# ---------------------------------------------------------------- time helpers


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def has_utc_offset(text: str) -> bool:
    """True when an ISO 8601 string ends in Z or an explicit +HH:MM / -HHMM offset."""
    return bool(re.search(r"(?:[Zz]|[+-]\d{2}:?\d{2})$", text.strip()))


def from_unix(value: Any) -> Optional[datetime]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> Optional[float]:
    """Return value as a finite float, or None. Huge JSON integers never raise."""
    if not _is_number(value):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def local_fmt(dt: datetime, cfg: Config) -> str:
    local = dt.astimezone(ZoneInfo(cfg.timezone))
    return f"{local.isoformat(timespec='minutes')} ({local:%a})"


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _pct(value: Optional[float]) -> str:
    return "unknown" if value is None else f"{value:.1f}%"


# ---------------------------------------------------------------- peak windows


def _windows_on(day: date, cfg: Config) -> list[tuple[datetime, datetime]]:
    tz = ZoneInfo(cfg.timezone)
    return [
        (
            datetime.combine(day, time.fromisoformat(start), tzinfo=tz),
            datetime.combine(day, time.fromisoformat(end), tzinfo=tz),
        )
        for start, end in cfg.peak_windows
    ]


def peak_at(now: datetime, cfg: Config) -> Optional[tuple[datetime, datetime]]:
    """Return the DeepSeek peak window containing now, or None when off-peak."""
    local = now.astimezone(ZoneInfo(cfg.timezone))
    if local.weekday() not in PEAK_WEEKDAYS:
        return None
    for start, end in _windows_on(local.date(), cfg):
        if start <= now < end:
            return start, end
    return None


def next_peak(now: datetime, cfg: Config) -> tuple[datetime, datetime]:
    """Return the first peak window whose start is strictly after now."""
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    for offset in range(8):
        day = today + timedelta(days=offset)
        if day.weekday() not in PEAK_WEEKDAYS:
            continue
        for start, end in _windows_on(day, cfg):
            if start > now:
                return start, end
    raise RuntimeError("no peak window found within a week")


def next_off_peak(now: datetime, cfg: Config) -> datetime:
    """Earliest instant at or after now that is off-peak."""
    t = now
    while True:
        window = peak_at(t, cfg)
        if window is None:
            return t
        t = window[1]


# ---------------------------------------------------------------- Syrtis quota


@dataclass
class Provider:
    name: str
    ok: bool
    reason: Optional[str] = None
    session_used: Optional[float] = None
    weekly_used: Optional[float] = None
    weekly_elapsed_pct: Optional[float] = None
    sampled_at: Optional[datetime] = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self, usable: bool) -> dict:
        return {
            "ok": self.ok,
            "usable": usable,
            "reason": self.reason,
            "session_used": self.session_used,
            "weekly_used": self.weekly_used,
            "weekly_elapsed_pct": None
            if self.weekly_elapsed_pct is None
            else round(self.weekly_elapsed_pct, 1),
            "sampled_at": iso(self.sampled_at) if self.sampled_at else None,
        }


def _unknown_providers(reason: str) -> dict[str, Provider]:
    return {
        "codex": Provider("Codex", False, reason),
        "claude": Provider("Claude", False, reason),
    }


def _index_samples(series: list) -> dict[tuple[str, str], list[tuple[datetime, dict]]]:
    index: dict[tuple[str, str], list[tuple[datetime, dict]]] = {}
    for entry in series:
        if not isinstance(entry, dict):
            continue
        pid, wk, samples = entry.get("providerId"), entry.get("windowKey"), entry.get("samples")
        if not isinstance(pid, str) or not isinstance(wk, str) or not isinstance(samples, list):
            continue  # malformed entry: its windows show up as missing
        key = (pid, wk)
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            sampled = from_unix(sample.get("sampledAt"))
            if sampled is not None:
                index.setdefault(key, []).append((sampled, sample))
    return index


SANE_YEARS = (2000, 2200)


def _sane_time(dt: Optional[datetime]) -> bool:
    return dt is not None and SANE_YEARS[0] <= dt.year <= SANE_YEARS[1]


def _expected_duration(wk: str) -> Optional[float]:
    if wk.endswith("session.v1"):
        return float(SESSION_SECONDS)
    if wk.endswith("weekly.v1"):
        return float(WEEKLY_SECONDS)
    return None


def _check_window(
    samples: Optional[list[tuple[datetime, dict]]], wk: str, now: datetime, cfg: Config
) -> tuple[Optional[dict], Optional[str], Optional[datetime]]:
    """Return (usable window or None, problem or None, sampled_at if known)."""
    if not samples:
        return None, f"window {wk} missing", None
    sampled, sample = max(samples, key=lambda pair: pair[0])
    if not _sane_time(sampled):
        return None, f"window {wk} sampledAt is out of range", None
    raw_used = sample.get("usedPercent")
    raw_duration = sample.get("durationSeconds")
    raw_reset = sample.get("resetAt")
    if not _is_number(raw_used):
        return None, f"window {wk} has no usedPercent", sampled
    used = _finite(raw_used)
    if used is None or not 0 <= used <= 100:
        return None, f"window {wk} usedPercent {raw_used!r} is outside 0-100 or not finite", sampled
    duration = _finite(raw_duration)
    if duration is None:
        return None, f"window {wk} durationSeconds {raw_duration!r} is not a finite number", sampled
    expected = _expected_duration(wk)
    if expected is None:
        return None, f"window {wk} is not a recognised session or weekly window", sampled
    if abs(duration - expected) > expected * DURATION_TOLERANCE:
        return None, (
            f"window {wk} durationSeconds {duration:.0f} is not within 10% of {expected:.0f}"
        ), sampled
    reset = from_unix(raw_reset)
    if not _sane_time(reset):
        return None, f"window {wk} resetAt {raw_reset!r} is missing or out of range", sampled
    if sampled - now > FUTURE_TOLERANCE:
        return None, f"window {wk} sample timestamp {iso(sampled)} is in the future (clock skew?)", sampled
    passed_reset = None
    if reset <= now:
        if sampled >= reset:
            return None, (
                f"window {wk} sample at {iso(sampled)} is after its reset at {iso(reset)}; inconsistent"
            ), sampled
        # The window rolled over after its last sample. Assume a fresh window at 0% only
        # within the grace period and for one period; beyond that the window stays unknown.
        if now - reset > timedelta(minutes=cfg.stale_minutes):
            return None, (
                f"window {wk} reset at {iso(reset)} more than {cfg.stale_minutes:g} min ago "
                "with no new sample"
            ), sampled
        if now - reset >= timedelta(seconds=duration):
            return None, (
                f"window {wk} reset at {iso(reset)} more than one period ago with no new sample"
            ), sampled
        passed_reset = reset
        reset = reset + timedelta(seconds=duration)
        used = 0.0
    try:
        window_start = reset - timedelta(seconds=duration)
        elapsed = (now - window_start).total_seconds() / duration * 100
    except (OverflowError, ValueError):
        return None, f"window {wk} times overflow", sampled
    window = {
        "used": used,
        "elapsed_pct": min(max(elapsed, 0.0), 100.0),
        "sampled_at": sampled,
        "reset_passed_at": passed_reset,
    }
    return window, None, sampled


def read_syrtis(path: Path, now: datetime, cfg: Config) -> dict[str, Provider]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _unknown_providers(f"Syrtis cache not found at {path}")
    except (OSError, ValueError) as exc:
        return _unknown_providers(f"Syrtis cache unreadable ({type(exc).__name__})")
    if not isinstance(data, dict):
        return _unknown_providers("Syrtis cache is not a JSON object")
    version = data.get("schemaVersion")
    if version != SYRTIS_SCHEMA_VERSION:
        return _unknown_providers(
            f"Syrtis schemaVersion {version!r} is not known (expected {SYRTIS_SCHEMA_VERSION})"
        )
    series = data.get("series")
    if not isinstance(series, list):
        return _unknown_providers("Syrtis cache has no series list")

    index = _index_samples(series)
    providers: dict[str, Provider] = {}
    for pid, label, windows in (
        ("codex", "Codex", CODEX_WINDOWS),
        ("claude", "Claude", CLAUDE_WINDOWS),
    ):
        results = {
            role: _check_window(index.get((pid, wk)), wk, now, cfg)
            for role, wk in windows.items()
        }
        # Syrtis appends a sample only when a value changes, so a quiet window can be old
        # while the provider is still polled. Freshness is judged on the newest sample.
        sampled = [r[2] for r in results.values() if r[2] is not None]
        problems = [r[1] for r in results.values() if r[1]]
        newest = max(sampled) if sampled else None
        if newest is None:
            problems.insert(0, f"no usable {label} sample")
        elif now - newest > timedelta(minutes=cfg.stale_minutes):
            age_min = (now - newest).total_seconds() / 60
            problems.insert(0, f"newest {label} sample is {age_min:.0f} min old")
        if problems:
            providers[pid] = Provider(label, False, "; ".join(problems), sampled_at=newest)
            continue
        session, weekly = results["session"][0], results["weekly"][0]
        notes = [
            f"{label} {ROLE_NAMES[role]} window reset at {local_fmt(window['reset_passed_at'], cfg)}; "
            "assumed 0% until the next sample"
            for role, (window, _, _) in results.items()
            if window["reset_passed_at"] is not None
        ]
        providers[pid] = Provider(
            label,
            True,
            session_used=session["used"],
            weekly_used=weekly["used"],
            weekly_elapsed_pct=weekly["elapsed_pct"],
            sampled_at=newest,
            notes=notes,
        )
    return providers


# ---------------------------------------------------------------- network sources


@dataclass
class Source:
    name: str
    body: Optional[dict]
    error: Optional[str] = None
    observed_at: Optional[datetime] = None


def _read_cache(path: Path, url: str) -> Optional[tuple[datetime, dict]]:
    """Return the cached (received_at, body) only when it was fetched from this same URL."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        received = parse_time(raw["received_at"])
        cached_url = raw["url"]
        body = raw["body"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if received is None or cached_url != url or not isinstance(body, dict):
        return None
    return received, body


def _write_cache(path: Path, received: datetime, url: str, body: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"received_at": iso(received), "url": url, "body": body}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    except OSError:
        pass  # caching is an optimisation; a read-only state dir must not break decisions


def default_fetcher(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "quota-advisor/0.1 (+local CLI)"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_source(
    name: str,
    url: str,
    timestamp_key: str,
    now: datetime,
    cache_dir: Path,
    fetcher: Callable[[str], dict],
) -> Source:
    cache_file = cache_dir / f"{name}.json"
    cached = _read_cache(cache_file, url)
    body: Optional[dict] = None
    received: Optional[datetime] = None
    error: Optional[str] = None
    if cached is not None and timedelta(0) <= now - cached[0] < CACHE_TTL:
        received, body = cached
    else:
        try:
            fetched = fetcher(url)
        except Exception as exc:  # any failure means "no announcement known"
            error = f"fetch failed ({type(exc).__name__})"
        else:
            if isinstance(fetched, dict):
                body, received = fetched, now
                _write_cache(cache_file, now, url, fetched)
            else:
                error = "response is not a JSON object"
    if body is None:
        return Source(name, None, error or "unavailable")
    if body.get("stale") is True:
        return Source(name, None, "source reports stale: true")
    observed = parse_time(body.get(timestamp_key))
    if observed is None:
        return Source(name, None, f"source timestamp {timestamp_key!r} missing or invalid")
    if observed - now > FUTURE_TOLERANCE:
        return Source(
            name,
            None,
            f"response timestamp {iso(observed)} is in the future (clock skew?)",
            observed,
        )
    age = now - observed
    if age > MAX_RESPONSE_AGE:
        hours = age.total_seconds() / 3600
        return Source(name, None, f"response is {hours:.1f} h old (limit 2 h)", observed)
    return Source(name, body, None, observed)


@dataclass
class Gathered:
    providers: dict[str, Provider]
    feed: Source
    forecast: Source
    lunar: Source


def gather(
    now: datetime,
    cfg: Config,
    state_dir: Path,
    fetcher: Callable[[str], dict],
) -> Gathered:
    cache_dir = state_dir / "cache"
    return Gathered(
        providers=read_syrtis(Path(cfg.syrtis_path).expanduser(), now, cfg),
        feed=fetch_source("feed", cfg.feed_url, "fetched_at", now, cache_dir, fetcher),
        forecast=fetch_source(
            "forecast", cfg.forecast_url, "updated_at", now, cache_dir, fetcher
        ),
        lunar=fetch_source(
            "lunarwerx", cfg.lunarwerx_url, "generatedAt", now, cache_dir, fetcher
        ),
    )


# ---------------------------------------------------------------- reset announcements


@dataclass(frozen=True)
class Announcement:
    label: str
    at: datetime
    summary: str


@dataclass
class ResetState:
    last_reset_at: Optional[datetime]
    last_reset_source: Optional[str]
    live: list[Announcement]
    spent: list[Announcement]
    notes: list[str]
    info: dict


def _announcement_time(value: Any) -> Optional[datetime]:
    if isinstance(value, dict):
        for key in ANNOUNCEMENT_TIME_KEYS:
            parsed = parse_time(value.get(key))
            if parsed is not None:
                return parsed
        return None
    return parse_time(value)


def _summary(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("summary"), str):
        return value["summary"]
    return ""


def _window_deadline(window: Any) -> Optional[datetime]:
    if not isinstance(window, dict):
        return None
    return parse_time(window.get("target_at")) or parse_time(window.get("end_at"))


def reset_state(now: datetime, cfg: Config, g: Gathered) -> ResetState:
    notes: list[str] = []
    info: dict[str, Any] = {}

    # Every trusted reset time. An announcement is spent when any of these is at or after it.
    reset_times: list[tuple[datetime, str]] = []
    # Trusted sources that are present but unreadable make the ordering uncertain: no BURN.
    uncertain: list[str] = []
    if g.lunar.body is not None:
        raw = g.lunar.body.get("lastResetAt")
        parsed = parse_time(raw)
        if parsed is not None:
            reset_times.append((parsed, "lunarwerx"))
        else:
            uncertain.append(f"lunarwerx lastResetAt {raw!r} is missing or unreadable")
    else:
        notes.append(f"lunarwerx unavailable: {g.lunar.error}")

    forecast = g.forecast.body
    if forecast is not None:
        raw = forecast.get("last_reset_at")
        parsed = parse_time(raw)
        if parsed is not None:
            reset_times.append((parsed, "codex-reset forecast"))
        else:
            uncertain.append(f"forecast last_reset_at {raw!r} is missing or unreadable")
        info["forecast_mode"] = forecast.get("mode")
        alert = forecast.get("latest_alert")
        if isinstance(alert, dict):
            info["latest_alert"] = {
                "kind": alert.get("kind"),
                "state": alert.get("state"),
                "source_at": alert.get("source_at"),
            }
            if alert.get("kind") == "reset" and alert.get("state") == "confirmed":
                parsed = parse_time(alert.get("source_at"))
                if parsed is not None:
                    reset_times.append((parsed, "confirmed reset alert"))
                else:
                    uncertain.append(
                        f"confirmed reset alert source_at {alert.get('source_at')!r} is missing or unreadable"
                    )
    else:
        notes.append(f"codex-reset forecast unavailable: {g.forecast.error}")

    if g.feed.body is None:
        notes.append(f"codex-reset feed unavailable: {g.feed.error}")

    last: Optional[datetime] = None
    last_source: Optional[str] = None
    if reset_times:
        # max() keeps the first maximal item, so a tie keeps the earlier-listed source.
        last, last_source = max(reset_times, key=lambda pair: pair[0])

    # (label, announcement time, summary, window deadline or None)
    candidates: list[tuple[str, Optional[datetime], str, Optional[datetime]]] = []
    if g.feed.body is not None:
        signal = g.feed.body.get("signal")
        if isinstance(signal, dict) and signal.get("active") is True:
            candidates.append(
                ("feed.signal", parse_time(signal.get("at")), _summary(signal), None)
            )
    if forecast is not None:
        official = forecast.get("official_signal")
        if isinstance(official, dict):
            window = official.get("window")
            signal_type = official.get("signal_type") or "signal"
            info["official_signal"] = {
                "signal_type": signal_type,
                "signal_tier": official.get("signal_tier"),
                "at": official.get("at"),
                "window": window.get("label") if isinstance(window, dict) else None,
            }
            candidates.append(
                (
                    f"official_signal ({signal_type})",
                    parse_time(official.get("at")),
                    _summary(official),
                    _window_deadline(window),
                )
            )
        # A top-level commitment only counts when it is a dict carrying a time.
        commitment = forecast.get("commitment")
        if isinstance(commitment, dict) and _announcement_time(commitment) is not None:
            candidates.append(
                (
                    "forecast.commitment",
                    _announcement_time(commitment),
                    _summary(commitment),
                    _window_deadline(commitment.get("window")),
                )
            )
        teased = forecast.get("teased_window")
        if isinstance(teased, dict):
            window = teased.get("window") if isinstance(teased.get("window"), dict) else {}
            start = parse_time(window.get("start_at"))
            end = parse_time(window.get("end_at")) or start
            tease_at = parse_time(teased.get("at"))
            info["teased_window"] = {
                "at": teased.get("at"),
                "summary": _summary(teased),
                "window": window.get("label"),
                "start_at": window.get("start_at"),
                "end_at": window.get("end_at"),
            }
            if tease_at is None or start is None:
                notes.append("codex-reset teased window has no readable at or start_at; not used for BURN")
            elif start - now > TEASE_HORIZON:
                notes.append("codex-reset teased window starts more than 24 h away; not used for BURN")
            else:
                candidates.append((TEASE_LABEL, tease_at, _summary(teased), end))

    if uncertain:
        notes.append("reset ordering uncertain (" + "; ".join(uncertain) + "), so no BURN")
    elif last is None:
        notes.append("latest reset unknown, so no BURN")

    live: list[Announcement] = []
    spent: list[Announcement] = []
    for label, at, summary, deadline in candidates:
        if at is None:
            notes.append(f"{label} has no readable time, so it is not used for BURN")
            continue
        if at - now > FUTURE_TOLERANCE:
            notes.append(f"{label} time {iso(at)} is more than 5 min in the future; ignored")
            continue
        if uncertain or last is None:
            continue
        if last >= at:
            spent.append(Announcement(label, at, summary))
            continue
        if deadline is not None and now - deadline > WINDOW_EXPIRY_GRACE:
            notes.append(
                f"{label} window ended {iso(deadline)} with no reset seen; treated as expired, no BURN"
            )
            continue
        live.append(Announcement(label, at, summary))
    return ResetState(last, last_source, live, spent, notes, info)

def evaluate(
    now: datetime,
    cfg: Config,
    g: Gathered,
    tier: str,
    est_minutes: int,
    est_source: str,
) -> dict:
    reset = reset_state(now, cfg, g)
    codex, claude = g.providers["codex"], g.providers["claude"]
    codex_limit = min(cfg.codex_session_limit, 100.0)
    claude_limit = min(cfg.claude_limit, 100.0)
    codex_usable = (
        codex.ok and codex.session_used < codex_limit and codex.weekly_used < 100
    )
    claude_usable = (
        claude.ok and claude.session_used < claude_limit and claude.weekly_used < claude_limit
    )
    codex_worker = WORKER_FOR_TIER[tier]
    peak = peak_at(now, cfg)
    peak_start, _ = next_peak(now, cfg)
    wrap_up = peak_start - timedelta(minutes=cfg.wrapup_minutes)
    task_end = now + timedelta(minutes=est_minutes + cfg.margin_minutes)
    wrap_up_fit = now + timedelta(minutes=est_minutes) <= wrap_up
    deepseek_fits = peak is None and task_end <= peak_start and wrap_up_fit
    start_after: Optional[datetime] = None

    notes = list(reset.notes)
    for prov in (codex, claude):
        if not prov.ok:
            notes.append(f"{prov.name} quota unknown: {prov.reason}")
        notes.extend(prov.notes)
    for item in reset.spent:
        notes.append(f"{item.label} announcement from {local_fmt(item.at, cfg)} predates the last reset; spent")

    codex_state = (
        f"Codex 5h {_pct(codex.session_used)}, weekly {_pct(codex.weekly_used)}"
    )
    announced = reset.live[0] if reset.live else None
    daily_target = None
    if codex.weekly_elapsed_pct is not None:
        day = min(int(codex.weekly_elapsed_pct // WEEKLY_DAY_SHARE) + 1, 7)
        target = min(cfg.daily_target_pct * day, 100.0)
        daily_target = {"day": day, "target_pct": target}

    if announced is not None and codex_usable:
        mode, worker = "BURN", codex_worker
        primary = (
            f"Reset announced ({announced.label}, {local_fmt(announced.at, cfg)}) is after "
            f"the last reset {local_fmt(reset.last_reset_at, cfg)}; {codex_state}, Codex usable any hour."
        )
        if announced.label == TEASE_LABEL:
            primary += " Basis: inferred tease (codex-reset), treated as an announcement."
    elif cfg.daily_target_pct > 0 and codex_usable and daily_target is not None and codex.weekly_used < target:
        mode, worker = "TARGET", codex_worker
        primary = (
            f"Day {day} of the Codex week: weekly {_pct(codex.weekly_used)} is under today's "
            f"target {target:g}% ({cfg.daily_target_pct:g}% x day); {codex_state}, Codex any hour."
        )
    elif codex_usable and codex.weekly_used < cfg.prime_threshold:
        mode, worker = "PRIME", codex_worker
        primary = (
            f"Just after a reset: Codex weekly {_pct(codex.weekly_used)} is under the prime "
            f"threshold {cfg.prime_threshold:g}% ({codex_state})."
        )
    else:
        mode = "SAVE"
        prefix = ""
        if announced is not None:
            prefix = (
                f"Reset announced ({local_fmt(announced.at, cfg)}) but Codex is not usable "
                f"({codex_state}). "
            )
        if deepseek_fits:
            worker = "deepseek"
            primary = (
                f"Off-peak, and the task ({est_minutes} min, {est_source}) with margin ends "
                f"{local_fmt(task_end, cfg)}, before the next peak at {local_fmt(peak_start, cfg)}."
            )
        else:
            why = (
                f"it is peak now until {local_fmt(peak[1], cfg)}"
                if peak is not None
                else f"the task would run past the peak start {local_fmt(peak_start, cfg)}"
            )
            allowance = f"elapsed {codex.weekly_elapsed_pct:.0f}% + {WEEKLY_DAY_SHARE:.1f}% allowance" if codex.ok else ""
            if codex_usable and codex.weekly_used <= codex.weekly_elapsed_pct + WEEKLY_DAY_SHARE:
                worker = codex_worker
                primary = (
                    f"DeepSeek not usable because {why}; Codex weekly {_pct(codex.weekly_used)} "
                    f"is on pace ({allowance})."
                )
            elif claude_usable:
                worker = "haiku"
                if codex_usable:
                    codex_part = (
                        f"Codex weekly {_pct(codex.weekly_used)} is ahead of pace ({allowance})"
                    )
                else:
                    codex_part = f"Codex is not usable ({codex_state})"
                primary = (
                    f"DeepSeek not usable because {why}; {codex_part}; Claude 5h "
                    f"{_pct(claude.session_used)} and weekly {_pct(claude.weekly_used)} are under "
                    f"{cfg.claude_limit:g}%."
                )
            else:
                worker = "deepseek"
                start_after = next_off_peak(now, cfg) if peak is not None else next_off_peak(peak_start, cfg)
                primary = (
                    f"DeepSeek runs at peak price because {why}, and no cheaper worker is usable "
                    f"(pay-as-you-go beats stalling). Cheapest start: {local_fmt(start_after, cfg)}."
                )
        primary = prefix + primary

    reason = primary
    if notes:
        reason += " Notes: " + "; ".join(notes) + "."

    def age_seconds(source: Source) -> Optional[float]:
        if source.body is None or source.observed_at is None:
            return None
        return max(0.0, round((now - source.observed_at).total_seconds(), 1))

    sample_times = [p.sampled_at for p in (codex, claude) if p.sampled_at is not None]
    result: dict[str, Any] = {
        "worker": worker,
        "mode": mode,
        "reason": reason,
    }
    if start_after is not None:
        result["start_after"] = iso(start_after)
    result.update(
        {
            "est_minutes": est_minutes,
            "codex": codex.as_dict(codex_usable),
            "claude": claude.as_dict(claude_usable),
            "signals": {
                "daily_target": daily_target,
                "announced": announced is not None,
                "announcement_at": iso(announced.at) if announced else None,
                "announcement_source": announced.label if announced else None,
                "last_reset_at": iso(reset.last_reset_at) if reset.last_reset_at else None,
                "last_reset_source": reset.last_reset_source,
                "spent_announcements": len(reset.spent),
                "reset_signals": reset.info,
                "in_peak": peak is not None,
                "next_peak_start": iso(peak_start),
                "wrap_up_at": iso(wrap_up),
                "deepseek_fits": deepseek_fits,
                "est_source": est_source,
            },
            "data_age": {
                "syrtis_seconds": round((now - min(sample_times)).total_seconds(), 1)
                if sample_times
                else None,
                "feed_seconds": age_seconds(g.feed),
                "forecast_seconds": age_seconds(g.forecast),
                "lunarwerx_seconds": age_seconds(g.lunar),
            },
        }
    )
    return result


# ---------------------------------------------------------------- estimates and log


def parse_est(text: str) -> int:
    """Parse '90m', '2h', '1h30m', '1h30', '1.5h', ' 2H ' or a bare number of minutes (max one week)."""
    raw = text.strip()
    try:
        if raw.isdigit():
            minutes = float(int(raw))
        else:
            match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+)m?)?", raw, flags=re.IGNORECASE)
            if not match or not any(match.groups()):
                raise ValueError("no match")
            hours, mins = match.groups()
            minutes = float(hours or 0) * 60 + int(mins or 0)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError(
            f"cannot parse duration {text!r} (use e.g. 90m, 2h, 1h30m)"
        ) from None
    rounded = round(minutes) if math.isfinite(minutes) else 0
    if not 0 < rounded <= MAX_MINUTES:
        raise argparse.ArgumentTypeError(f"duration must be at least 1 and at most {MAX_MINUTES} minutes")
    return rounded


def read_log(state_dir: Path) -> tuple[list[dict], Optional[str]]:
    """Return (records, note). An unreadable log yields no records plus a note, never an exception."""
    path = state_dir / "decisions.log"
    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records, None
    except (OSError, UnicodeDecodeError) as exc:
        return records, f"decision history unavailable ({type(exc).__name__}); estimate uses defaults"
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, None


def append_log(state_dir: Path, record: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "decisions.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def recorded_minutes(records: list[dict], tier: str) -> list[float]:
    """Latest recorded actual per decision of this tier. Malformed records are ignored."""
    tiers: dict[str, Any] = {}
    for r in records:
        if r.get("kind") == "decision" and isinstance(r.get("id"), str):
            tiers[r["id"]] = r.get("tier")
    latest: dict[str, float] = {}
    for r in records:
        if r.get("kind") != "done":
            continue
        rid = r.get("id")
        if not isinstance(rid, str) or rid not in tiers or tiers[rid] != tier:
            continue
        minutes = _finite(r.get("minutes"))
        if minutes is None or not 0 < minutes < MAX_MINUTES:
            continue
        latest[rid] = minutes
    return list(latest.values())


def resolve_est(
    tier: str, est_arg: Optional[int], records: list[dict], cfg: Config
) -> tuple[int, str]:
    if est_arg is not None:
        return est_arg, "--est"
    actual = recorded_minutes(records, tier)
    if len(actual) >= MIN_ACTUALS:
        return int(round(statistics.median(actual))), f"median of {len(actual)} recorded runs"
    return int(cfg.default_est_minutes[tier]), "default"


# ---------------------------------------------------------------- commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quota-advisor",
        description="Decide which worker (sol, luna, deepseek, haiku) takes the next task.",
    )
    parser.add_argument("--tier", choices=TIERS, help="task tier (required for a decision)")
    parser.add_argument("--est", metavar="DURATION", type=parse_est, help="estimate, e.g. 90m, 2h")
    parser.add_argument("--json", action="store_true", help="print the full decision as JSON")
    parser.add_argument("--config", metavar="PATH", help="config file (default: ~/.config/quota-advisor/config.toml)")
    parser.add_argument("--state-dir", metavar="PATH", help="state directory (default: ~/.local/state/quota-advisor)")
    parser.add_argument("--now", metavar="ISO8601", help="pretend the current time, for what-if checks")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show mode, sources, ages and next peak")
    sub.add_parser("next-peak", help="show the next DeepSeek peak start and wrap-up time")
    done = sub.add_parser("done", help="record the actual duration of a decision")
    done.add_argument("id", help="decision id printed by --json")
    done.add_argument("--minutes", type=int, help="actual minutes (default: now minus decision time)")
    return parser


def _cmd_next_peak(now: datetime, cfg: Config, out) -> int:
    current = peak_at(now, cfg)
    if current is None:
        out.write(f"now: off-peak, {local_fmt(now, cfg)}\n")
    else:
        out.write(f"now: peak until {local_fmt(current[1], cfg)} (DeepSeek at peak price)\n")
    start, _ = next_peak(now, cfg)
    wrap_up = start - timedelta(minutes=cfg.wrapup_minutes)
    out.write(f"next peak start: {local_fmt(start, cfg)}\n")
    out.write(f"wrap-up by:      {local_fmt(wrap_up, cfg)}\n")
    return 0


def _cmd_status(now: datetime, cfg: Config, state_dir: Path, fetcher, out) -> int:
    g = gather(now, cfg, state_dir, fetcher)
    records, history_note = read_log(state_dir)
    out.write(f"quota-advisor status at {local_fmt(now, cfg)}\n")
    if history_note:
        out.write(f"history    {history_note}\n")
    for prov in g.providers.values():
        if prov.ok:
            out.write(
                f"{prov.name:<7} 5h {_pct(prov.session_used)} | weekly {_pct(prov.weekly_used)}"
                f" (window elapsed {prov.weekly_elapsed_pct:.0f}%)\n"
            )
        else:
            out.write(f"{prov.name:<7} unknown: {prov.reason}\n")
    for src in (g.feed, g.forecast, g.lunar):
        if src.body is None:
            out.write(f"{src.name:<10} unavailable: {src.error}\n")
        else:
            age = (now - src.observed_at).total_seconds() / 60 if src.observed_at else 0
            out.write(f"{src.name:<10} data {age:.0f} min old\n")
    for tier in TIERS:
        est, est_source = resolve_est(tier, None, records, cfg)
        result = evaluate(now, cfg, g, tier, est, est_source)
        out.write(f"{tier:<8} -> {result['worker']} [{result['mode']}] {result['reason']}\n")
    peak_start, _ = next_peak(now, cfg)
    out.write(f"next peak start: {local_fmt(peak_start, cfg)}\n")
    return 0


def _cmd_decide(args, now: datetime, cfg: Config, state_dir: Path, fetcher, out, err) -> int:
    records, history_note = read_log(state_dir)
    est, est_source = resolve_est(args.tier, args.est, records, cfg)
    g = gather(now, cfg, state_dir, fetcher)
    result = evaluate(now, cfg, g, args.tier, est, est_source)
    if history_note:
        result["reason"] += f" Notes: {history_note}."
    decision_id = uuid.uuid4().hex[:12]
    exit_code = 0
    recorded = True
    try:
        append_log(
            state_dir,
            {
                "kind": "decision",
                "id": decision_id,
                "ts": iso(now),
                "tier": args.tier,
                "est_minutes": est,
                "worker": result["worker"],
                "mode": result["mode"],
                "reason": result["reason"],
            },
        )
    except OSError as exc:
        err.write(
            f"warning: decision was NOT recorded in the decision log "
            f"({exc.strerror or type(exc).__name__}); `done` cannot be used for it\n"
        )
        recorded = False
        exit_code = 3
    if args.json:
        payload = {"id": decision_id if recorded else None, "recorded": recorded, **result}
        out.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        out.write(result["worker"] + "\n")
    return exit_code


def _cmd_done(args, now: datetime, state_dir: Path, out, err) -> int:
    records, history_note = read_log(state_dir)
    if history_note:
        err.write(history_note + "\n")
        return 1
    decision = next(
        (r for r in records if r.get("kind") == "decision" and r.get("id") == args.id), None
    )
    if decision is None:
        err.write(f"no decision with id {args.id!r} in {state_dir / 'decisions.log'}\n")
        return 1
    if args.minutes is not None:
        minutes = args.minutes
    else:
        started = parse_time(decision.get("ts"))
        if started is None:
            err.write("decision has no readable timestamp; pass --minutes\n")
            return 1
        minutes = round((now - started).total_seconds() / 60)
    if not 0 < minutes <= MAX_MINUTES:
        err.write(f"recorded duration must be more than 0 and at most {MAX_MINUTES} minutes\n")
        return 1
    try:
        append_log(
            state_dir,
            {"kind": "done", "id": args.id, "ts": iso(now), "tier": decision.get("tier"), "minutes": minutes},
        )
    except OSError as exc:
        err.write(f"could not write decision log ({exc.strerror or type(exc).__name__})\n")
        return 1
    out.write(f"recorded {minutes} min for {args.id} ({decision.get('tier')})\n")
    return 0


def main(
    argv: Optional[list[str]] = None,
    *,
    now: Optional[datetime] = None,
    config_path: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    fetcher: Optional[Callable[[str], dict]] = None,
    out=None,
    err=None,
) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None and args.tier is None:
        parser.error("--tier is required (or use status, next-peak or done)")
    if args.command is not None and args.tier is not None:
        parser.error("--tier cannot be combined with a subcommand")

    clock = now or datetime.now(UTC)
    if args.now is not None:
        clock = parse_time(args.now)
        if clock is None:
            parser.error(f"--now must be an ISO 8601 time, got {args.now!r}")
        if not has_utc_offset(args.now):
            parser.error(
                f"--now {args.now!r} has no UTC offset; add one such as +09:00 or Z"
            )

    try:
        if args.config is not None:
            cfg = load_config(Path(args.config).expanduser(), required=True)
        else:
            cfg = load_config(config_path or Path(DEFAULT_CONFIG_PATH).expanduser(), required=False)
    except ConfigError as exc:
        err.write(f"config error: {exc}\n")
        return 2

    sdir = (
        Path(args.state_dir).expanduser()
        if args.state_dir
        else (state_dir or Path(DEFAULT_STATE_DIR).expanduser())
    )
    fetch = fetcher or default_fetcher

    if args.command == "next-peak":
        return _cmd_next_peak(clock, cfg, out)
    if args.command == "status":
        return _cmd_status(clock, cfg, sdir, fetch, out)
    if args.command == "done":
        return _cmd_done(args, clock, sdir, out, err)
    return _cmd_decide(args, clock, cfg, sdir, fetch, out, err)


if __name__ == "__main__":
    sys.exit(main())
