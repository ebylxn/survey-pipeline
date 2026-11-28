# -*- coding: utf-8 -*-
"""File-defined image object types, check engines, and admin API."""

import io
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from main import FORM_REGISTRY, app
from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig, QuestionType
from services.image_check_engines import (
    EngineDefinitionError,
    conditions_pass,
    evaluate_condition,
    resolve_placeholders,
    run_engine,
    safe_arithmetic,
)
from services.image_objects import (
    ImageObjectDefinitionError,
    build_detection_prompt,
    delete_object,
    get_object,
    list_engines,
    list_objects,
    load_registry,
    merged_config,
    object_type_for_classified_role,
    resolve_slot_object,
    run_definition_checks,
    run_object_checks,
    save_object,
)
from services.image_object_projection import project_income_totals, project_income_weeks
from services.image_types import normalize_ocr_field_aliases


def _weekly_config(**overrides):
    config = {"period_type": "WEEKLY", "constraints": []}
    config.update(overrides)
    return config


# ==============================================================================
# Registry & schema
# ==============================================================================

def test_registry_loads_object_and_engine_files():
    registry = load_registry(force=True)
    assert registry["errors"] == []
    ids = {obj.id for obj in registry["objects"].values()}
    assert {"weekly_income", "app_profile", "vehicle_exterior_or_interior"} <= ids
    implementations = {engine.implementation for engine in registry["engines"].values()}
    assert {"required_fields", "date_range", "number_range", "arithmetic", "regex", "enum", "period_granularity", "constraints"} <= implementations
    assert all(engine.implementation for engine in list_engines())


def test_missing_object_fails_loudly():
    with pytest.raises(ImageObjectDefinitionError):
        get_object("does_not_exist")


def test_customizable_defaults_and_slot_overrides():
    obj = get_object("weekly_income")
    defaults = merged_config(obj, {})
    assert defaults["period_type"] == "WEEKLY"
    assert defaults["constraints"] == []

    overridden = merged_config(obj, {
        "period_type": "MONTHLY",
        "constraints": [{"field": "period_months", "op": "contains", "value": 10}],
    })
    assert overridden["period_type"] == "MONTHLY"
    assert overridden["constraints"][0]["field"] == "period_months"


def test_constraints_item_fields_are_validated():
    from services.image_objects import CustomizableField

    field = CustomizableField(
        key="constraints",
        label="Ràng buộc",
        type="constraints",
        item_fields=[{"key": "field", "label": "Trường", "type": "select"}],
    )
    assert field.item_fields[0]["key"] == "field"

    with pytest.raises(ValueError):
        CustomizableField(key="bad", label="Bad", type="text", item_fields=[{"key": "field", "label": "Trường"}])

    with pytest.raises(ValueError):
        CustomizableField(key="missing", label="Missing", type="constraints")


def test_placeholder_substitution_keeps_type_and_renders_text():
    config = {"period_type": "WEEKLY", "duration": 7}
    resolved = resolve_placeholders(
        {"expected": "{{period_type}}", "duration": "{{duration}}", "label": "Kỳ {{period_type}}/{{duration}}"},
        config,
    )
    assert resolved["expected"] == "WEEKLY"
    assert resolved["duration"] == 7
    assert resolved["label"] == "Kỳ WEEKLY/7"


def test_detection_prompt_is_built_from_object_and_slot_config():
    obj = get_object("weekly_income")
    config = {
        "period_type": "MONTHLY",
        "constraints": [{"field": "period_months", "op": "contains", "value": 10}],
    }
    prompt = build_detection_prompt(obj, config)
    assert "driver income screen" in prompt
    assert "Doanh thu gộp" in prompt
    assert "MONTHLY" in prompt
    assert "period_months" in prompt


def test_slot_object_resolution_and_legacy_role_compatibility():
    obj = resolve_slot_object({"object_type": "weekly_income"})
    assert obj.id == "weekly_income"
    assert object_type_for_classified_role("app_income") == "weekly_income"
    assert object_type_for_classified_role("app_income_week_3") == "weekly_income"
    assert object_type_for_classified_role("app_profile") == "app_profile"
    assert object_type_for_classified_role("vehicle_exterior") == "vehicle_exterior_or_interior"
    legacy = resolve_slot_object({"role": "app_income", "title": "Thu nhập tuần"})
    assert legacy.id == "weekly_income"
    unknown = resolve_slot_object({"role": "custom_object", "title": "Ảnh bất kỳ"})
    assert unknown.id.startswith("legacy_")


def test_custom_prompt_wins_over_legacy_role_and_title():
    slot = {
        "role": "app_income",
        "title": "Tuần thu nhập tháng 10",
        "ai_detection_prompt": "CUSTOM PROMPT ONLY",
    }
    obj = resolve_slot_object(slot)
    assert obj.id == "custom_app_income"
    assert obj.prompt == "CUSTOM PROMPT ONLY"
    assert obj.expected_fields == []
    assert obj.engines == []
    assert build_detection_prompt(obj) == "CUSTOM PROMPT ONLY"
    result = run_definition_checks(obj, {"anything": 1}, {})
    assert result["passed"] is True
    assert result["engines"] == []


