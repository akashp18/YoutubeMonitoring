"""StatusChangeDetector: compare a check with the previous successful one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain import AlertType, Availability, PlaybackStatus, RestrictionType


@dataclass
class ChangeEvent:
    alert_type: str
    severity: str
    message: str
    previous_status: str | None
    current_status: str
    restriction_type: str


class StatusChangeDetector:
    def __init__(
        self,
        playback_regression_threshold_ms: int = 4000,
        playback_regression_factor: float = 3.0,
    ) -> None:
        self.playback_regression_threshold_ms = max(500, int(playback_regression_threshold_ms))
        self.playback_regression_factor = max(1.5, float(playback_regression_factor))

    def detect(
        self,
        previous: dict[str, Any] | None,
        current: dict[str, Any],
        short: dict[str, Any],
    ) -> list[ChangeEvent]:
        if previous is None:
            return []
        prev_status = previous.get("availability_status")
        cur_status = current.get("availability_status")
        prev_restriction = previous.get("restriction_type") or RestrictionType.NONE
        cur_restriction = current.get("restriction_type") or RestrictionType.NONE
        diagnostic = (current.get("diagnostic_message") or "")[:400]
        title = short.get("title") or short.get("video_id")
        events: list[ChangeEvent] = []

        if cur_status == prev_status:
            pass
        elif prev_status in (Availability.PASS, Availability.WARNING) and cur_status == Availability.RESTRICTED:
            alert_type = (
                AlertType.BECAME_UNAVAILABLE
                if cur_restriction in (RestrictionType.UNAVAILABLE, RestrictionType.REMOVED)
                else AlertType.BECAME_RESTRICTED
            )
            events.append(
                ChangeEvent(
                    alert_type=alert_type,
                    severity="critical",
                    message=f"\u201c{title}\u201d is no longer publicly accessible "
                    f"({prev_status} \u2192 {cur_status}, {cur_restriction}). {diagnostic}",
                    previous_status=prev_status,
                    current_status=cur_status,
                    restriction_type=cur_restriction,
                )
            )
        elif prev_status in (Availability.PASS, Availability.WARNING) and cur_status == Availability.FAILED:
            events.append(
                ChangeEvent(
                    alert_type=AlertType.PLAYBACK_FAILING,
                    severity="critical",
                    message=f"Playback is failing for \u201c{title}\u201d "
                    f"({prev_status} \u2192 {cur_status}). {diagnostic}",
                    previous_status=prev_status,
                    current_status=cur_status,
                    restriction_type=cur_restriction,
                )
            )
        elif prev_status == Availability.INCONCLUSIVE and cur_status == Availability.RESTRICTED:
            events.append(
                ChangeEvent(
                    alert_type=AlertType.BECAME_RESTRICTED,
                    severity="warning",
                    message=f"\u201c{title}\u201d now shows an explicit restriction "
                    f"({cur_restriction}) after an inconclusive check. {diagnostic}",
                    previous_status=prev_status,
                    current_status=cur_status,
                    restriction_type=cur_restriction,
                )
            )
        elif prev_status in (Availability.RESTRICTED, Availability.FAILED) and cur_status == Availability.PASS:
            events.append(
                ChangeEvent(
                    alert_type=AlertType.RECOVERED,
                    severity="info",
                    message=f"\u201c{title}\u201d is publicly accessible and playing again "
                    f"({prev_status} \u2192 {cur_status}).",
                    previous_status=prev_status,
                    current_status=cur_status,
                    restriction_type=RestrictionType.NONE,
                )
            )

        # Playback performance regression (only meaningful when both checks played).
        if (
            cur_status == Availability.PASS
            and prev_status == Availability.PASS
            and current.get("playback_status") == PlaybackStatus.VERIFIED
            and previous.get("playback_status") == PlaybackStatus.VERIFIED
        ):
            cur_start = current.get("playback_start_time_ms")
            prev_start = previous.get("playback_start_time_ms")
            if (
                cur_start is not None
                and prev_start is not None
                and int(cur_start) >= self.playback_regression_threshold_ms
                and int(cur_start) > int(prev_start) * self.playback_regression_factor
            ):
                events.append(
                    ChangeEvent(
                        alert_type=AlertType.PLAYBACK_PERFORMANCE,
                        severity="warning",
                        message=(
                            f"Playback start time for \u201c{title}\u201d deteriorated: "
                            f"{int(prev_start)} ms \u2192 {int(cur_start)} ms."
                        ),
                        previous_status=prev_status,
                        current_status=cur_status,
                        restriction_type=RestrictionType.NONE,
                    )
                )
        return events

    @staticmethod
    def dedupe_key(short_id: int, event: ChangeEvent) -> str:
        return f"{short_id}:{event.alert_type}"
