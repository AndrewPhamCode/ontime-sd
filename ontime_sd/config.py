"""Process configuration, read from the environment exactly once at startup.

Settings are frozen so no code path can mutate them mid run, and a change
requires a restart. That is correct for the collector: a restart is cheap and
cleanly logged, whereas live reload would interact badly with the in memory
prediction cache. See DESIGN.md ADR-0014.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"

VEHICLE_POSITIONS = "vehicle_positions"
TRIP_UPDATES = "trip_updates"
FEEDS = (VEHICLE_POSITIONS, TRIP_UPDATES)

# Maps our internal feed name to the path segment MTS uses. The mock server
# serves the same paths so that only the base URL differs between mock and
# production. See ADR-0012.
_FEED_PATHS = {
    VEHICLE_POSITIONS: "vehicle-positions-for-agency",
    TRIP_UPDATES: "trip-updates-for-agency",
}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of the environment."""

    database_url: str
    # repr is suppressed so the key cannot leak into a log line or traceback.
    mts_api_key: str = field(repr=False)
    mts_feed_base_url: str
    poll_interval_seconds: int
    prediction_change_threshold_seconds: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    health_port: int
    health_stale_after_seconds: int
    log_level: str
    mock_port: int
    mock_vehicle_count: int
    mock_failure_rate: float
    mock_slow_rate: float
    mock_slow_seconds: float
    mock_truncate_rate: float
    mock_feed_refresh_seconds: int
    mock_trip_update_style: str

    @classmethod
    def from_env(cls, *, load_env_file: bool = True) -> Settings:
        if load_env_file:
            # Real environment variables win over .env, which matters in CI.
            load_dotenv(PROJECT_ROOT / ".env", override=False)

        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError(
                "DATABASE_URL is not set. Copy .env.example to .env, or export it directly."
            )

        settings = cls(
            database_url=database_url,
            mts_api_key=os.environ.get("MTS_API_KEY", "").strip(),
            mts_feed_base_url=os.environ.get(
                "MTS_FEED_BASE_URL", "http://localhost:8081/api/api/gtfs_realtime"
            )
            .strip()
            .rstrip("/"),
            poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 30),
            prediction_change_threshold_seconds=_env_int("PREDICTION_CHANGE_THRESHOLD_SECONDS", 30),
            backoff_base_seconds=_env_float("BACKOFF_BASE_SECONDS", 1.0),
            backoff_max_seconds=_env_float("BACKOFF_MAX_SECONDS", 300.0),
            health_port=_env_int("HEALTH_PORT", 8080),
            health_stale_after_seconds=_env_int("HEALTH_STALE_AFTER_SECONDS", 300),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
            mock_port=_env_int("MOCK_PORT", 8081),
            mock_vehicle_count=_env_int("MOCK_VEHICLE_COUNT", 40),
            mock_failure_rate=_env_float("MOCK_FAILURE_RATE", 0.0),
            mock_slow_rate=_env_float("MOCK_SLOW_RATE", 0.0),
            mock_slow_seconds=_env_float("MOCK_SLOW_SECONDS", 5.0),
            mock_truncate_rate=_env_float("MOCK_TRUNCATE_RATE", 0.0),
            mock_feed_refresh_seconds=_env_int("MOCK_FEED_REFRESH_SECONDS", 30),
            mock_trip_update_style=os.environ.get("MOCK_TRIP_UPDATE_STYLE", "per_stop").strip(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("POLL_INTERVAL_SECONDS must be positive")
        if self.prediction_change_threshold_seconds < 0:
            raise ValueError("PREDICTION_CHANGE_THRESHOLD_SECONDS must not be negative")
        if self.backoff_base_seconds <= 0:
            raise ValueError("BACKOFF_BASE_SECONDS must be positive")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("BACKOFF_MAX_SECONDS must be at least BACKOFF_BASE_SECONDS")
        for name, rate in (
            ("MOCK_FAILURE_RATE", self.mock_failure_rate),
            ("MOCK_SLOW_RATE", self.mock_slow_rate),
            ("MOCK_TRUNCATE_RATE", self.mock_truncate_rate),
        ):
            if not 0.0 <= rate <= 1.0:
                raise ValueError(f"{name} must be between 0.0 and 1.0")
        if self.mock_feed_refresh_seconds <= 0:
            raise ValueError("MOCK_FEED_REFRESH_SECONDS must be positive")
        if self.mock_trip_update_style not in ("per_stop", "single_delay"):
            raise ValueError("MOCK_TRIP_UPDATE_STYLE must be per_stop or single_delay")

    @property
    def using_mock_feed(self) -> bool:
        return not self.mts_api_key

    def feed_url(self, feed: str, *, text_format: bool = False) -> str:
        """Build the feed URL for one feed.

        text_format swaps .pb for .pbtext, which MTS serves as human readable
        protobuf. That is the first thing to fetch when the real key arrives.
        """
        try:
            path = _FEED_PATHS[feed]
        except KeyError:
            raise ValueError(f"unknown feed {feed!r}, expected one of {FEEDS}") from None

        suffix = "pbtext" if text_format else "pb"
        url = f"{self.mts_feed_base_url}/{path}/MTS.{suffix}"
        if self.mts_api_key:
            url = f"{url}?key={self.mts_api_key}"
        return url

    def redacted_feed_url(self, feed: str) -> str:
        """Same URL with the key masked, safe to put in logs and poll_log."""
        url = self.feed_url(feed)
        if self.mts_api_key:
            url = url.replace(self.mts_api_key, "REDACTED")
        return url