def test_legacy_role_with_empty_prompt_still_resolves_known_object():
    obj = resolve_slot_object({"role": "app_income", "title": "Thu nhập tuần", "ai_detection_prompt": ""})
    assert obj.id == "weekly_income"
    assert obj.engines


def test_custom_prompt_object_save_roundtrip():
    object_id = "pytest_custom_prompt_object"
    try:
        save_object(object_id, {
            "label": "Custom prompt",
            "description": "tmp",
            "prompt": "CUSTOM ONLY {{objects}}",
            "objects": ["thing"],
            "expected_fields": [],
            "customizable": [],
            "engines": [],
            "extractor": "generic",
        })
        loaded = get_object(object_id)
        assert loaded.prompt == "CUSTOM ONLY {{objects}}"
        assert build_detection_prompt(loaded) == "CUSTOM ONLY thing"
    finally:
        delete_object(object_id)



def test_default_form_and_migrated_form_have_object_types():
    migrated = json.load(open("data/forms/TB_GSM_2026_Nov_Test.json", encoding="utf-8"))
    schema = FormSchema.model_validate(migrated)
    types = [
        slot.get("object_type")
        for question in schema.questions
        for slot in (question.image_slots or [])
    ]
    assert "app_profile" in types
    assert "vehicle_exterior_or_interior" in types
    assert types.count("weekly_income") == 5
    slot_by_type = {}
    for question in schema.questions:
        for slot in (question.image_slots or []):
            if slot.get("object_type"):
                slot_by_type.setdefault(slot["object_type"], []).append(slot)
    assert all(slot.get("object_config") for slots in slot_by_type.values() for slot in slots)
    profile_config = slot_by_type["app_profile"][0]["object_config"]
    assert profile_config["require_authentic"] is True
    assert "allowed_apps" in profile_config
    vehicle_config = slot_by_type["vehicle_exterior_or_interior"][0]["object_config"]
    assert "require_plate" in vehicle_config
    assert "allowed_views" in vehicle_config
    income_slots = [
        slot for question in schema.questions for slot in (question.image_slots or [])
        if slot.get("object_type") == "weekly_income"
    ]
    assert all(slot["object_config"]["period_type"] == "WEEKLY" for slot in income_slots)
    assert all(
        slot["object_config"]["constraints"] == [
            {"field": "period_months", "op": "contains", "value": 10},
            {"field": "period_years", "op": "contains", "value": 2026},
        ]
        for slot in income_slots
    )
    weeks = ["28/09 - 04/10", "05/10 - 11/10", "12/10 - 18/10", "19/10 - 25/10", "26/10 - 01/11"]
    for slot, period in zip(income_slots, weeks):
        result = run_object_checks(
            "weekly_income",
            {
                "gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000,
                "period": period, "detected_app": "XANH_SM", "is_authentic_gsm_ui": True,
                "is_legible": True, "confidence_score": 0.95, "is_manipulated_or_photoshopped": False,
            },
            slot["object_config"],
        )
        assert result["passed"] is True, (period, result["reasons"])


# ==============================================================================
# Engines
# ==============================================================================

def test_conditions_gate_engine_execution():
    extraction = {"period_raw_text": "28/09 - 04/10"}
    assert conditions_pass([{"field": "period_raw_text", "op": "not_empty"}], extraction)
    assert not conditions_pass([{"field": "period_raw_text", "op": "empty"}], extraction)
    assert evaluate_condition({"field": "gross_revenue", "op": "gte", "value": 100}, {"gross_revenue": 200})
    assert not evaluate_condition({"field": "gross_revenue", "op": "lte", "value": 100}, {"gross_revenue": 200})
    assert evaluate_condition({"field": "$config.require_plate", "op": "equals", "value": True}, {}, {"require_plate": True})
    assert not evaluate_condition({"field": "$config.require_plate", "op": "equals", "value": True}, {}, {"require_plate": False})

    engine = {"id": "req", "implementation": "required_fields"}
    binding = {"id": "req", "params": {"fields": ["gross_revenue"]}, "conditions": [{"field": "period_raw_text", "op": "empty"}]}
    result = run_engine(engine, binding, extraction, {})
    assert result["skipped"] is True and result["passed"] is True


def test_vehicle_profile_object_engines_read_object_config():
    vehicle = get_object("vehicle_exterior_or_interior")
    complete = {"view": "EXTERIOR", "is_authentic": True}

    # A required view/authenticity label is enforced even without an extra
    # engine binding, so a partly hallucinated OCR response cannot pass.
    missing_view = run_definition_checks(vehicle, {"is_authentic": True}, {"require_plate": False})
    assert missing_view["passed"] is False
    assert "view" in missing_view["reasons"][0]

    missing_plate = run_definition_checks(vehicle, complete, {"require_plate": True})
    assert missing_plate["passed"] is False
    assert "license_plate" in missing_plate["reasons"][0]

    no_plate_required = run_definition_checks(vehicle, complete, {"require_plate": False})
    assert no_plate_required["passed"] is True

    bad_view = run_definition_checks(vehicle, {**complete, "view": "TRUNK"}, {"require_plate": False})
    assert bad_view["passed"] is False

    profile = get_object("app_profile")
    missing_tier = run_definition_checks(profile, {"app": "XANH_SM"}, {"require_member_tier": True})
    assert missing_tier["passed"] is False
    assert "membership_type" in missing_tier["reasons"][0]

    bad_app = run_definition_checks(profile, {"app": "GRAB"}, {"require_member_tier": False})
    assert bad_app["passed"] is False


