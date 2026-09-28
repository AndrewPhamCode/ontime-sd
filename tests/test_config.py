"""Settings behavior. No database required."""

from __future__ import annotations

import pytest

from ontime_sd.config import TRIP_UPDATES, VEHICLE_POSITIONS, Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql://u:p@localhost:5433/db",
        "mts_api_key": "",
        "mts_feed_base_url": "http://localhost:8081/api/api/gtfs_realtime",
        "poll_interval_seconds": 30,
        "prediction_change_threshold_seconds": 30,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 300.0,
        "health_port": 8080,
        "health_stale_after_seconds": 300,
        "log_level": "INFO",
        "mock_port": 8081,
        "mock_vehicle_count": 40,
        "mock_failure_rate": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_missing_database_url_is_a_clear_error(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="DATABASE_URL is not set"):
        Settings.from_env(load_env_file=False)


def test_defaults_match_the_charter(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@localhost:5433/db")
    settings = Settings.from_env(load_env_file=False)

    # These four come straight from CLAUDE.md Phase 1 and should not drift.
    assert settings.poll_interval_seconds == 30
    assert settings.prediction_change_threshold_seconds == 30
    assert settings.backoff_max_seconds == 300.0
    assert settings.health_stale_after_seconds == 300


def test_api_key_never_appears_in_repr() -> None:
    settings = _settings(mts_api_key="super-secret-key")
    assert "super-secret-key" not in repr(settings)


def test_feed_urls_match_the_mts_shape() -> None:
    settings = _settings(
        mts_api_key="k123", mts_feed_base_url="https://realtime.sdmts.com/api/api/gtfs_realtime"
    )

    assert settings.feed_url(VEHICLE_POSITIONS) == (
        "https://realtime.sdmts.com/api/api/gtfs_realtime"
        "/vehicle-positions-for-agency/MTS.pb?key=k123"
    )
    assert settings.feed_url(TRIP_UPDATES) == (
        "https://realtime.sdmts.com/api/api/gtfs_realtime/trip-updates-for-agency/MTS.pb?key=k123"
    )


def test_pbtext_variant_is_available_for_inspecting_the_real_feed() -> None:
    settings = _settings(mts_api_key="k123")
    assert ".pbtext?key=k123" in settings.feed_url(VEHICLE_POSITIONS, text_format=True)


def test_redacted_url_is_safe_to_log() -> None:
    settings = _settings(mts_api_key="k123")
    redacted = settings.redacted_feed_url(VEHICLE_POSITIONS)
    assert "k123" not in redacted
    assert "REDACTED" in redacted


def test_no_key_means_mock_mode_and_no_query_string() -> None:
    settings = _settings(mts_api_key="")
    assert settings.using_mock_feed is True
    assert "?" not in settings.feed_url(VEHICLE_POSITIONS)


def test_unknown_feed_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown feed"):
        _settings().feed_url("bus_positions")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"poll_interval_seconds": 0}, "POLL_INTERVAL_SECONDS"),
        ({"prediction_change_threshold_seconds": -1}, "PREDICTION_CHANGE_THRESHOLD_SECONDS"),
        ({"backoff_base_seconds": 0}, "BACKOFF_BASE_SECONDS"),
        ({"backoff_base_seconds": 10.0, "backoff_max_seconds": 5.0}, "BACKOFF_MAX_SECONDS"),
        ({"mock_failure_rate": 1.5}, "MOCK_FAILURE_RATE"),
    ],
)
def test_invalid_settings_are_rejected_at_startup(overrides: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _settings(**overrides).validate()


def test_non_numeric_env_value_names_the_variable(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "postgresql://u:p@localhost:5433/db")
    clean_env.setenv("POLL_INTERVAL_SECONDS", "half a minute")
    with pytest.raises(ValueError, match="POLL_INTERVAL_SECONDS must be an integer"):
        Settings.from_env(load_env_file=False)


def test_settings_are_immutable() -> None:
    settings = _settings()
    with pytest.raises((AttributeError, TypeError)):
        settings.poll_interval_seconds = 5  # type: ignore[misc]
