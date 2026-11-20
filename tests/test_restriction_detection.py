from __future__ import annotations

from app.domain import Availability, RestrictionType
from app.services.restriction_detection import RestrictionDetectionService
from tests.helpers import (
    accessible_probe,
    age_gated_probe,
    base_signals,
    captcha_probe,
    no_player_probe,
    unavailable_probe,
)

service = RestrictionDetectionService()


def test_accessible_when_player_reports_ok() -> None:
    verdict = service.classify(accessible_probe())
    assert verdict.verdict == "ACCESSIBLE"
    assert verdict.restriction_type == RestrictionType.NONE
    assert service.availability_for(verdict) is None


def test_age_restriction() -> None:
    verdict = service.classify(age_gated_probe())
    assert verdict.verdict == "RESTRICTED"
    assert verdict.restriction_type == RestrictionType.AGE_RESTRICTED
    assert service.availability_for(verdict) == Availability.RESTRICTED


def test_unavailable_message() -> None:
    verdict = service.classify(unavailable_probe("This video is unavailable"))
    assert verdict.restriction_type == RestrictionType.UNAVAILABLE
    assert verdict.verdict == "RESTRICTED"


def test_private_video_message() -> None:
    verdict = service.classify(
        base_signals(
            playability={"status": "UNPLAYABLE", "reason": "This video is private"},
            video={"present": False},
            errorTexts=[],
        )
    )
    assert verdict.restriction_type == RestrictionType.PRIVATE
    assert verdict.verdict == "RESTRICTED"


def test_terminated_account_is_removed() -> None:
    verdict = service.classify(
        base_signals(
            playability={
                "status": "UNPLAYABLE",
                "reason": "This video is no longer available because the YouTube account "
                "associated with this video has been terminated.",
            },
            video={"present": False},
        )
    )
    assert verdict.restriction_type == RestrictionType.REMOVED


def test_region_block_message() -> None:
    verdict = service.classify(
        base_signals(
            playability={
                "status": "UNPLAYABLE",
                "reason": "The uploader has not made this video available in your country",
            },
            video={"present": False},
        )
    )
    assert verdict.restriction_type == RestrictionType.REGION_BLOCKED


def test_copyright_block_message() -> None:
    verdict = service.classify(
        base_signals(
            playability={
                "status": "UNPLAYABLE",
                "reason": "The uploader has blocked this video on copyright grounds",
            },
            video={"present": False},
        )
    )
    assert verdict.restriction_type == RestrictionType.COPYRIGHT


def test_community_guidelines_message() -> None:
    verdict = service.classify(
        base_signals(
            playability={
                "status": "UNPLAYABLE",
                "reason": "This video has been removed for violating YouTube's Community Guidelines",
            },
            video={"present": False},
        )
    )
    assert verdict.restriction_type == RestrictionType.COMMUNITY_GUIDELINES


def test_captcha_is_inconclusive_not_restricted() -> None:
    verdict = service.classify(captcha_probe())
    assert verdict.verdict == "INCONCLUSIVE"
    assert verdict.restriction_type == RestrictionType.CAPTCHA
    assert service.availability_for(verdict) == Availability.INCONCLUSIVE


def test_challenge_flag_wins_over_other_text() -> None:
    signals = accessible_probe(hasChallenge=True)
    signals["bodyText"] = "This video is unavailable"
    verdict = service.classify(signals)
    assert verdict.verdict == "INCONCLUSIVE"
    assert verdict.restriction_type == RestrictionType.CAPTCHA


def test_consent_wall_is_inconclusive() -> None:
    verdict = service.classify(
        base_signals(
            playability=None,
            video={"present": False},
            consentWall=True,
            bodyText="Before you continue to YouTube",
        )
    )
    assert verdict.restriction_type == RestrictionType.CONSENT_WALL
    assert verdict.verdict == "INCONCLUSIVE"


def test_error_container_text_without_playability() -> None:
    verdict = service.classify(
        base_signals(
            playability=None,
            video={"present": False},
            errorTexts=["This video is private"],
        )
    )
    assert verdict.restriction_type == RestrictionType.PRIVATE
    assert verdict.confidence == "explicit"


def test_playing_video_is_not_downgraded_by_unrelated_page_text() -> None:
    signals = accessible_probe()
    signals["bodyText"] = "This video is unavailable. This video is private."
    verdict = service.classify(signals)
    assert verdict.verdict == "ACCESSIBLE"


def test_no_player_and_no_message_is_inconclusive() -> None:
    verdict = service.classify(no_player_probe())
    assert verdict.verdict == "INCONCLUSIVE"
    assert verdict.restriction_type == RestrictionType.UNKNOWN


def test_http_404_is_unavailable() -> None:
    verdict = service.classify(no_player_probe(), http_status=404)
    assert verdict.restriction_type == RestrictionType.UNAVAILABLE
    assert verdict.verdict == "RESTRICTED"


def test_playback_error_status_is_failed() -> None:
    verdict = service.classify(
        base_signals(
            playability={"status": "ERROR", "reason": ""},
            video={"present": False},
        )
    )
    assert verdict.verdict == "FAILED"
    assert service.availability_for(verdict) == Availability.FAILED