def test_required_fields_engine():
    result = run_object_checks("weekly_income", {"period": "Tuần 1"}, _weekly_config())
    assert result["passed"] is False
    assert "Thiếu trường dữ liệu bắt buộc" in result["reasons"][0]

    result = run_object_checks("weekly_income", {"gross_revenue": 1000, "period": "Tuần 1"}, _weekly_config())
    assert result["passed"] is True


def test_ocr_localized_keys_are_normalized_to_canonical():
    localized = {
        "Doanh thu gộp": 1602000,
        "Thu nhập ròng": 393928,
        "Chiết khấu và thuế": -1208072,
        "Tổng số chuyến": 22,
        "Số giờ online": "27h 15m",
        "Khoảng thời gian": "24 - 25/10/2026",
        "Ứng dụng phát hiện": "XANH_SM",
        "Giao diện GSM chính thức": True,
        "Dấu hiệu chỉnh sửa": False,
        "Ảnh đọc được": True,
        "Độ tin cậy": 0.95,
    }
    normalized = normalize_ocr_field_aliases(localized)
    assert normalized["gross_revenue"] == 1602000
    assert normalized["period_raw_text"] == "24 - 25/10/2026"
    assert normalized["detected_app"] == "XANH_SM"

    config = {
        "period_type": "WEEKLY",
        "constraints": [
            {"field": "period_months", "op": "contains", "value": 10},
            {"field": "period_years", "op": "contains", "value": 2026},
        ],
    }
    result = run_object_checks("weekly_income", localized, config)
    assert result["passed"] is True, result["reasons"]
    assert result["extraction"]["gross_revenue"] == 1602000


def test_ocr_english_aliases_and_currency_formatting_are_supported():
    payload = {
        "gross": "1.602.000đ",
        "profit": "393.928đ",
        "discount_and_tax": "-1.208.072",
        "trips": 22,
        "period_text": "24 - 25/10/2026",
        "detected_app": "XANH_SM",
        "is_authentic_gsm_ui": True,
        "is_manipulated_or_photoshopped": False,
        "is_legible": True,
        "confidence_score": 0.95,
    }
    result = run_object_checks("weekly_income", payload, _weekly_config())
    assert result["passed"] is True, result["reasons"]
    assert result["extraction"]["gross_revenue"] == 1602000
    assert result["extraction"]["net_income"] == 393928


def test_canonical_values_win_over_aliases():
    normalized = normalize_ocr_field_aliases({
        "gross_revenue": 100,
        "Doanh thu gộp": 200,
    })
    assert normalized["gross_revenue"] == 100


def test_day_only_period_uses_detail_date_month_context():
    base = {
        "Doanh thu gộp": 5160500,
        "Thu nhập ròng": 1210380,
        "Chiết khấu và thuế": -3950120,
        "Tổng số chuyến": 61,
        "Số giờ online": "82h 1m",
        "Khoảng thời gian": "26 - 02",
        "Ngày chi tiết đầu tiên": "Thứ bảy, 26/09/2026",
        "Ứng dụng phát hiện": "XANH_SM",
        "Giao diện GSM chính thức": True,
        "Dấu hiệu chỉnh sửa": False,
        "Ảnh đọc được": True,
        "Độ tin cậy": 0.95,
    }
    config = {
        "period_type": "WEEKLY",
        "constraints": [
            {"field": "period_months", "op": "contains", "value": 10},
            {"field": "period_years", "op": "contains", "value": 2026},
        ],
    }
    result = run_object_checks("weekly_income", base, config)
    assert result["passed"] is True, result["reasons"]


def test_week_label_uses_detail_date_month_context():
    base = {
        "Doanh thu gộp": 5160500,
        "Thu nhập ròng": 1210380,
        "Chiết khấu và thuế": -3950120,
        "Tổng số chuyến": 61,
        "Khoảng thời gian": "Tuần 1",
        "Ngày chi tiết đầu tiên": "Thứ bảy, 26/09/2026",
        "Ứng dụng phát hiện": "XANH_SM",
        "Giao diện GSM chính thức": True,
        "Dấu hiệu chỉnh sửa": False,
        "Ảnh đọc được": True,
        "Độ tin cậy": 0.95,
    }
    result = run_object_checks("weekly_income", base, {
        "period_type": "WEEKLY",
        "constraints": [
            {"field": "period_months", "op": "contains", "value": 10},
            {"field": "period_years", "op": "contains", "value": 2026},
        ],
    })
    assert result["passed"] is True, result["reasons"]


def test_month_only_period_satisfies_month_constraint():
    base = {
        "Doanh thu gộp": 6014500,
        "Thu nhập ròng": 1410830,
        "Chiết khấu và thuế": -4603670,
        "Tổng số chuyến": 60,
        "Khoảng thời gian": "10/2026",
        "Ngày chi tiết đầu tiên": "Thứ bảy, 17/10/2026",
        "Ứng dụng phát hiện": "XANH_SM",
        "Giao diện GSM chính thức": True,
        "Dấu hiệu chỉnh sửa": False,
        "Ảnh đọc được": True,
        "Độ tin cậy": 0.95,
    }
    result = run_object_checks("weekly_income", base, {
        "period_type": "WEEKLY",
        "constraints": [
            {"field": "period_months", "op": "contains", "value": 10},
            {"field": "period_years", "op": "contains", "value": 2026},
        ],
    })
    assert result["passed"] is True, result["reasons"]


