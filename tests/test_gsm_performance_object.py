"""Performance-screen policy checks using a manual transcription, not live OCR."""

import pytest

from services.image_objects import run_object_checks


@pytest.mark.parametrize(
    "changes,config,failure_engine",
    [
        ({}, {}, None),
        ({"acceptance_rate_pct": 0, "cancellation_rate_pct": 0, "completion_rate_pct": 0, "completed_trips": 0}, {}, None),
        ({"acceptance_rate_pct": None}, {}, "required_fields"),
        ({"cancellation_rate_pct": None}, {}, "required_fields"),
        ({"completion_rate_pct": None}, {}, "required_fields"),
        ({"period_raw_text": None}, {}, "required_fields"),
        ({"period_raw_text": "16/10/2026"}, {}, "constraints"),
        ({"period_raw_text": "16/11/2025"}, {}, "constraints"),
        ({"period_raw_text": "Không rõ"}, {}, "constraints"),
        ({"period_type": "MONTHLY", "period_raw_text": "11/2026"}, {}, None),
        (
            {"period_raw_text": "16/12/2027"},
            {"constraints": [
                {"field": "period_months", "op": "contains", "value": 12},
                {"field": "period_years", "op": "contains", "value": 2027},
            ]},
            None,
        ),
        ({"is_performance_screen": False}, {}, "boolean"),
        ({"is_legible": False}, {}, "legible"),
        ({"is_legible": None}, {}, "required_fields"),
        ({"capture_source": "UNKNOWN"}, {}, "enum"),
        ({}, {"allowed_capture_sources": "DIRECT_SCREENSHOT,PHOTO_OF_SCREEN"}, "enum"),
        ({"capture_source": "DIRECT_SCREENSHOT"}, {"allowed_capture_sources": "DIRECT_SCREENSHOT"}, None),
    ],
    ids=[
        "social-media-sample-with-pending-values-and-zero-five-star-trips",
        "visible-zero-rates-and-count-are-valid",
        "missing-acceptance-is-rejected",
        "missing-cancellation-is-rejected",
        "missing-completion-is-rejected",
        "missing-report-date-is-rejected",
        "wrong-month-is-rejected",
        "same-month-wrong-year-is-rejected",
        "unreadable-month-is-rejected",
        "monthly-tab-is-allowed-but-not-required",
        "slot-can-configure-another-month-and-year",
        "unrelated-screen-is-rejected",
        "unreadable-screen-is-rejected",
        "missing-legibility-is-rejected",
        "unknown-source-is-rejected",
        "slot-can-reject-social-media-capture",
        "slot-can-allow-only-direct-screenshots",
    ],
)
def test_performance_screen_submission_policy(changes, config, failure_engine):
    extraction = {
        "screen_title": "Hiệu suất hoạt động",
        "period_type": "DAILY",
        "period_raw_text": "16/11/2026",
        "acceptance_rate_pct": 100,
        "cancellation_rate_pct": 20,
        "completion_rate_pct": 80,
        "completed_trips": 4,
        "average_rating": None,
        "average_rating_status": "Cập nhật sau khi kết thúc ngày hiện tại.",
        "five_star_trips": 0,
        "revenue_km_odo_raw": "--/-- (km)",
        "revenue_km_odo_status": "Đang cập nhật",
        "capture_source": "SOCIAL_MEDIA_CAPTURE",
        "is_performance_screen": True,
        "is_legible": True,
    }
    extraction.update(changes)

    result = run_object_checks("gsm_performance", extraction, config)

    assert result["passed"] is (failure_engine is None), result["reasons"]
    if failure_engine:
        assert failure_engine in {item["engine_id"] for item in result["failures"]}
    assert result["extraction"]["average_rating"] is None
    assert result["extraction"]["five_star_trips"] == 0
    assert result["extraction"]["revenue_km_odo_raw"] == "--/-- (km)"


def test_three_rates_and_period_suffice_without_optional_business_fields():
    result = run_object_checks("gsm_performance", {
        "acceptance_rate_pct": 0,
        "cancellation_rate_pct": 0,
        "completion_rate_pct": 0,
        "period_raw_text": "16/11/2026",
        "capture_source": "SOCIAL_MEDIA_CAPTURE",
        "is_performance_screen": True,
        "is_legible": True,
    })

    assert result["passed"] is True, result["reasons"]
