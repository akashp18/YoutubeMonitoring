from __future__ import annotations

from app.domain import AlertType, Availability, PlaybackStatus, RestrictionType
from app.services.change_detection import StatusChangeDetector

detector = StatusChangeDetector(playback_regression_threshold_ms=4000)
SHORT = {"id": 1, "video_id": "abcdefghijk", "title": "Example Short"}


def result(status: str, restriction: str = RestrictionType.NONE, start_ms: int | None = 500) -> dict:
    return {
        "availability_status": status,
        "playback_status": (
            PlaybackStatus.VERIFIED if status == Availability.PASS else PlaybackStatus.SKIPPED
        ),
        "restriction_type": restriction,
        "playback_start_time_ms": start_ms,
        "diagnostic_message": "diagnostic",
    }


def test_no_previous_result_produces_no_transition_alerts() -> None:
    assert detector.detect(None, result(Availability.PASS), SHORT) == []


def test_accessible_to_unavailable() -> None:
    events = detector.detect(
        result(Availability.PASS), result(Availability.RESTRICTED, RestrictionType.UNAVAILABLE), SHORT
    )
    assert len(events) == 1
    assert events[0].alert_type == AlertType.BECAME_UNAVAILABLE
    assert events[0].severity == "critical"


def test_accessible_to_age_restriction() -> None:
    events = detector.detect(
        result(Availability.PASS), result(Availability.RESTRICTED, RestrictionType.AGE_RESTRICTED), SHORT
    )
    assert events[0].alert_type == AlertType.BECAME_RESTRICTED


def test_accessible_to_playback_failure() -> None:
    events = detector.detect(result(Availability.PASS), result(Availability.FAILED), SHORT)
    assert events[0].alert_type == AlertType.PLAYBACK_FAILING


def test_recovery_from_restriction() -> None:
    events = detector.detect(result(Availability.RESTRICTED, RestrictionType.PRIVATE), result(Availability.PASS), SHORT)
    assert events[0].alert_type == AlertType.RECOVERED
    assert events[0].severity == "info"


def test_inconclusive_does_not_raise_false_alerts() -> None:
    assert detector.detect(result(Availability.PASS), result(Availability.INCONCLUSIVE), SHORT) == []
    assert detector.detect(result(Availability.PASS), result(Availability.WARNING), SHORT) == []


def test_inconclusive_to_restricted_is_reported() -> None:
    events = detector.detect(
        result(Availability.INCONCLUSIVE), result(Availability.RESTRICTED, RestrictionType.UNAVAILABLE), SHORT
    )
    assert events[0].alert_type == AlertType.BECAME_RESTRICTED


def test_playback_performance_regression() -> None:
    previous = result(Availability.PASS, start_ms=500)
    current = result(Availability.PASS, start_ms=6000)
    events = detector.detect(previous, current, SHORT)
    assert [event.alert_type for event in events] == [AlertType.PLAYBACK_PERFORMANCE]


def test_small_playback_difference_is_ignored() -> None:
    previous = result(Availability.PASS, start_ms=400)
    current = result(Availability.PASS, start_ms=900)
    assert detector.detect(previous, current, SHORT) == []


def test_same_status_produces_no_alerts() -> None:
    current = result(Availability.RESTRICTED, RestrictionType.PRIVATE)
    assert detector.detect(result(Availability.RESTRICTED, RestrictionType.PRIVATE), current, SHORT) == []


def test_dedupe_key_is_stable_per_short_and_type() -> None:
    events = detector.detect(result(Availability.PASS), result(Availability.FAILED), SHORT)
    assert detector.dedupe_key(5, events[0]) == detector.dedupe_key(5, events[0])
    assert detector.dedupe_key(5, events[0]).startswith("5:")