@pytest.mark.parametrize(
    "period,passed",
    [
        ("Tháng 10/2026", False),
        ("15/10/2026", False),
        ("15/10 - 21/10", True),
        ("28/09/2026 - 04/10/2026", True),
        ("15/10 - 28/10", True),
        ("15/10 - 29/10", False),
        ("Tuần 1", True),
    ],
)
def test_period_granularity_engine(period, passed):
    result = run_object_checks(
        "weekly_income",
        {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "period": period, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95},
        _weekly_config(),
    )
    assert result["passed"] is passed, result["reasons"]


@pytest.mark.parametrize(
    "period_type,period,passed",
    [
        ("DAILY", "15/10/2026", True),
        ("DAILY", "15/10 - 16/10", False),
        ("WEEKLY", "28/09 - 04/10", True),
        ("WEEKLY", "15/10 - 28/10", True),
        ("WEEKLY", "15/10 - 29/10", False),
        ("WEEKLY", "15/10/2026", False),
        ("MONTHLY", "01/10/2026 - 31/10/2026", True),
        ("MONTHLY", "Tháng 10/2026", True),
        ("MONTHLY", "05/10/2026 - 31/10/2026", True),
        ("QUARTERLY", "01/01/2026 - 31/03/2026", True),
        ("MONTHLY", "01/01/2026 - 31/03/2026", False),
        ("QUARTERLY", "01/01/2026 - 15/04/2026", False),
        ("YEARLY", "01/01/2026 - 31/12/2026", True),
        ("YEARLY", "01/01/2026 - 31/10/2026", False),
    ],
)
def test_period_type_duration_windows(period_type, period, passed):
    result = run_object_checks(
        "weekly_income",
        {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "period": period, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95},
        _weekly_config(period_type=period_type),
    )
    assert result["passed"] is passed, result["reasons"]


def test_date_range_engine_cross_month_target_window():
    engine = {"id": "dr", "implementation": "date_range"}
    binding = {"id": "dr", "params": {"field": "period_raw_text", "target_month": "2026-10", "granularity": "WEEKLY"}}

    cross_start = run_engine(engine, binding, {"period_raw_text": "28/09 - 04/10"}, {})
    assert cross_start["passed"] is True, cross_start["reason"]

    cross_end = run_engine(engine, binding, {"period_raw_text": "26/10 - 01/11"}, {})
    assert cross_end["passed"] is True, cross_end["reason"]

    wrong_month = run_engine(engine, binding, {"period_raw_text": "05/09 - 11/09"}, {})
    assert wrong_month["passed"] is False
    assert "không khớp tháng mục tiêu" in wrong_month["reason"]


def test_constraints_engine_period_facts():
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95}
    cross_month = {**base, "period": "28/09 - 04/10"}

    passed = run_object_checks("weekly_income", cross_month, _weekly_config(constraints=[
        {"field": "period_months", "op": "contains", "value": 10},
        {"field": "period_years", "op": "contains", "value": 2026},
    ]))
    assert passed["passed"] is True, passed["reasons"]

    month_fail = run_object_checks("weekly_income", cross_month, _weekly_config(constraints=[
        {"field": "period_months", "op": "contains", "value": 8},
    ]))
    assert month_fail["passed"] is False
    assert "Ràng buộc không đạt" in month_fail["reasons"][0]

    duration_fail = run_object_checks("weekly_income", cross_month, _weekly_config(constraints=[
        {"field": "duration_days", "op": "between", "value": "8,14"},
    ]))
    assert duration_fail["passed"] is False

    duration_pass = run_object_checks("weekly_income", cross_month, _weekly_config(constraints=[
        {"field": "duration_days", "op": "between", "value": "7,7"},
    ]))
    assert duration_pass["passed"] is True, duration_pass["reasons"]

    in_pass = run_object_checks("weekly_income", cross_month, _weekly_config(constraints=[
        {"field": "period_type", "op": "in", "value": "WEEKLY,MONTHLY"},
        {"field": "period_start", "op": "equals", "value": "2026-09-28"},
        {"field": "period_end", "op": "equals", "value": "2026-10-04"},
    ]))
    assert in_pass["passed"] is True, in_pass["reasons"]


def test_constraints_engine_accepts_comma_separated_months():
    """The Nov campaign stores '9,10' so September and October weeks both pass."""
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95}
    september = {**base, "period": "01/09/2026 - 07/09/2026"}
    october = {**base, "period": "01/10/2026 - 07/10/2026"}
    november = {**base, "period": "01/11/2026 - 07/11/2026"}
    both_months = [
        {"field": "period_months", "op": "contains", "value": "9,10"},
        {"field": "period_years", "op": "contains", "value": 2026},
    ]

    for extraction in (september, october):
        result = run_object_checks("weekly_income", extraction, _weekly_config(constraints=both_months))
        assert result["passed"] is True, result["reasons"]

    outside = run_object_checks("weekly_income", november, _weekly_config(constraints=both_months))
    assert outside["passed"] is False
    assert "Ràng buộc không đạt" in outside["reasons"][0]

    # A single-month equality must match a one-element month list, not its repr.
    september_only = run_object_checks("weekly_income", september, _weekly_config(constraints=[
        {"field": "period_months", "op": "equals", "value": "9"},
        {"field": "period_years", "op": "equals", "value": "2026"},
    ]))
    assert september_only["passed"] is True, september_only["reasons"]

    october_against_september = run_object_checks("weekly_income", october, _weekly_config(constraints=[
        {"field": "period_months", "op": "equals", "value": "9"},
    ]))
    assert october_against_september["passed"] is False


def test_constraints_engine_between_uses_two_bounds_on_list_facts():
    """A week that crosses the month boundary still belongs to the campaign month."""
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95}
    september = {**base, "period": "01/09/2026 - 07/09/2026"}
    october = {**base, "period": "01/10/2026 - 07/10/2026"}
    november = {**base, "period": "01/11/2026 - 07/11/2026"}
    cross_month = {**base, "period": "28/09/2026 - 04/10/2026"}
    august_boundary = {**base, "period": "31/08/2026 - 06/09/2026"}

    bounds = [{"field": "period_months", "op": "between", "value_min": 9, "value_max": 10}]
    for extraction in (september, october, cross_month, august_boundary):
        result = run_object_checks("weekly_income", extraction, _weekly_config(constraints=bounds))
        assert result["passed"] is True, (extraction["period"], result["reasons"])

    outside = run_object_checks("weekly_income", november, _weekly_config(constraints=bounds))
    assert outside["passed"] is False
    assert "between 9-10" in outside["reasons"][0]

    # Legacy comma form of the same constraint keeps working.
    legacy = run_object_checks("weekly_income", september, _weekly_config(constraints=[
        {"field": "period_months", "op": "between", "value": "9,10"},
    ]))
    assert legacy["passed"] is True, legacy["reasons"]

    # Overlap means one matching month is enough; a fully outside window still fails.
    overlapping = run_object_checks("weekly_income", cross_month, _weekly_config(constraints=[
        {"field": "period_months", "op": "between", "value_min": 10, "value_max": 11},
    ]))
    assert overlapping["passed"] is True, overlapping["reasons"]

    disjoint = run_object_checks("weekly_income", cross_month, _weekly_config(constraints=[
        {"field": "period_months", "op": "between", "value_min": 11, "value_max": 12},
    ]))
    assert disjoint["passed"] is False

    missing_bound = run_object_checks("weekly_income", september, _weekly_config(constraints=[
        {"field": "period_months", "op": "between", "value_min": 9},
    ]))
    assert missing_bound["passed"] is False
    assert "nhỏ nhất và lớn nhất" in missing_bound["reasons"][0]


def test_constraints_engine_numeric_ops_accept_list_facts():
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95}
    october = {**base, "period": "01/10/2026 - 07/10/2026"}

    assert run_object_checks("weekly_income", october, _weekly_config(constraints=[
        {"field": "period_months", "op": "gte", "value": 10},
    ]))["passed"] is True
    assert run_object_checks("weekly_income", october, _weekly_config(constraints=[
        {"field": "period_months", "op": "lte", "value": 9},
    ]))["passed"] is False
    assert run_object_checks("weekly_income", october, _weekly_config(constraints=[
        {"field": "period_months", "op": "in", "value": "9,10"},
    ]))["passed"] is True


def test_split_list_values_supports_quotes_and_escapes():
    from services.image_check_engines import split_list_values

    assert split_list_values("9,10") == ["9", "10"]
    assert split_list_values(" 9 , 10 ") == ["9", "10"]
    assert split_list_values('"a,b",c') == ["a,b", "c"]
    assert split_list_values('"say \\"hi\\"",x') == ['say "hi"', "x"]
    assert split_list_values('"back\\\\slash"') == ["back\\slash"]
    assert split_list_values("plain\\path") == ["plain\\path"]
    assert split_list_values("trailing\\") == ["trailing\\"]
    assert split_list_values("") == []
    assert split_list_values(None) == []


def test_quoted_constraint_value_keeps_the_comma_inside_one_token():
    from services.image_check_engines import engine_enum

    # 'A,B' is one allowed value, not two.
    result = engine_enum({"field": "detected_app", "allowed": '"A,B",XANH_SM'}, {"detected_app": "a,b"}, {})
    assert result["passed"] is True
    rejected = engine_enum({"field": "detected_app", "allowed": '"A,B",XANH_SM'}, {"detected_app": "A"}, {})
    assert rejected["passed"] is False


def test_constraints_engine_rejects_empty_value_and_unknown_field():
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "period": "28/09 - 04/10"}
    empty = run_object_checks("weekly_income", base, _weekly_config(constraints=[
        {"field": "period_months", "op": "contains", "value": ""},
    ]))
    assert empty["passed"] is False
    assert "thiếu giá trị" in empty["reasons"][0]

    unknown = run_object_checks("weekly_income", base, _weekly_config(constraints=[
        {"field": "gross_revenue", "op": "gte", "value": "1"},
    ]))
    assert unknown["passed"] is False
    assert "không được hỗ trợ" in unknown["reasons"][0]


def test_number_range_engine():
    engine = {"id": "nr", "implementation": "number_range"}
    binding = {"id": "nr", "params": {"field": "confidence_score", "min": 0.6}}
    assert run_engine(engine, binding, {"confidence_score": 0.9}, {})["passed"] is True
    failed = run_engine(engine, binding, {"confidence_score": 0.2}, {})
    assert failed["passed"] is False and "tối thiểu" in failed["reason"]


def test_arithmetic_engine_is_safe_ast_not_eval():
    value, error = safe_arithmetic("(gross - abs(discount) - net) / max(gross, 1)", {"gross": 10, "discount": -2, "net": 8})
    assert error is None and value == 0.0

    value, error = safe_arithmetic("__import__('os').system('echo pwn')", {})
    assert value is None and "không được phép" in error

    engine = {"id": "arith", "implementation": "arithmetic"}
    binding = {"id": "arith", "params": {"expression": "net - gross", "tolerance": 0, "direction": "lte", "message": "Số học không khớp"}}
    assert run_engine(engine, binding, {"net": 8, "gross": 10}, {})["passed"] is True
    assert run_engine(engine, binding, {"net": 12, "gross": 10}, {})["passed"] is False


def test_regex_and_enum_engines():
    regex_engine = {"id": "rx", "implementation": "regex"}
    assert run_engine(regex_engine, {"id": "rx", "params": {"field": "plate", "pattern": r"^\d{2}[A-Z]"}}, {"plate": "29E-123"}, {})["passed"] is True
    assert run_engine(regex_engine, {"id": "rx", "params": {"field": "plate", "pattern": r"^\d{2}[A-Z]"}}, {"plate": "bad"}, {})["passed"] is False

    enum_engine = {"id": "en", "implementation": "enum"}
    assert run_engine(enum_engine, {"id": "en", "params": {"field": "app", "allowed": ["XANH_SM"]}}, {"app": "XANH_SM"}, {})["passed"] is True
    assert run_engine(enum_engine, {"id": "en", "params": {"field": "app", "allowed": ["XANH_SM"]}}, {"app": "GRAB"}, {})["passed"] is False


def test_unknown_engine_implementation_fails_loudly():
    with pytest.raises(EngineDefinitionError):
        run_engine({"id": "bad", "implementation": "arbitrary_python"}, {"id": "bad"}, {}, {})


# ==============================================================================
# Weekly-income preserved behavior
# ==============================================================================

def test_weekly_income_daily_monthly_single_day_and_valid_week():
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95}

    monthly = run_object_checks("weekly_income", {**base, "period": "Tháng 10/2026"}, _weekly_config())
    assert monthly["passed"] is False and "THÁNG" in monthly["reasons"][0] and "WEEKLY" in monthly["reasons"][0]

    daily_tab = run_object_checks("weekly_income", {**base, "period": "Hôm nay (15/10/2026)", "active_tab": "DAILY"}, _weekly_config())
    assert daily_tab["passed"] is False and "NGÀY" in daily_tab["reasons"][0]

    single_day = run_object_checks("weekly_income", {**base, "period": "15/10/2026 - 15/10/2026"}, _weekly_config())
    assert single_day["passed"] is False and "1 ngày" in single_day["reasons"][0]

    valid = run_object_checks("weekly_income", {**base, "period": "28/09/2026 - 04/10/2026"}, _weekly_config())
    assert valid["passed"] is True, valid["reasons"]


def test_weekly_income_config_changes_period_type_and_constraints():
    base = {"gross_revenue": 5_000_000, "net_income": 4_000_000, "discount_and_tax": 1_000_000, "detected_app": "XANH_SM", "is_legible": True, "confidence_score": 0.95, "period": "05/10 - 11/10"}
    assert run_object_checks("weekly_income", base, _weekly_config())["passed"] is True

    monthly = run_object_checks("weekly_income", {**base, "period": "01/10/2026 - 31/10/2026"}, _weekly_config(period_type="MONTHLY"))
    assert monthly["passed"] is True, monthly["reasons"]

    wrong_month = run_object_checks("weekly_income", base, _weekly_config(
        constraints=[{"field": "period_months", "op": "contains", "value": 9}],
    ))
    assert wrong_month["passed"] is False
    assert "Ràng buộc không đạt" in wrong_month["reasons"][0]


def test_projection_adapter_is_not_fixed_to_five_weeks():
    results = [
        {"object_type": "weekly_income", "extraction": {"gross_revenue": 1_000_000 * n, "net_income": 800_000 * n, "total_trips": 10 * n, "period": f"0{n}/10 - 0{n}/10"}}
        for n in range(1, 8)
    ]
    weeks = project_income_weeks(results)
    assert len(weeks) == 7
    assert weeks["week_7"]["gross_revenue"] == 7_000_000
    totals = project_income_totals(weeks)
    assert totals["total_gross_revenue"] == sum(1_000_000 * n for n in range(1, 8))
    assert totals["total_trips"] == sum(10 * n for n in range(1, 8))


def test_projection_adapter_accepts_any_income_bearing_object():
    weeks = project_income_weeks([
        {"object_type": "custom_profit_screen", "extraction": {"gross_revenue": 500_000, "net_income": 400_000}},
        {"object_type": "app_profile", "extraction": {"membership_type": "Vàng"}},
    ])
    assert list(weeks) == ["week_1"]
    assert weeks["week_1"]["gross_revenue"] == 500_000


# ==============================================================================
# Admin page + API
# ==============================================================================

def test_admin_api_returns_on_disk_definitions():
    from core.state import verify_admin_auth
    app.dependency_overrides[verify_admin_auth] = lambda: "admin"
    try:
        client = TestClient(app)
        response = client.get("/api/v1/image-objects", auth=("admin", "12345678"))
    finally:
        app.dependency_overrides.pop(verify_admin_auth, None)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"objects", "engines"}
    object_ids = {item["id"] for item in body["objects"]}
    assert {"weekly_income", "app_profile", "vehicle_exterior_or_interior"} <= object_ids
    assert any(engine["implementation"] == "period_granularity" for engine in body["engines"])
    assert any(engine["implementation"] == "constraints" for engine in body["engines"])


def test_admin_page_lists_objects_and_engines():
    from core.state import verify_admin_auth
    app.dependency_overrides[verify_admin_auth] = lambda: "admin"
    try:
        client = TestClient(app)
        response = client.get("/admin/image-objects", auth=("admin", "12345678"))
        forms_page = client.get("/admin/forms", auth=("admin", "12345678"))
    finally:
        app.dependency_overrides.pop(verify_admin_auth, None)
    assert response.status_code == 200
    assert "weekly_income" in response.text
    assert "period_granularity" in response.text
    assert "constraints" in response.text
    assert forms_page.status_code == 200
    assert '/static/ui/admin_nav.js' in forms_page.text


def test_admin_save_and_delete_object_roundtrip():
    object_id = "pytest_image_object"
    try:
        saved = save_object(object_id, {"label": "Pytest object", "description": "tmp"})
        assert saved.id == object_id
        assert os.path.exists(os.path.join("data", "image_objects", f"{object_id}.json"))
        assert get_object(object_id).label == "Pytest object"
    finally:
        delete_object(object_id)
    assert object_id not in {obj.id for obj in list_objects()}


# ==============================================================================
# Runtime: object_type form and legacy form
# ==============================================================================

def _jpeg_bytes(color=(30, 160, 180)):
    buffer = io.BytesIO()
    Image.new("RGB", (200, 300), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _probe_form(form_id: str, slots):
    FORM_REGISTRY[form_id] = FormSchema(
        form_id=form_id,
        title="Image Object Probe",
        settings=FormSettings(
            decree13_required=False,
            is_test_mode=True,
            require_manual_review=False,
            image_checks=ImageCheckConfig(enable_blur_check=False, enable_ai_vision=True, enable_anti_fraud=False),
        ),
        questions=[FormQuestion(
            id="q_cam",
            title="Anh",
            type=QuestionType.CAMERA_WEBRTC,
            required=True,
            image_slots=slots,
            min_images=len(slots),
            max_images=len(slots),
            exact_image_count=len(slots),
        )],
    )


def test_runtime_accepts_legacy_form_without_object_type(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-LEGACY-IMAGE-OBJECTS"
    _probe_form(form_id, [
        {"slot": 0, "title": "Ho so", "role": "app_profile", "required": True},
        {"slot": 1, "title": "Thu nhap", "role": "app_income", "required": True},
    ])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true", "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"},
            files=[
                ("survey_photo_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("income.jpg", io.BytesIO(_jpeg_bytes((220, 40, 40))), "image/jpeg")),
            ],
        )
        body = response.json()
        assert "Thiếu ảnh" not in (body.get("reason") or "")
        assert "Cấu hình kiểm tra ảnh không hợp lệ" not in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)


def test_runtime_custom_prompt_mode_skips_hardcoded_income_checks(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-CUSTOM-PROMPT-RUNTIME"
    _probe_form(form_id, [
        {"slot": 0, "title": "Ho so tuy chinh", "role": "app_profile", "ai_detection_prompt": "CUSTOM PROFILE PROMPT", "required": True},
        {"slot": 1, "title": "Thu nhap tuy chinh", "role": "app_income", "ai_detection_prompt": "CUSTOM INCOME PROMPT", "required": True},
    ])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true", "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"},
            files=[
                ("survey_photo_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("income.jpg", io.BytesIO(_jpeg_bytes((220, 40, 40))), "image/jpeg")),
            ],
        )
        body = response.json()
        assert body.get("status") == "APPROVED", body
        assert "Số học" not in (body.get("reason") or "")
        assert "báo cáo thu nhập" not in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)


def test_runtime_accepts_object_type_form(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-OBJECT-TYPE-RUNTIME"
    _probe_form(form_id, [
        {"slot": 0, "title": "Ho so", "role": "app_profile", "object_type": "app_profile", "object_config": {}, "required": True},
        {"slot": 1, "title": "Thu nhap", "role": "app_income", "object_type": "weekly_income", "object_config": _weekly_config(), "required": True},
    ])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true", "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"},
            files=[
                ("survey_photo_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("income.jpg", io.BytesIO(_jpeg_bytes((220, 40, 40))), "image/jpeg")),
            ],
        )
        body = response.json()
        assert "Thiếu ảnh" not in (body.get("reason") or "")
        assert "Cấu hình kiểm tra ảnh không hợp lệ" not in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)


def _probe_multi_camera_form(form_id: str):
    FORM_REGISTRY[form_id] = FormSchema(
        form_id=form_id,
        title="Multi Camera Probe",
        settings=FormSettings(
            decree13_required=False,
            is_test_mode=True,
            require_manual_review=False,
            image_checks=ImageCheckConfig(enable_blur_check=False, enable_ai_vision=True, enable_anti_fraud=False),
        ),
        questions=[
            FormQuestion(
                id="q_identity", title="Profile + vehicle", type=QuestionType.CAMERA_WEBRTC, required=True,
                image_slots=[
                    {"slot": 0, "title": "Ho so", "role": "app_profile", "object_type": "app_profile", "object_config": {}, "required": True},
                    {"slot": 1, "title": "Phuong tien", "role": "vehicle_exterior", "object_type": "vehicle_exterior_or_interior", "object_config": {}, "required": True},
                ],
                min_images=2, max_images=2, exact_image_count=2,
            ),
            FormQuestion(
                id="q_income", title="Income x5", type=QuestionType.CAMERA_WEBRTC, required=True,
                image_slots=[
                    {"slot": i, "title": f"Tuan {i + 1}", "role": f"app_income_week_{i + 1}", "object_type": "weekly_income", "object_config": _weekly_config(), "required": True, "require_unique": True}
                    for i in range(5)
                ],
                min_images=5, max_images=5, exact_image_count=5,
            ),
            FormQuestion(
                id="q_phone", title="Phone only", type=QuestionType.CAMERA_WEBRTC, required=False,
                image_slots=[
                    {"slot": 0, "title": "Dien thoai", "role": "custom_object", "required": False},
                ],
                min_images=1, max_images=1, exact_image_count=1,
            ),
        ],
    )


def test_multi_camera_questions_process_independently(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    # OCR is stubbed: this test proves per-question routing, and a real vehicle
    # object now requires readable evidence instead of passing on no data.
    monkeypatch.setattr(
        "core.state.vision_client.extract_object_fields",
        AsyncMock(return_value={"view": "EXTERIOR", "is_authentic": True}),
    )
    form_id = "SIM-MULTI-CAMERA-QUESTIONS"
    _probe_multi_camera_form(form_id)
    captured = {}
    try:
        client = TestClient(app)
        files = [
            ("q_identity__file_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg")),
            ("q_identity__file_1", ("vehicle.jpg", io.BytesIO(_jpeg_bytes((90, 90, 90))), "image/jpeg")),
            ("q_income__file_0", ("income1.jpg", io.BytesIO(_jpeg_bytes((210, 20, 20))), "image/jpeg")),
            ("q_income__file_1", ("income2.jpg", io.BytesIO(_jpeg_bytes((20, 210, 20))), "image/jpeg")),
            ("q_income__file_2", ("income3.jpg", io.BytesIO(_jpeg_bytes((20, 20, 210))), "image/jpeg")),
            ("q_income__file_3", ("income4.jpg", io.BytesIO(_jpeg_bytes((210, 210, 20))), "image/jpeg")),
            ("q_income__file_4", ("income5.jpg", io.BytesIO(_jpeg_bytes((20, 210, 210))), "image/jpeg")),
            ("q_phone__file_0", ("phone.jpg", io.BytesIO(_jpeg_bytes((150, 100, 50))), "image/jpeg")),
        ]
        with patch("routers.survey.append_dev_submission", side_effect=lambda record: captured.update(record)):
            response = client.post(
                "/api/v1/survey/submit-webrtc",
                data={"form_id": form_id, "is_test_mode": "true"},
                files=files,
            )
        body = response.json()
        assert body.get("status") == "APPROVED", body
        assert "0/5" not in (body.get("reason") or "")
        results = captured["ai_result"]["image_object_results"]
        assert {result["question_id"] for result in results} >= {"q_identity", "q_income"}
        assert sum(1 for result in results if result["question_id"] == "q_income" and result["object_type"] == "weekly_income") == 5
        assert all(result["passed"] for result in results), results
    finally:
        FORM_REGISTRY.pop(form_id, None)


def test_upload_key_unknown_question_is_rejected_clearly(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-UNKNOWN-UPLOAD-QUESTION"
    _probe_form(form_id, [{"slot": 0, "title": "Ho so", "role": "app_profile", "required": True}])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true"},
            files=[("missing_q__file_0", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg"))],
        )
        body = response.json()
        assert body.get("status") == "REJECTED"
        assert "không tồn tại" in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)


def test_upload_key_out_of_range_slot_is_rejected_clearly(monkeypatch):
    monkeypatch.setenv("MOCK_AI", "1")
    form_id = "SIM-BAD-UPLOAD-SLOT"
    _probe_form(form_id, [
        {"slot": 0, "title": "Ho so", "role": "app_profile", "required": True},
        {"slot": 1, "title": "Thu nhap", "role": "app_income", "required": True},
    ])
    try:
        client = TestClient(app)
        response = client.post(
            "/api/v1/survey/submit-webrtc",
            data={"form_id": form_id, "is_test_mode": "true"},
            files=[("q_cam__file_5", ("profile.jpg", io.BytesIO(_jpeg_bytes()), "image/jpeg"))],
        )
        body = response.json()
        assert body.get("status") == "REJECTED"
        assert "slot 5" in (body.get("reason") or "")
        assert "vượt quá" in (body.get("reason") or "")
    finally:
        FORM_REGISTRY.pop(form_id, None)
