"""Evaluate and commit a WebRTC submission without transport objects."""

import asyncio
import hashlib
import html
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from psycopg import errors as pg_errors

from core import embedding_executor
from core.durable_work import delivery_policy_snapshot, enqueue_submission_work, require_delivery_schema
from core.phone_links import normalize_vn_phone, phone_normalization_sql
from core.schema_gate import writer_schema_ready
from core.submission_attempts import attempt_scope, commit_response, request_fingerprint, reserve
from services.action_checklist import init_action_checklist
from services.duplicate_detector import (
    check_inter_submission_duplicates,
    check_intra_submission_duplicates,
    compute_dinov2_embedding,
    compute_image_dhash_str,
)
from services.evaluator import evaluate_survey_compliance
from services.form_engine import (
    FormSchema, _is_blank, effective_gates, gate_satisfied, get_effective_image_checks,
    validate_submission_against_schema,
)
from services.form_modules import run_form_modules
from services.image_object_projection import image_object_result, project_income_totals, project_income_weeks
from services.image_objects import (
    ImageObjectDefinitionError,
    build_detection_prompt,
    resolve_slot_object,
    run_definition_checks,
)
from core.admin_modules import refresh_image_object_registry
from services.image_types import parse_date_range_from_text
from services.telegram_ui import submission_form_line, with_survey_copy
from services.normalizer import compute_laplacian_blur_score, normalize_image_to_webp, sanitize_and_normalize_image
from services.online_and_ai_detector import compute_dhash_int, hamming_distance
from services.privacy_crypto import generate_driver_tag, hash_phone, hash_plate
from services.submission_media import build_manifest
from services.vehicle_tracker import VehiclePersistenceTracker

logger = logging.getLogger(__name__)
INTERRUPTED_CHECK_REASON = (
    "Hệ thống gián đoạn trong quá trình kiểm tra; ảnh đã nhận, cần kiểm tra lại."
)


@dataclass(frozen=True, slots=True)
class SubmissionUpload:
    question_id: str
    slot_index: int
    filename: str
    content: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionInput:
    """Authorized inputs from the adapter, with canonical upload order."""

    form_id: str
    schema: FormSchema | None = None
    delivery_schema: FormSchema | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    uploads: tuple[SubmissionUpload, ...] = ()
    upload_error: str | None = None
    question_id: str | None = None
    driver_phone: str | None = None
    driver_name: str | None = None
    license_plate: str | None = None
    collaborator_id: str | None = None
    vehicle_type: str | None = "CAR"
    gross_revenue: str | None = "0"
    trip_count: str | None = "0"
    signature_png: str | None = None
    is_test_mode: bool = False
    bypass_img_check: bool = False
    idempotency_key: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class SubmissionEvaluation:
    normalized_media: dict[int, bytes]
    ai_result: dict[str, Any]
    status: str
    reason: str
    is_approved: bool
    blur_score: float | None
    weekly_income: dict[str, Any]
    income_totals: dict[str, int]
    target_image: Any = field(repr=False)
    embeddings: dict[int, list[float] | None] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class SubmissionCommit:
    submission_id: str
    status: str
    response: dict[str, Any]
    outbox_ids: tuple[str, ...] = ()
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class LegacySubmissionWork:
    checklist: dict[str, Any]
    admin_text: str
    admin_markup: dict[str, Any]
    party_notification: dict[str, Any] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmissionDependencies:
    vision: Any
    media_root: str
    dev_storage_file: str
    append_dev_record: Callable[[dict[str, Any]], None]
    image_max_dimension: int
    blur_threshold: float
    db_pool: Any = None
    redis: Any = None
    legacy_cache: MutableMapping[str, str] | None = None
    production: bool = False
    durable_enabled: bool = False
    schedule_legacy: Callable[[LegacySubmissionWork], None] | None = None
    normalize_image: Callable[..., tuple[bytes, Any]] = sanitize_and_normalize_image


class SubmissionConflict(Exception):
    """This attempt conflicts with the existing payload or mode."""


class SubmissionUnavailable(Exception):
    """The writer cannot acknowledge this attempt yet."""


@dataclass(frozen=True, slots=True)
class _Attempt:
    scope: str
    key: str | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _Prepared:
    phone: str | None
    phone_hash: str | None
    driver_tag: str
    answers: dict[str, Any]
    form_data: dict[str, Any]
    target_question_id: str
    camera_questions: list[Any]
    upload_items: list[dict[str, Any]]
    raw_bytes: tuple[bytes, ...]
    attempt: _Attempt


def _looks_like_income_statement(result: Any) -> bool:
    """True when an OCR result actually looks like a revenue statement, not a menu/profile screen."""
    if not isinstance(result, dict):
        return False
    if result.get("layout_matched"):
        return True
    if result.get("period_tabs"):
        return True
    labels = [str(x).lower() for x in (result.get("field_labels") or [])]
    if any(token in label for label in labels for token in ("tổng thu nhập", "doanh số", "thu nhập ròng", "chiết khấu")):
        return True
    return any(int(result.get(key) or 0) for key in ("gross_revenue", "net_income", "total_trips"))


def _income_fingerprint(result: Any) -> Optional[Tuple[int, int, int]]:
    """Financial identity of a weekly screen, or None when OCR read nothing usable."""
    if not isinstance(result, dict):
        return None
    gross = int(result.get("gross_revenue") or 0)
    net = int(result.get("net_income") or 0)
    trips = int(result.get("total_trips") or 0)
    if gross <= 0 and net <= 0 and trips <= 0:
        return None
    return (gross, net, trips)


def _find_duplicate_income_period(income_pairs: List[Tuple[int, Any]]) -> Optional[dict]:
    """Return a violation when two uploaded screens report the same week period.

    Period text alone is not proof: the vision model periodically misreads which
    teal tab is active (e.g. week 17-23 read as 10-16), which would reject five
    genuinely distinct weeks. Two screens of the same physical week always show
    the same money, so a duplicate also requires matching financials.
    """
    seen: Dict[Any, Tuple[int, Any]] = {}
    for idx_pos, result in income_pairs:
        if not isinstance(result, dict):
            continue
        if result.get("period_reliable") is False:
            # The selected week tab could not be read; the weekly loop rejects it with
            # an actionable reason. Never turn an unreadable tab into a duplicate claim.
            continue
        label = str(result.get("period") or result.get("period_raw_text") or "").strip()
        if not label:
            continue
        start, end, _ = parse_date_range_from_text(label, default_year=2026)
        key = (start, end) if start and end else label.lower()
        fingerprint = _income_fingerprint(result)
        if key in seen:
            first_idx, first_result = seen[key]
            first_fingerprint = _income_fingerprint(first_result)
            if fingerprint is None or first_fingerprint is None or fingerprint != first_fingerprint:
                logger.info(
                    f"[DUPLICATE PERIOD] Ảnh {first_idx + 1} and Ảnh {idx_pos + 1} share the OCR period "
                    f"'{label}' but have different financials {first_fingerprint} vs {fingerprint}; not a duplicate."
                )
                continue
            return {
                "failed_slot": idx_pos,
                "ocr_data": result,
                "reason": (
                    f"Ảnh Tuần bị trùng kỳ báo cáo ({label}) ở Ảnh {first_idx + 1} và Ảnh {idx_pos + 1}. "
                    "Vui lòng chụp đủ các tuần khác nhau (thứ tự ảnh không quan trọng)."
                ),
            }
        seen[key] = (idx_pos, result)
    return None


def _unreliable_week_reason(
    ocr_data: Any, week_num: int, image_num: Optional[int] = None
) -> Optional[str]:
    """Actionable rejection text when the selected week tab could not be read reliably."""
    if not isinstance(ocr_data, dict) or ocr_data.get("period_reliable") is not False:
        return None
    detail = str(ocr_data.get("period_uncertain_reason") or "").strip()
    label = f"Ảnh Tuần {week_num}" if image_num is None else f"Ảnh Tuần {week_num} (Ảnh {image_num})"
    reason = (
        f"{label}: Không đọc được chính xác tuần đang chọn (tab tuần bị cắt/khuất). "
        "Vui lòng chụp lại màn hình hiển thị đủ các tab tuần."
    )
    if detail:
        reason += f" Chi tiết: {detail}."
    return reason


def _select_unique_check_index(target_q: Any, income_indices: List[int], image_count: int) -> Optional[int]:
    """
    Image index that must be globally unique. Profile/vehicle slots explicitly opt out
    (require_unique=False) because the same driver reuses them across submissions;
    the weekly income screens are the uniqueness-bearing objects.
    """
    # income_indices are GLOBAL image positions (all camera questions are flattened
    # into one list); target_q slot numbers are question-local and only line up with
    # the global list when this question's uploads start at index 0.
    if income_indices:
        first = income_indices[0]
        if 0 <= first < image_count:
            return first
    if target_q and getattr(target_q, "image_slots", None):
        for s_idx, slot in enumerate(target_q.image_slots):
            if slot.get("require_unique") is not False and s_idx < image_count:
                return s_idx
    return None


_VN_PHONE_RE = re.compile(r"(?:\+?84|0)(?:\d[ .-]?){8,10}\d")


def _infer_driver_phone(schema, answers: Dict[str, Any]) -> Optional[str]:
    """Find the driver phone in schema answers when the legacy field is absent."""
    if not isinstance(answers, dict):
        return None
    best = None
    for q in (getattr(schema, "questions", None) or []):
        value = answers.get(q.id)
        if not isinstance(value, str):
            continue
        match = _VN_PHONE_RE.search(value.strip())
        if not match:
            continue
        label = f"{q.id} {getattr(q, 'title', '')}".lower()
        score = 2 if any(t in label for t in ("phone", "điện thoại", "dien thoai", "sđt", "sdt", "zalo")) else 1
        candidate = (score, value.strip())
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best else None


def _infer_slot_role(slot: Any) -> str:
    """Resolve generic/custom slot roles from their titles (profile/income/vehicle)."""
    if not isinstance(slot, dict):
        return "unknown"
    role = str(slot.get("role") or "").strip().lower()
    title = str(slot.get("title") or "").lower()
    if role and role not in ("custom_object", "custom", "unknown", "slot", "other"):
        return role
    if "hồ sơ" in title or "profile" in title or "màn hình chính" in title:
        return "app_profile"
    if "thu nhập" in title or "income" in title or "tuần" in title:
        return "app_income"
    if "thân xe" in title or "đầu xe" in title or "vô lăng" in title or "phương tiện" in title:
        return "vehicle_exterior"
    return role or "unknown"


def _normalized_image_slots(slots: Any) -> List[dict]:
    normalized = []
    for slot in (slots or []):
        if not isinstance(slot, dict):
            continue
        slot_copy = {**slot, "role": _infer_slot_role(slot)}
        try:
            obj = resolve_slot_object(slot)
        except ImageObjectDefinitionError:
            if str(slot.get("object_type") or "").strip():
                raise
            obj = None
        if obj is not None and obj.classify_role:
            slot_copy["role"] = obj.classify_role
            slot_copy["object_type"] = obj.id
        normalized.append(slot_copy)
    return normalized


def _answer_by_title_keywords(schema: Any, answers: Dict[str, Any], keywords: tuple) -> Optional[str]:
    """First non-empty textual answer whose question title matches any keyword."""
    for question in (getattr(schema, "questions", None) or []):
        title = str(getattr(question, "title", "") or "").lower()
        if not any(keyword in title for keyword in keywords):
            continue
        value = answers.get(getattr(question, "id", ""))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None

def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return 0



def _normalize_vehicle_type(value: Optional[str]) -> str:
    """Vehicle type is only ever CAR or BIKE."""
    text = str(value or "").upper()
    if "BIKE" in text or "MÁY" in text or "XE MAY" in text:
        return "BIKE"
    return "CAR"


def _role_group(role: Any) -> str:
    value = str(role or "").lower()
    if "profile" in value or "odometer" in value:
        return "profile"
    if "vehicle" in value or "exterior" in value or "steering" in value:
        return "vehicle"
    if "income" in value or "revenue" in value or "earnings" in value:
        return "income"
    return "unknown"


def _question_classification_slots(question: Any) -> List[dict]:
    """Deduplicated (role,title) slot contract for one camera question."""
    return _classification_slots(None, question)


def _classification_slots(schema: Any, target_q: Any) -> List[dict]:
    """Deduplicated (role,title) slot contract covering every camera question."""
    slots = []
    seen = set()
    questions = [
        q for q in (getattr(schema, "questions", None) or [])
        if getattr(q, "type", None) == "camera_webrtc" and getattr(q, "image_slots", None)
    ] or ([target_q] if target_q and getattr(target_q, "image_slots", None) else [])
    for question in questions:
        for slot in _normalized_image_slots(question.image_slots):
            role = slot.get("role")
            if role in seen:
                continue
            seen.add(role)
            slots.append(slot)
    return slots


def _slot_config(slot: Any) -> Dict[str, Any]:
    if not isinstance(slot, dict):
        return {}
    config = slot.get("object_config")
    return dict(config) if isinstance(config, dict) else {}


def _slot_object(slot: Any):
    """Resolve a slot to its file-defined object (compat resolver for legacy slots)."""
    return resolve_slot_object(slot)


def _requires_evidence(obj: Any) -> bool:
    """An image object can only pass when OCR evidence exists for its rules."""
    return obj.extractor != "none" and bool(
        obj.expected_fields or obj.engines or obj.extractor in ("profile", "income_statement", "vehicle")
    )


async def _call_object_extractor(extractor_fn, image_bytes: bytes, week_idx: Optional[int], prompt: str) -> Dict[str, Any]:
    """Call a vision extractor, passing only arguments its real signature accepts."""
    kwargs: Dict[str, Any] = {}
    try:
        parameters = inspect.signature(extractor_fn).parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        if week_idx is not None and ("week_idx" in parameters or accepts_kwargs):
            kwargs["week_idx"] = week_idx
        if "prompt" in parameters or accepts_kwargs:
            kwargs["prompt"] = prompt
    except (TypeError, ValueError):
        pass
    try:
        return await extractor_fn(image_bytes, **kwargs)
    except TypeError as exc:
        if "prompt" in kwargs and "prompt" in str(exc):
            kwargs.pop("prompt")
            return await extractor_fn(image_bytes, **kwargs)
        raise


async def _extract_image_object(
    vision_client: Any,
    image_bytes: bytes,
    obj: Any,
    config: Optional[Dict[str, Any]] = None,
    week_idx: Optional[int] = None,
    prompt_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the object's built-in extraction profile with its generated prompt."""
    if obj.extractor == "none":
        return {}
    prompt = build_detection_prompt(obj, config or {})
    if (prompt_override or "").strip() and prompt_override.strip() not in prompt:
        prompt = f"{prompt}\nAdditional slot instructions: {prompt_override.strip()}"
    if obj.extractor == "income_statement":
        result = await _call_object_extractor(vision_client.ocr_driver_app_income, image_bytes, week_idx, prompt)
    elif obj.extractor == "profile":
        result = await _call_object_extractor(vision_client.ocr_driver_app_profile, image_bytes, None, prompt)
    else:
        result = await vision_client.extract_object_fields(image_bytes, prompt)
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    return result if isinstance(result, dict) else {}


def _slot_title(question: Any, slot_index: int) -> str:
    slots = getattr(question, "image_slots", None) or []
    if 0 <= slot_index < len(slots):
        return str(slots[slot_index].get("title") or slots[slot_index].get("role") or f"Ảnh {slot_index + 1}")
    return f"Ảnh {slot_index + 1}"


async def _audit_camera_question(
    vision_client: Any,
    question: Any,
    items: List[Dict[str, Any]],
    norm_bytes_cache: Dict[int, bytes],
) -> Dict[str, Any]:
    """Classify, extract and check one camera question with its own slots only."""
    question_id = getattr(question, "id", "__legacy__")
    result = {
        "question_id": question_id,
        "question": question,
        "image_object_results": [],
        "income_pairs": [],
        "income_required": 0,
        "profile_res": {},
        "profile_flat_index": None,
        "profile_object": None,
        "profile_config": {},
        "violation": None,
        "role_0": "unknown",
        "slot_0_is_profile": False,
    }

    local_norm = [norm_bytes_cache.get(item["flat_index"], b"") for item in items]
    slots = getattr(question, "image_slots", None) or []
    slot_objects = [_slot_object(slot) for slot in slots]
    direct_slots = {
        idx for idx, obj in enumerate(slot_objects)
        if obj.extractor not in ("profile", "income_statement", "none")
    }
    classification_slots = _question_classification_slots(question)
    classified_roles = await asyncio.gather(
        *[
            vision_client.classify_image_role(
                local_norm[idx],
                filename=item.get("filename", ""),
                expected_slots=classification_slots,
            )
            for idx, item in enumerate(items)
        ],
        return_exceptions=True,
    )
    classified_roles = [role if isinstance(role, str) else "unknown" for role in classified_roles]
    result["role_0"] = classified_roles[0] if classified_roles else "unknown"

    groups = {"profile": [], "income": [], "vehicle": []}
    for idx, role in enumerate(classified_roles):
        slot_idx = items[idx]["slot_index"]
        obj = slot_objects[slot_idx] if slot_idx < len(slot_objects) else None
        if obj is not None and obj.extractor == "vehicle":
            groups["vehicle"].append(idx)
            continue
        if obj is not None and obj.extractor not in ("profile", "income_statement"):
            continue
        group = _role_group(role)
        if group in groups:
            groups[group].append(idx)

    required_slots = [
        slot for idx, slot in enumerate(slots)
        if (idx not in direct_slots or slot_objects[idx].extractor == "vehicle") and slot.get("required", True)
    ]
    required_roles = [
        (_normalized_image_slots([slot])[0].get("role"), slot) for slot in required_slots
    ]
    required_counts = {"profile": 0, "vehicle": 0, "income": 0}
    for role, _slot in required_roles:
        group = _role_group(role)
        if group in required_counts:
            required_counts[group] += 1
    missing_titles = []
    for group, needed in required_counts.items():
        if len(groups[group]) < needed:
            for role, slot in required_roles:
                if _role_group(role) == group:
                    missing_titles.append(str(slot.get("title") or slot.get("role") or group))
                    break
    if missing_titles:
        result["violation"] = {
            "failed_slot": items[0]["flat_index"],
            "reason": (
                f"Câu hỏi '{question_id}': Thiếu ảnh đúng loại cho: " + "; ".join(missing_titles)
                + ". Vui lòng chụp đủ các mục yêu cầu."
            ),
            "ocr_data": {},
        }

    config_by_object: Dict[str, Dict[str, Any]] = {}
    prompt_by_object: Dict[str, str] = {}
    for slot, obj in zip(slots, slot_objects):
        config_by_object.setdefault(obj.id, _slot_config(slot))
        custom_prompt = str((slot or {}).get("ai_detection_prompt") or "").strip()
        if custom_prompt:
            prompt_by_object.setdefault(obj.id, custom_prompt)

    income_object = next((obj for obj in slot_objects if obj.extractor == "income_statement"), None)
    profile_object = next((obj for obj in slot_objects if obj.extractor == "profile"), None)
    income_config = config_by_object.get(income_object.id, {}) if income_object else {}
    profile_config = config_by_object.get(profile_object.id, {}) if profile_object else {}
    result["profile_object"] = profile_object
    result["profile_config"] = profile_config

    extraction_tasks = []
    extraction_kinds: List[str] = []
    extraction_positions: List[int] = []
    matched_profile_idx = groups["profile"][0] if groups["profile"] else None
    if matched_profile_idx is not None:
        result["role_0"] = classified_roles[matched_profile_idx]
    matched_income_indices = list(groups["income"])
    result["slot_0_is_profile"] = bool(
        slot_objects
        and slot_objects[0].extractor == "profile"
        and not slot_objects[0].id.startswith("custom_")
    )

    if profile_object is not None and matched_profile_idx is not None:
        extraction_tasks.append(
            _extract_image_object(
                vision_client,
                local_norm[matched_profile_idx],
                profile_object,
                profile_config,
                prompt_override=prompt_by_object.get(profile_object.id),
            )
        )
        extraction_kinds.append("profile")
        extraction_positions.append(matched_profile_idx)
    if income_object is not None:
        for i_pos, local_idx in enumerate(matched_income_indices):
            slot_idx = items[local_idx]["slot_index"]
            extraction_tasks.append(
                _extract_image_object(
                    vision_client,
                    local_norm[local_idx],
                    income_object,
                    _slot_config(slots[slot_idx]) if slot_idx < len(slots) else income_config,
                    week_idx=i_pos + 1,
                    prompt_override=prompt_by_object.get(income_object.id),
                )
            )
            extraction_kinds.append("income")
            extraction_positions.append(local_idx)
    custom_slots = {
        idx for idx in direct_slots
        if slot_objects[idx].extractor != "vehicle" and _requires_evidence(slot_objects[idx])
    }
    vehicle_slots = [idx for idx, obj in enumerate(slot_objects) if obj.extractor == "vehicle"]
    direct_positions = {
        local_idx: item["slot_index"] for local_idx, item in enumerate(items)
        if item["slot_index"] in custom_slots
    }
    taken = set(direct_positions)
    assigned = set(direct_positions.values())
    for slot_idx in vehicle_slots:
        if slot_idx in assigned:
            continue
        local_idx = next(
            (i for i, item in enumerate(items) if i not in taken and item["slot_index"] == slot_idx), None
        )
        if local_idx is None:
            local_idx = next((i for i in groups["vehicle"] if i not in taken), None)
        if local_idx is None:
            continue
        direct_positions[local_idx] = slot_idx
        taken.add(local_idx)
        assigned.add(slot_idx)
    for local_idx, slot_idx in direct_positions.items():
        extraction_tasks.append(_extract_image_object(
            vision_client, local_norm[local_idx], slot_objects[slot_idx],
            _slot_config(slots[slot_idx]),
            prompt_override=str(slots[slot_idx].get("ai_detection_prompt") or ""),
        ))
        extraction_kinds.append("object")
        extraction_positions.append(local_idx)

    all_results = await asyncio.gather(*extraction_tasks, return_exceptions=True) if extraction_tasks else []
    profile_res: Any = {}
    income_pairs: List[Any] = []
    direct_pairs = []
    for kind, local_idx, extracted in zip(extraction_kinds, extraction_positions, all_results):
        if isinstance(extracted, BaseException):
            raise extracted
        if kind == "profile":
            profile_res = extracted if isinstance(extracted, dict) else {}
            result["profile_res"] = profile_res
            result["profile_flat_index"] = items[local_idx]["flat_index"]
        elif kind == "object":
            direct_pairs.append((local_idx, extracted))
        else:
            income_pairs.append((local_idx, extracted))

    def _period_start(pair):
        extracted = pair[1]
        if isinstance(extracted, dict):
            start, _, _ = parse_date_range_from_text(str(extracted.get("period") or ""), default_year=2026)
            if start:
                return (0, start)
        return (1, items[pair[0]]["flat_index"])

    income_pairs.sort(key=_period_start)
    income_only = [pair for pair in income_pairs if _looks_like_income_statement(pair[1])]
    if income_only:
        income_pairs = income_only
    result["income_required"] = sum(obj.extractor == "income_statement" for obj in slot_objects)

    image_results: List[Dict[str, Any]] = []
    for local_idx, extracted in direct_pairs:
        item = items[local_idx]
        slot_idx = direct_positions[local_idx]
        obj = slot_objects[slot_idx]
        object_check = run_definition_checks(obj, extracted, _slot_config(slots[slot_idx]))
        if _requires_evidence(obj) and not any(not _is_blank(v) for v in object_check["extraction"].values()):
            reason = "Không trích xuất được thông tin từ ảnh để kiểm tra đối tượng yêu cầu."
            object_check["passed"] = False
            object_check["reasons"].append(reason)
            object_check["failures"].append({"engine_id": "object_extraction", "reason": reason, "details": {}})
        image_results.append(image_object_result(
            obj.id, slot_idx, _slot_title(question, slot_idx), extracted, object_check,
            question_id=question_id, flat_index=item["flat_index"],
        ))
        if result["violation"] is None and not object_check["passed"]:
            result["violation"] = {
                "failed_slot": item["flat_index"],
                "reason": f"Câu hỏi '{question_id}' ảnh {slot_idx + 1}: {object_check['reasons'][0]}",
                "ocr_data": extracted,
            }
    if profile_object is not None and matched_profile_idx is not None:
        slot_idx = items[matched_profile_idx]["slot_index"]
        try:
            profile_check = run_definition_checks(profile_object, profile_res, profile_config)
        except ImageObjectDefinitionError as exc:
            logger.error("[IMAGE OBJECTS] profile check unusable for %s: %s", question_id, exc)
            profile_check = {"passed": False, "reasons": [f"Cấu hình kiểm tra ảnh không hợp lệ: {exc}"], "failures": [], "engines": []}
        image_results.append(image_object_result(
            profile_object.id, slot_idx, _slot_title(question, slot_idx), profile_res, profile_check,
            question_id=question_id, flat_index=items[matched_profile_idx]["flat_index"],
        ))
        if result["violation"] is None and not profile_check.get("passed"):
            result["violation"] = {
                "failed_slot": items[matched_profile_idx]["flat_index"],
                "reason": f"Câu hỏi '{question_id}' ảnh {slot_idx + 1}: {profile_check['reasons'][0]}",
                "ocr_data": profile_res,
            }

    for w_i, (local_idx, extracted) in enumerate(income_pairs):
        if not isinstance(extracted, dict):
            continue
        slot_idx = items[local_idx]["slot_index"]
        slot_cfg = _slot_config(slots[slot_idx]) if slot_idx < len(slots) else income_config
        object_check = None
        if income_object is not None:
            try:
                object_check = run_definition_checks(income_object, extracted, slot_cfg)
            except ImageObjectDefinitionError as exc:
                logger.error("[IMAGE OBJECTS] income check unusable for %s: %s", question_id, exc)
                object_check = {"passed": False, "reasons": [f"Cấu hình kiểm tra ảnh không hợp lệ: {exc}"], "failures": [], "engines": []}
        image_results.append(image_object_result(
            income_object.id if income_object else "income", slot_idx, _slot_title(question, slot_idx), extracted, object_check,
            question_id=question_id, flat_index=items[local_idx]["flat_index"],
        ))
        if result["violation"] is not None:
            continue
        unreliable_reason = _unreliable_week_reason(extracted, w_i + 1, slot_idx + 1)
        if unreliable_reason:
            result["violation"] = {
                "failed_slot": items[local_idx]["flat_index"],
                "reason": f"Câu hỏi '{question_id}': {unreliable_reason}",
                "ocr_data": extracted,
            }
            continue
        if object_check is not None and not object_check.get("passed"):
            result["violation"] = {
                "failed_slot": items[local_idx]["flat_index"],
                "reason": f"Câu hỏi '{question_id}' ảnh {slot_idx + 1}: {object_check['reasons'][0]}",
                "ocr_data": extracted,
            }

    audited = {entry["flat_index"] for entry in image_results}
    missing_item = next((
        item for item in items
        if item["flat_index"] not in audited
        and item["slot_index"] < len(slot_objects)
        and _requires_evidence(slot_objects[item["slot_index"]])
    ), None)
    present_slots = {item["slot_index"] for item in items}
    missing_slot = next((
        idx for idx, slot in enumerate(slots)
        if slot.get("required", True) and idx not in present_slots and _requires_evidence(slot_objects[idx])
    ), None)
    if result["violation"] is None and (missing_item is not None or missing_slot is not None):
        slot_idx = missing_item["slot_index"] if missing_item is not None else missing_slot
        result["violation"] = {
            "failed_slot": missing_item["flat_index"] if missing_item is not None else (items[0]["flat_index"] if items else 0),
            "reason": f"Câu hỏi '{question_id}' ảnh {slot_idx + 1}: Thiếu ảnh hoặc chưa kiểm tra được đối tượng yêu cầu.",
            "ocr_data": {},
        }

    result["income_pairs"] = [(items[idx]["flat_index"], facts) for idx, facts in income_pairs]
    result["image_object_results"] = image_results
    return result


def _fsync_directory(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _store_submission_photos(
    sub_photo_dir: str,
    raw_bytes_list: Sequence[bytes],
    upload_items: Optional[List[dict]] = None,
) -> List[dict]:
    """Durably write every received slot once, preserving ingestion identity.

    A required write failure aborts the submission before any database success.
    Undecodable payloads are retained as .bin evidence, not acknowledged as
    images. The manifest is built from the ingestion descriptors, not from a
    sorted directory listing, so question/slot identity is stable.
    """
    try:
        os.makedirs(sub_photo_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create photo dir {sub_photo_dir}: {e}")
        raise
    files = []
    for index, raw in enumerate(raw_bytes_list):
        item = upload_items[index] if upload_items and index < len(upload_items) else {}
        flat_index = int(item.get("flat_index", index))
        target_base = os.path.join(sub_photo_dir, f"slot_{flat_index}")
        try:
            webp_bytes = normalize_image_to_webp(raw)
            if webp_bytes is None:
                target = target_base + ".bin"
                payload = raw
                logger.warning(f"Undecodable slot {flat_index} upload; stored raw bytes at {target}")
            else:
                target = target_base + ".webp"
                payload = webp_bytes
            staged_path = os.path.join(sub_photo_dir, "." + os.path.basename(target))
            with open(staged_path, "xb") as fp:
                fp.write(payload)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(staged_path, target)
        except OSError as e:
            logger.error(f"Required photo write failed for slot {flat_index}: {e}")
            raise
        except Exception as e:
            logger.error(f"Required photo normalization failed for slot {flat_index}: {e}")
            raise
        files.append({
            "name": os.path.basename(target),
            "question_id": item.get("question_id"),
            "slot_index": item.get("slot_index", index),
            "flat_index": flat_index,
        })
    _fsync_directory(sub_photo_dir)
    if not upload_items:
        # Compatibility for direct callers/tests that provide only bytes.
        files = []
        for name in sorted(os.listdir(sub_photo_dir)) if os.path.isdir(sub_photo_dir) else []:
            if os.path.isfile(os.path.join(sub_photo_dir, name)):
                files.append({"name": name})
    return build_manifest(os.path.basename(sub_photo_dir), files, base_dir=sub_photo_dir)


def _prepare(input: SubmissionInput) -> _Prepared:
    schema = input.schema
    form_data = dict(input.fields)
    answers = dict(form_data)
    phone = (input.driver_phone or "").strip() or _infer_driver_phone(schema, answers)
    phone = (normalize_vn_phone(phone) or phone) if phone else None
    if phone:
        answers["driver_phone"] = phone
    if "driver_name" not in answers and input.driver_name:
        answers["driver_name"] = input.driver_name
    if input.signature_png and answers.get("driver_signature") and str(input.signature_png) != str(answers["driver_signature"]):
        raise SubmissionConflict("Conflicting signature aliases in one submission.")
    if input.signature_png:
        answers["driver_signature"] = input.signature_png
    signature = answers.get("driver_signature") or input.signature_png

    camera_questions = [q for q in (schema.questions if schema else []) if q.type == "camera_webrtc"]
    by_id = {q.id: q for q in camera_questions}
    upload_items = [
        {"question_id": u.question_id, "question": by_id.get(u.question_id),
         "slot_index": u.slot_index, "flat_index": index, "filename": u.filename}
        for index, u in enumerate(input.uploads)
    ]
    target_question_id = (
        input.question_id
        or (input.uploads[0].question_id if input.uploads else None)
        or next(iter(by_id), None)
        or "camera_receipt"
    )
    if input.uploads:
        uploaded_by_question: dict[str, set[int]] = {}
        for upload in input.uploads:
            uploaded_by_question.setdefault(upload.question_id, set()).add(upload.slot_index)
        for question_id, slot_indexes in uploaded_by_question.items():
            question = by_id.get(question_id)
            if not answers.get(question_id):
                slots = range(max(slot_indexes) + 1) if question and question.image_slots else sorted(slot_indexes)
                answers[question_id] = [f"uploaded_{i}" for i in slots]
            for slot in slot_indexes:
                answers.setdefault(f"{question_id}_{slot}", f"uploaded_{slot}")
    elif input.is_test_mode and input.bypass_img_check:
        for question in camera_questions:
            slot_count = len(question.image_slots or []) or question.min_images or 1
            answers[question.id] = [f"bypassed_slot_{i}" for i in range(slot_count)]
            for slot in range(slot_count):
                answers.setdefault(f"{question.id}_{slot}", f"bypassed_slot_{slot}")
    elif not answers.get(target_question_id):
        answers[target_question_id] = None
    if input.gross_revenue is not None:
        answers.setdefault("daily_net_income", input.gross_revenue)
        answers.setdefault("monthly_gross_revenue", input.gross_revenue)

    clean_form_data = {k: v for k, v in answers.items() if not k.startswith("survey_photo")}
    mode = "TEST_DEV_MODE" if input.is_test_mode else "PROD_MODE"
    phone_markers = ("phone", "điện thoại", "dien thoai", "sđt", "sdt", "zalo")
    phone_question_id = next((q.id for q in (schema.questions if schema else []) if any(
        marker in f"{q.id} {q.title}".lower() for marker in phone_markers
    )), None)
    transport_keys = {"driver_phone", "signature_png", "driver_signature", "idempotency_key", "form_id",
                      "is_test_mode", "bypass_img_check", "question_id", "collaborator_id", "ctv_id", "ctv_code",
                      "collaborator_tg_id", "_delivery_policy", *by_id}
    if phone_question_id:
        transport_keys.add(phone_question_id)
    for camera_id in by_id:
        transport_keys.update(k for k in clean_form_data if k.startswith(f"{camera_id}_") and k[len(camera_id) + 1:].isdigit())
    raw_bytes = tuple(u.content for u in input.uploads)
    fingerprint = request_fingerprint(
        input.form_id, mode, {k: v for k, v in clean_form_data.items() if k not in transport_keys},
        raw_bytes, [(u.question_id, u.slot_index) for u in input.uploads],
        signature=signature,
        gps={
            "lat": answers.get("gps_latitude") or form_data.get("gps_latitude"),
            "lng": answers.get("gps_longitude") or form_data.get("gps_longitude"),
            "acc": answers.get("gps_accuracy") or answers.get("gps_accuracy_meters"),
        },
        ctv=str(input.collaborator_id or "").strip() or None,
    )
    # The delivery policy is a server-owned snapshot, never a client answer.
    clean_form_data.pop("_delivery_policy", None)
    return _Prepared(
        phone, hash_phone(phone) if phone else None,
        generate_driver_tag(phone) if phone else f"ANON-{secrets.token_hex(4).upper()}",
        answers, clean_form_data, target_question_id, camera_questions, upload_items, raw_bytes,
        _Attempt(attempt_scope(input.form_id, mode, phone), (input.idempotency_key or "").strip() or None, fingerprint),
    )


async def _evaluate(
    input: SubmissionInput, deps: SubmissionDependencies, prepared: _Prepared,
) -> SubmissionEvaluation:
    schema = input.schema
    target_question_id = prepared.target_question_id
    target_q = next((q for q in schema.questions if q.id == target_question_id), None) if schema else None
    camera_questions = prepared.camera_questions
    if deps.db_pool and any(getattr(question, "image_slots", None) for question in camera_questions):
        try:
            await refresh_image_object_registry(deps.db_pool)
        except Exception as exc:
            logger.warning("[IMAGE OBJECTS] database registry refresh failed: %s", type(exc).__name__)
            if deps.production:
                raise SubmissionUnavailable("Không thể tải cấu hình kiểm tra ảnh; vui lòng thử lại sau.") from exc
    upload_items = prepared.upload_items
    raw_bytes_list = prepared.raw_bytes
    driver_phone = prepared.phone
    license_plate = input.license_plate
    checks = get_effective_image_checks(schema, target_question_id)
    is_schema_valid, validation_err = True, None
    if input.upload_error:
        is_schema_valid, validation_err = False, input.upload_error
    elif schema:
        is_schema_valid, validation_err = validate_submission_against_schema(schema, prepared.answers)
    is_bypassed = input.is_test_mode and input.bypass_img_check
    pil_im = blur_var = None
    norm_bytes_cache: dict[int, bytes] = {}
    embeddings: dict[int, list[float] | None] = {}
    compat_income: Dict[str, Dict[str, Any]] = {}
    compat_totals: Dict[str, int] = {"total_gross_revenue": 0, "total_net_income": 0, "total_trips": 0}
    if not is_schema_valid:
        status_str = "REJECTED"
        is_approved = False
        reason_str = validation_err or "Không hợp lệ theo biểu mẫu."
        ai_res = {}
    else:
        # 1. Fleet Anti-Fraud: Route through VehiclePersistenceTracker when license plate & phone are provided
        v_match = None
        if checks.enable_vehicle_match and license_plate and driver_phone:
            tracker = VehiclePersistenceTracker(db_pool=None if input.is_test_mode else deps.db_pool)
            v_match = await tracker.verify_and_persist_vehicle(
                driver_phone=driver_phone,
                detected_plate=license_plate,
                detected_vehicle_type=input.vehicle_type or "CAR",
                driver_phone_hash=prepared.phone_hash,
                persist=False,
            )

        if is_bypassed:
            ai_res = {
                "status": "APPROVED",
                "gross_revenue": input.gross_revenue or "650000",
                "driver_phone": driver_phone,
                "detected_plate": license_plate or "29E-123.45",
                "is_screen_photo": False,
                "bypass_img_check": True,
            }
            if v_match and not v_match.is_match:
                is_approved = False
                status_str = "REJECTED"
                reason_str = v_match.flag_reason or f"Lỗi phương tiện: {v_match.status_code}"
            else:
                is_approved = True
                status_str = "APPROVED"
                reason_str = "Chế độ thử nghiệm: Đã bỏ qua kiểm tra ảnh để đẩy nhanh luồng dữ liệu mẫu."
        else:
            # Decode + downscale every upload exactly ONCE, in worker threads.
            # The previous flow decoded each image up to four times (target frame,
            # blur loop, AI normalisation pass, duplicate detector) synchronously on
            # the event loop, so a single submission froze the whole uvicorn worker
            # for seconds and the client sat on a dead "Đang gửi..." button.
            norm_results = await asyncio.gather(
                *[
                    asyncio.to_thread(
                        deps.normalize_image,
                        b,
                        max_dimension=deps.image_max_dimension,
                    )
                    for b in raw_bytes_list
                ],
                return_exceptions=True,
            )
            pil_cache: Dict[int, Any] = {}
            for _idx, _res in enumerate(norm_results):
                if isinstance(_res, tuple) and _res[1] is not None:
                    norm_bytes_cache[_idx] = _res[0]
                    pil_cache[_idx] = _res[1]

            # For facts extraction, select the app revenue screen if multi-image
            target_idx = 1 if len(raw_bytes_list) >= 2 else 0
            target_norm = norm_results[target_idx] if 0 <= target_idx < len(norm_results) else None
            if isinstance(target_norm, tuple) and target_norm[1] is not None:
                norm_bytes, pil_im = target_norm
            else:
                decode_err = target_norm if isinstance(target_norm, Exception) else "unreadable image"
                norm_bytes, pil_im = None, None
                is_approved = False
                status_str = "REJECTED"
                reason_str = f"Không thể giải mã ảnh: {str(decode_err)}"
                ai_res = {}

            if pil_im is not None:
                is_sharp = True
                blur_var = None
                blur_thresh = deps.blur_threshold
                blurry_slot_idx = None
                blurry_slot_title = None
                decode_error_slot_idx = None
                decode_error_slot_title = None

                for idx, item in enumerate(upload_items):
                    item_question = item.get("question") if item else None
                    item_slot = int(item.get("slot_index", idx)) if item else idx
                    slot_checks = get_effective_image_checks(
                        schema,
                        item.get("question_id") if item else target_question_id,
                        slot_index=item_slot,
                    )
                    if not slot_checks.enable_blur_check:
                        continue
                    slot_blur_thresh = slot_checks.blur_threshold if slot_checks.blur_threshold is not None else deps.blur_threshold
                    slot_name = f"Ảnh {idx + 1}"
                    if item_question is not None and getattr(item_question, "image_slots", None):
                        stitle = _slot_title(item_question, item_slot)
                        if stitle:
                            slot_name = f"{item.get('question_id')} - {stitle}"
                    elif target_q and target_q.image_slots and idx < len(target_q.image_slots):
                        stitle = target_q.image_slots[idx].get("title") or target_q.image_slots[idx].get("role")
                        if stitle:
                            slot_name = f"Ảnh {idx + 1} ({stitle})"
                    try:
                        p_im = pil_cache.get(idx)
                        if p_im is None:
                            raise ValueError("unreadable image stream")
                        sharp, b_var = await asyncio.to_thread(
                            compute_laplacian_blur_score, p_im, slot_blur_thresh
                        )
                        if not sharp:
                            is_sharp = False
                            blur_var = b_var
                            blurry_slot_idx = idx
                            blurry_slot_title = slot_name
                            blur_thresh = slot_blur_thresh
                            break
                        blur_var = b_var
                    except Exception:
                        is_sharp = False
                        decode_error_slot_idx = idx
                        decode_error_slot_title = slot_name
                        break

                if not is_sharp:
                    is_approved = False
                    status_str = "REJECTED"
                    if decode_error_slot_title:
                        reason_str = f"{decode_error_slot_title}: Không thể giải mã tệp ảnh (định dạng stream lỗi hoặc file hỏng). Vui lòng chụp lại!"
                    elif blurry_slot_title:
                        score_text = f" (độ nét đo được: {blur_var:.1f} < ngưỡng tối thiểu {blur_thresh})" if blur_var is not None else ""
                        reason_str = f"{blurry_slot_title}: Ảnh chụp bị mờ hoặc rung tay{score_text}, vui lòng giữ chắc tay và chụp lại rõ nét hơn!"
                    else:
                        reason_str = "Ảnh chụp bị mờ hoặc rung tay, vui lòng chụp lại rõ nét hơn!"
                    ai_res = {
                        "status": "REJECTED",
                        "failed_slot": blurry_slot_idx if blurry_slot_idx is not None else decode_error_slot_idx,
                        "failed_slot_name": blurry_slot_title or decode_error_slot_title,
                        "blur_score": blur_var,
                        "blur_threshold": blur_thresh if checks.enable_blur_check else None,
                    }
                elif v_match and not v_match.is_match:
                    is_approved = False
                    status_str = "REJECTED"
                    reason_str = v_match.flag_reason or f"Lỗi phương tiện: {v_match.status_code}"
                    ai_res = {}
                elif not checks.enable_ai_vision:
                    ai_res = {"skipped": True, "reason": "Bỏ qua kiểm tra AI theo cấu hình"}
                    is_approved = True
                    status_str = "APPROVED"
                    reason_str = "Chấp thuận (bỏ qua kiểm tra AI theo cấu hình biểu mẫu)."
                else:
                    # AI vision audit with parallel execution via asyncio.gather
                    camera_groups: Dict[str, List[Dict[str, Any]]] = {}
                    for item in upload_items:
                        question = item.get("question")
                        if question is None or not getattr(question, "image_slots", None):
                            continue
                        camera_groups.setdefault(question.id, []).append(item)

                    if camera_groups:
                        audits: List[Dict[str, Any]] = []
                        for question in camera_questions:
                            group_items = camera_groups.get(question.id)
                            if not group_items:
                                continue
                            audits.append(
                                await _audit_camera_question(deps.vision, question, group_items, norm_bytes_cache)
                            )

                        image_object_results = [
                            result for audit in audits for result in audit["image_object_results"]
                        ]
                        all_income_pairs = [
                            pair for audit in audits for pair in audit["income_pairs"]
                        ]
                        income_required = sum(audit["income_required"] for audit in audits)
                        coverage_violation = next(
                            (audit["violation"] for audit in audits if audit["violation"]),
                            None,
                        )

                        def _period_start(pair):
                            extracted = pair[1]
                            if isinstance(extracted, dict):
                                start, _, _ = parse_date_range_from_text(
                                    str(extracted.get("period") or ""), default_year=2026
                                )
                                if start:
                                    return (0, start)
                            return (1, pair[0])

                        all_income_pairs.sort(key=_period_start)
                        duplicate_week_violation = None if coverage_violation else _find_duplicate_income_period(all_income_pairs)
                        short_income_violation = None
                        if income_required and len(all_income_pairs) < income_required:
                            short_income_violation = {
                                "failed_slot": all_income_pairs[0][0] if all_income_pairs else 0,
                                "ocr_data": {},
                                "reason": (
                                    f"Thiếu ảnh thu nhập hợp lệ: chỉ nhận diện được "
                                    f"{len(all_income_pairs)}/{income_required} kỳ. "
                                    "Vui lòng chụp đủ các ảnh thu nhập được yêu cầu."
                                ),
                            }
                        monthly_violation = coverage_violation or short_income_violation or duplicate_week_violation
                        matched_income_indices = [pair[0] for pair in all_income_pairs]

                        profile_audit = next(
                            (audit for audit in audits if audit["profile_object"] is not None),
                            None,
                        )
                        profile_res: Any = profile_audit["profile_res"] if profile_audit else {}
                        membership_type = (
                            profile_res.get("membership_type")
                            if isinstance(profile_res, dict) else None
                        )
                        role_0 = (
                            profile_audit["role_0"] if profile_audit
                            else (audits[0]["role_0"] if audits else "unknown")
                        )
                        primary_ocr = (
                            all_income_pairs[0][1]
                            if all_income_pairs and isinstance(all_income_pairs[0][1], dict)
                            else {}
                        )
                        compat_income = project_income_weeks(image_object_results)
                        compat_totals = project_income_totals(compat_income)

                        profile_question = profile_audit["question"] if profile_audit else None
                        profile_slots = getattr(profile_question, "image_slots", None) or []
                        slot_0_role = _infer_slot_role(profile_slots[0]) if profile_slots else "app_profile"
                        slot_0_title = _slot_title(profile_question, 0) if profile_question else "Ảnh 1"
                        slot_0_is_profile = bool(profile_audit and profile_audit["slot_0_is_profile"])

                        is_approved = False
                        if monthly_violation:
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = monthly_violation["reason"]
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": monthly_violation["failed_slot"],
                                "reason": reason_str,
                                "ocr_data": monthly_violation["ocr_data"],
                                "image_object_results": image_object_results,
                            }
                        elif slot_0_is_profile and role_0 in ("app_income", "revenue_screen"):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = f"Ảnh 1 ({slot_0_title}): Phát hiện sai loại màn hình (đây là màn hình thống kê thu nhập, không phải màn hình hồ sơ/tiện ích ứng dụng). Vui lòng chụp đúng màn hình hồ sơ ứng dụng ở Ảnh 1!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": 0,
                                "reason": reason_str,
                                "detected_roles": [role_0],
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        elif slot_0_is_profile and role_0 != slot_0_role and role_0 not in ("app_profile", "odometer"):
                            is_approved = False
                            status_str = "REJECTED"
                            reason_str = f"Ảnh 1 ({slot_0_title}): Không phát hiện màn hình hồ sơ ứng dụng tài xế Xanh SM (Phát hiện vật thể không hợp lệ). Vui lòng chụp đúng màn hình hồ sơ ứng dụng tài xế!"
                            ai_res = {
                                "status": "REJECTED",
                                "failed_slot": 0,
                                "reason": reason_str,
                                "detected_roles": [role_0],
                                "model": "DEEPSEEK_V3_CHAT",
                            }
                        else:
                            is_approved = True
                            status_str = "APPROVED"
                            reason_str = "Gửi phiếu khảo sát thành công!"
                            hours_val = str(primary_ocr.get("online_hours") or "")
                            period_val = str(primary_ocr.get("period") or "")
                            ai_res = {
                                "status": "APPROVED",
                                "model": "DEEPSEEK_V3_CHAT",
                                "roles_verified": [audit["role_0"] for audit in audits],
                                "total_slots_processed": len(raw_bytes_list),
                                "membership_type": membership_type,
                                "image_object_results": image_object_results,
                                "income_totals": compat_totals,
                                "online_hours": hours_val,
                                "period": period_val,
                            }

                        should_check_fraud = bool(getattr(checks, "enable_anti_fraud", True) and getattr(checks, "require_unique", True))
                        if is_approved and should_check_fraud:
                            exempt_embeddings, exempt_dhashes = await _load_active_image_exceptions(
                                deps.db_pool, input.form_id
                            )
                            intra_dup, intra_reason = await embedding_executor.run_inference(
                                check_intra_submission_duplicates,
                                raw_bytes_list, deepcopy(target_q), deepcopy(compat_income),
                                income_indices=tuple(matched_income_indices),
                                pil_cache={idx: image.copy() for idx, image in pil_cache.items()},
                                exempt_embeddings=exempt_embeddings,
                                exempt_dhashes=exempt_dhashes,
                            )
                            if intra_dup:
                                is_approved = False
                                status_str = "REJECTED"
                                reason_str = intra_reason
                                ai_res["status"] = "REJECTED"
                                ai_res["reason"] = reason_str
                                ai_res["duplicate_detected"] = True
                                ai_res["duplicate_type"] = "INTRA_SUBMISSION"
                            else:
                                check_idx = _select_unique_check_index(
                                    target_q, matched_income_indices, len(raw_bytes_list)
                                )
                                check_im = pil_cache.get(check_idx) if check_idx is not None else None

                                if check_im is not None:
                                    if deps.db_pool:
                                        primary_emb = await embedding_executor.run_inference(
                                            compute_dinov2_embedding, check_im.copy(),
                                        )
                                        embeddings[check_idx] = primary_emb
                                        inter_dup, matched_sub_id, dup_type = await check_inter_submission_duplicates(
                                            deps.db_pool, check_im, primary_emb,
                                            weekly_incomes=compat_income,
                                            tot_gross=compat_totals["total_gross_revenue"],
                                            tot_trips=compat_totals["total_trips"],
                                            form_id=input.form_id,
                                        )
                                        if inter_dup:
                                            is_approved = False
                                            status_str = "REJECTED"
                                            if dup_type == "DATA_DUPLICATE":
                                                reason_str = f"Số liệu doanh thu các kỳ bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{matched_sub_id})! Hệ thống chống gian lận phát hiện số liệu bị sao chép."
                                            elif dup_type == "IMAGE_DHASH":
                                                reason_str = f"Ảnh chụp đã được sử dụng trong phiếu khảo sát trước (#{matched_sub_id})! Hệ thống phát hiện ảnh trùng (pHash)."
                                            else:
                                                reason_str = f"Ảnh chụp đã được sử dụng trong phiếu khảo sát trước (#{matched_sub_id})! Hệ thống Meta DINOv2 phát hiện ảnh bị nộp lại."
                                            ai_res["status"] = "REJECTED"
                                            ai_res["reason"] = reason_str
                                            ai_res["duplicate_detected"] = True
                                            ai_res["matched_submission_id"] = matched_sub_id
                                            ai_res["duplicate_type"] = dup_type or "INTER_SUBMISSION"
                                    elif input.is_test_mode:
                                        dev_storage_file = deps.dev_storage_file
                                        if os.path.exists(dev_storage_file):
                                            try:
                                                with open(dev_storage_file, "r", encoding="utf-8") as df:
                                                    dev_recs = json.load(df)
                                                target_dhash = compute_dhash_int(check_im)
                                                for prev in dev_recs:
                                                    if prev.get("status") != "APPROVED":
                                                        continue
                                                    prev_w = prev.get("weekly_incomes") or {}
                                                    prev_g = int(prev.get("total_gross_revenue") or prev.get("gross_revenue") or 0)
                                                    prev_t = int(prev.get("total_trips") or prev.get("trip_count") or 0)
                                                    cur_w1 = int(compat_income.get("week_1", {}).get("gross_revenue") or 0)
                                                    cur_w2 = int(compat_income.get("week_2", {}).get("gross_revenue") or 0)
                                                    if prev_g > 0 and compat_totals["total_gross_revenue"] > 0 and prev_g == compat_totals["total_gross_revenue"] and prev_t > 0 and prev_t == compat_totals["total_trips"]:
                                                        p_w1 = int(prev_w.get("week_1", {}).get("gross_revenue") or prev.get("income_week_1_gross") or 0)
                                                        p_w2 = int(prev_w.get("week_2", {}).get("gross_revenue") or prev.get("income_week_2_gross") or 0)
                                                        if p_w1 > 0 and p_w1 == cur_w1 and p_w2 > 0 and p_w2 == cur_w2:
                                                            is_approved = False
                                                            status_str = "REJECTED"
                                                            reason_str = f"Số liệu doanh thu các kỳ bị trùng lặp hoàn toàn với phiếu khảo sát đã duyệt trước đây (#{prev.get('submission_id')})! Phát hiện sao chép số liệu thu nhập."
                                                            ai_res["status"] = "REJECTED"
                                                            ai_res["reason"] = reason_str
                                                            ai_res["duplicate_detected"] = True
                                                            ai_res["matched_submission_id"] = prev.get('submission_id')
                                                            ai_res["duplicate_type"] = "INTER_SUBMISSION_DATA"
                                                            break
                                                    if prev.get("image_dhash_int") is not None:
                                                        prev_dh = int(prev["image_dhash_int"])
                                                        if hamming_distance(target_dhash, prev_dh) <= 6:
                                                            is_approved = False
                                                            status_str = "REJECTED"
                                                            reason_str = f"Ảnh chụp đã được sử dụng trong phiếu khảo sát trước (#{prev.get('submission_id')})! Phát hiện trùng lặp hình ảnh."
                                                            ai_res["status"] = "REJECTED"
                                                            ai_res["reason"] = reason_str
                                                            ai_res["duplicate_detected"] = True
                                                            ai_res["matched_submission_id"] = prev.get('submission_id')
                                                            ai_res["duplicate_type"] = "INTER_SUBMISSION"
                                                            break
                                            except Exception:
                                                pass

                    if not camera_groups:
                        try:
                            facts = await deps.vision.extract_facts(norm_bytes)
                            ai_res = facts.model_dump() if hasattr(facts, "model_dump") else {}
                            if not getattr(checks, "enable_object_detection", False):
                                if getattr(facts, "is_manipulated_or_photoshopped", False):
                                    is_approved = False
                                    status_str = "REJECTED"
                                    reason_str = "Hệ thống phát hiện dấu hiệu chỉnh sửa ảnh kỹ thuật số (Photoshop)."
                                else:
                                    is_approved = True
                                    status_str = "APPROVED"
                                    reason_str = "Ảnh chụp rõ nét, đạt chuẩn kiểm tra hình ảnh (Đã tắt nhận diện đối tượng theo yêu cầu)."
                            else:
                                compliance = evaluate_survey_compliance(facts, checks=checks)
                                is_approved = bool(compliance.passed)
                                status_str = "APPROVED" if is_approved else "REJECTED"
                                reason_str = compliance.collaborator_note or compliance.reason_code
                        except embedding_executor.InferenceBusyError:
                            raise
                        except Exception as ai_err:
                            if isinstance(ai_err, (asyncio.TimeoutError, TimeoutError)):
                                logger.error(f"Vision AI call timed out: {ai_err}")
                                ai_res = {"status": "manual_review", "error": "AI Vision timeout"}
                                is_approved = False
                                status_str = "MANUAL_REVIEW"
                                reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
                            elif not getattr(checks, "enable_object_detection", False):
                                ai_res = {"status": "approved", "note": "passed image quality check"}
                                is_approved = True
                                status_str = "APPROVED"
                                reason_str = "Ảnh chụp rõ nét, đạt chuẩn kiểm tra hình ảnh (Bỏ qua nhận diện đối tượng)."
                            else:
                                logger.error(f"Vision AI call failed: {ai_err}")
                                ai_res = {"status": "manual_review", "error": str(ai_err)}
                                is_approved = False
                                status_str = "MANUAL_REVIEW"
                                reason_str = "Hệ thống đã tiếp nhận ảnh và chuyển sang chờ kiểm tra thủ công."
    if is_schema_valid and schema and getattr(schema.settings, "modules", None):
        try:
            module_eval = await run_form_modules(schema, prepared.form_data, deps.db_pool)
        except Exception as exc:
            logger.error("[FORM MODULES] execution failed: %s", type(exc).__name__)
            module_eval = {
                "outputs": {},
                "failure": {
                    "module_uid": "",
                    "module_name": "form modules",
                    "message": "Không thể chạy cấu hình module của biểu mẫu; vui lòng thử lại sau.",
                },
            }
        if module_eval.get("outputs"):
            ai_res["module_outputs"] = module_eval["outputs"]
            prepared.form_data["module_outputs"] = module_eval["outputs"]
        failure = module_eval.get("failure")
        if failure:
            is_approved = False
            status_str = "REJECTED"
            reason_str = failure["message"]
            ai_res["module_failure"] = failure
    is_dissent = str(prepared.answers.get("consent_choice") or "").strip() == "Tôi không đồng ý"
    if schema:
        is_dissent = is_dissent or any(
            not _is_blank(prepared.answers.get(gate.id))
            and not gate_satisfied(gate, prepared.answers[gate.id])
            for gate in effective_gates(schema)
        )
    if is_dissent:
        is_approved = False
        status_str = "REJECTED"
        reason_str = "Tài xế không đồng ý xử lý dữ liệu theo Nghị định 13."
    require_manual_review = bool(getattr(getattr(schema, "settings", None), "require_manual_review", True))
    # Test mode only selects storage/delivery; the form's review setting still wins,
    # otherwise a "test" form silently auto-approves work that never gets reviewed.
    if require_manual_review and is_approved and not is_bypassed and not is_dissent:
        status_str = "MANUAL_REVIEW"
        reason_str = "Phiếu khảo sát của bạn đã được ghi nhận. Ban Quản Trị đang tiến hành đối soát thủ công. Vui lòng mở Telegram @TrustBridgeVN_bot để nhận kết quả!"
    return SubmissionEvaluation(
        norm_bytes_cache, ai_res, status_str, reason_str, is_approved, blur_var,
        compat_income, compat_totals, pil_im, embeddings,
    )


def _commit(response: dict[str, Any], ids=(), *, replayed: bool = False) -> SubmissionCommit:
    return SubmissionCommit(response["submission_id"], response["status"], response, tuple(ids), replayed)


def _attempt_outcome(state: str, response: dict[str, Any] | None) -> SubmissionCommit | None:
    if state == "replay":
        if response is None:
            raise SubmissionUnavailable("Lượt gửi đã hoàn tất nhưng phản hồi chưa sẵn sàng.")
        return _commit(response, replayed=True)
    if state == "conflict":
        raise SubmissionConflict("Idempotency key was used with a different request payload.")
    if state == "reserved":
        raise SubmissionUnavailable("Submission attempt is still unresolved; retry after reconciliation.")
    return None


async def _lookup_attempt(pool: Any, attempt: _Attempt) -> tuple[str, dict | None]:
    started = time.monotonic()
    async with pool.connection() as conn:
        result = await reserve(conn, attempt.scope, attempt.key, attempt.fingerprint)
        await conn.rollback()
        logger.info("submission_attempt_lookup outcome=%s elapsed_ms=%.3f", result[0], (time.monotonic() - started) * 1000)
        return result


async def _writer_ready(deps: SubmissionDependencies, *, durable: bool) -> bool:
    if not (deps.production or durable):
        return True
    if deps.db_pool is None:
        return False
    try:
        async with deps.db_pool.connection() as conn:
            if not await writer_schema_ready(conn):
                return False
            if durable:
                await require_delivery_schema(conn)
            return True
    except Exception:
        return False


def _record(
    input: SubmissionInput, prepared: _Prepared, eval: SubmissionEvaluation,
    submission_id: str, manifest: list[dict],
) -> tuple[dict[str, Any], dict[str, Any]]:
    fields, answers = input.fields, prepared.answers
    ai = eval.ai_result
    weeks = eval.weekly_income or ai.get("weekly_incomes") or {}
    income_columns: dict[str, int] = {}
    totals = {"gross": 0, "net": 0, "trips": 0}
    for number in range(1, 6):
        week = weeks.get(f"week_{number}", {})
        gross = week.get("gross_revenue")
        net = week.get("net_income")
        trips = week.get("total_trips")
        if number == 1:
            gross = gross or ai.get("gross_revenue") or input.gross_revenue
            net = net or ai.get("net_income")
            trips = trips or input.trip_count
        for key, value in (("gross", gross), ("net", net), ("trips", trips)):
            count = _to_int(value)
            income_columns[f"income_week_{number}_{key}"] = count
            totals[key] += count
    for key, total_key in (("gross", "total_gross_revenue"), ("net", "total_net_income"), ("trips", "total_trips")):
        totals[key] = _to_int(eval.income_totals.get(total_key) or totals[key])
    dissent = str(answers.get("consent_choice") or "").strip() == "Tôi không đồng ý"
    daily_trips = fields.get("daily_trips")
    incentives = fields.get("gsm_incentive_policy") or ""
    record = {
        "submission_id": submission_id,
        "form_id": input.form_id,
        "ctv_code": str(input.collaborator_id or answers.get("ctv_code") or "").strip() or None,
        "driver_phone": None if dissent else prepared.phone,
        "driver_name": None if dissent else (input.driver_name or "Tài xế"),
        "receipt_serial_no": f"SN-{input.license_plate or 'AUTO'}",
        "status": "REJECTED" if dissent else eval.status,
        "rejection_reason": (
            eval.reason
            if (dissent or not eval.is_approved)
            else None
        ),
        "form_data": {**prepared.form_data, "_delivery_policy": delivery_policy_snapshot(
            input.delivery_schema or input.schema, dissent=dissent)},
        "raw_extracted_facts": ai,
        "consent_choice": str(answers.get("consent_choice") or "Tôi đồng ý"),
        "vehicle_type": _normalize_vehicle_type(
            fields.get("vehicle_type") or input.vehicle_type
            or _answer_by_title_keywords(input.schema, answers, ("phương tiện", "dòng xe", "loại xe"))
            or "CAR"
        ),
        "car_model": str(fields.get("car_model") or "").strip()
        or _answer_by_title_keywords(input.schema, answers, ("dòng xe ô tô điện", "dòng xe máy điện", "model", "dòng xe")) or "",
        "gsm_duration": str(fields.get("gsm_duration") or "").strip()
        or _answer_by_title_keywords(input.schema, answers, ("bao lâu",)) or "",
        "gsm_contract_type": str(fields.get("gsm_contract_type") or "").strip()
        or _answer_by_title_keywords(input.schema, answers, ("hình thức hợp tác",)) or "",
        "daily_trips": int(daily_trips) if daily_trips is not None and str(daily_trips).isdigit() else None,
        "monthly_income_range": str(fields.get("monthly_income_range") or ""),
        "gsm_incentive_policy": "; ".join(str(x) for x in incentives) if isinstance(incentives, list) else str(incentives),
        "membership_type": ai.get("membership_type") or fields.get("membership_type")
        or _answer_by_title_keywords(input.schema, answers, ("hạng thành viên", "hạng")) or "Chưa xác định",
        "gps_latitude": _to_float(answers.get("gps_latitude") or fields.get("gps_latitude")),
        "gps_longitude": _to_float(answers.get("gps_longitude") or fields.get("gps_longitude")),
        "gps_accuracy_meters": _to_float(
            answers.get("gps_accuracy") or answers.get("gps_accuracy_meters") or fields.get("gps_accuracy")
        ),
        "signature_r2_url": None,
        "image_r2_url": None,
        "audit_seal_hash": hashlib.sha256(
            f"{submission_id}:{None if dissent else prepared.phone}:{eval.status}:"
            f"{json.dumps(prepared.form_data, sort_keys=True, default=str)}".encode()
        ).hexdigest(),
        **income_columns,
        "total_gross_revenue": _to_int(totals["gross"] if totals["gross"] > 0 else (ai.get("gross_revenue") or input.gross_revenue)),
        "total_net_income": _to_int(totals["net"] if totals["net"] > 0 else ai.get("net_income")),
        "total_trips": _to_int(totals["trips"] if totals["trips"] > 0 else input.trip_count),
        "action_status": init_action_checklist(eval.is_approved, bool(input.collaborator_id)),
        "created_at": input.received_at,
        "media_manifest": manifest,
        "media_version": 1,
    }
    return record, weeks


def _response(
    input: SubmissionInput, prepared: _Prepared, eval: SubmissionEvaluation, submission_id: str,
) -> dict[str, Any]:
    ai = eval.ai_result
    response = {
        "status": eval.status,
        "submission_id": submission_id,
        "mode": "TEST_DEV_MODE" if input.is_test_mode else "PROD_MODE",
        "reason": eval.reason,
        "driver_tag": prepared.driver_tag,
        "driver_phone": prepared.phone,
        "detected_plate": ai.get("detected_plate") or input.license_plate,
        "gross_revenue": ai.get("gross_revenue") or input.gross_revenue,
        "net_income": ai.get("net_income"),
        "trip_count": ai.get("total_trips") or input.trip_count,
        "ocr_data": ai.get("ocr_data"),
    }
    if ai.get("duplicate_detected"):
        response.update(duplicate_detected=ai["duplicate_detected"], duplicate_type=ai.get("duplicate_type"))
    if ai.get("module_outputs"):
        response["module_outputs"] = ai["module_outputs"]
    if eval.blur_score is not None:
        response["blur_score"] = eval.blur_score
    return response


def _dev_record(input: SubmissionInput, record: dict, prepared: _Prepared, eval: SubmissionEvaluation, weeks: dict) -> dict:
    return {
        **{k: v for k, v in record.items() if k.startswith("income_week_") or k in (
            "submission_id", "form_id", "status", "action_status", "consent_choice", "form_data",
            "total_gross_revenue", "total_net_income", "total_trips", "driver_phone", "driver_name",
            "vehicle_type", "car_model", "gsm_duration", "gsm_contract_type", "daily_trips",
            "media_manifest", "media_version",
        )},
        "mode": "TEST_DEV_MODE",
        "weekly_incomes": weeks,
        "driver_tag": prepared.driver_tag,
        "collaborator_id": input.collaborator_id,
        "ai_result": eval.ai_result,
        "image_dhash_int": compute_dhash_int(eval.target_image) if eval.target_image is not None else None,
        "submitted_at": input.received_at.isoformat(),
        "net_income": record["total_net_income"],
        "trip_count": record["total_trips"],
        "license_plate": input.license_plate or eval.ai_result.get("detected_plate"),
        "reason": eval.reason,
    }


async def _insert_row(cur, columns: dict[str, Any]) -> bool:
    json_columns = {"form_data", "raw_extracted_facts", "action_status", "media_manifest"}
    params = tuple(json.dumps(v) if k in json_columns else v for k, v in columns.items())
    placeholders = ", ".join(["%s"] * len(columns))
    await cur.execute(
        f"INSERT INTO survey_submissions ({', '.join(columns)}) VALUES ({placeholders}) "
        "ON CONFLICT (submission_id) DO NOTHING;",
        params,
    )
    return cur.rowcount != 0


async def _legacy_event(cur, submission_id: str, event_type: str, payload: dict) -> tuple[str, ...]:
    await cur.execute(
        "INSERT INTO outbox_events (event_type, flow_target, aggregate_id, payload, status) "
        "VALUES (%s, 'ALL', %s, %s, 'PENDING') RETURNING id;",
        (event_type, submission_id, json.dumps(payload)),
    )
    row = await cur.fetchone()
    return (str(row[0]),) if row else ()


async def _persist(
    input: SubmissionInput, deps: SubmissionDependencies, prepared: _Prepared,
    eval: SubmissionEvaluation, record: dict, response: dict, *, durable: bool,
) -> SubmissionCommit:
    attempt = prepared.attempt
    submission_id = record["submission_id"]
    pilot = eval.target_image
    # Never hold an SQL write transaction or reserve a key while waiting for CPU admission.
    digest = compute_image_dhash_str(pilot) if pilot is not None else None
    target_index = 1 if len(prepared.raw_bytes) >= 2 else 0
    embedding = eval.embeddings.get(target_index)
    if target_index not in eval.embeddings and pilot is not None:
        embedding = await embedding_executor.run_inference(compute_dinov2_embedding, pilot.copy())
    async with deps.db_pool.connection() as conn:
        if attempt.key:
            previous = _attempt_outcome(*await reserve(conn, attempt.scope, attempt.key, attempt.fingerprint))
            if previous is not None:
                await conn.rollback()
                return previous
        async with conn.cursor() as cur:
            driver_id = None
            if record["driver_phone"]:
                await cur.execute(
                    "INSERT INTO driver_identity_vault (driver_id, driver_phone, driver_name) VALUES (%s, %s, %s) "
                    "ON CONFLICT (driver_phone) DO UPDATE SET driver_name = EXCLUDED.driver_name "
                    "RETURNING driver_id;",
                    (prepared.driver_tag, record["driver_phone"], record["driver_name"]),
                )
                row = await cur.fetchone()
                driver_id = row[0] if row else None
            # Keep the legacy extended-column fallback scoped to a single statement.
            # A durable row cannot lose the columns needed by its delivery policy.
            await cur.execute("SAVEPOINT survey_extended_insert")
            columns = {
                **record, "driver_id": driver_id, "image_dhash": digest,
                "embedding": "[" + ",".join(str(x) for x in embedding) + "]" if embedding else None,
            }
            if durable:
                columns["work_owner"] = "outbox_v2"
            try:
                inserted = await _insert_row(cur, columns)
            except pg_errors.UndefinedColumn:
                if durable:
                    raise
                logger.warning("[SUBMIT] Extended columns unavailable for #%s", submission_id)
                await cur.execute("ROLLBACK TO SAVEPOINT survey_extended_insert")
                inserted = await _insert_row(cur, {k: record[k] for k in (
                    "submission_id", "form_id", "ctv_code", "driver_phone", "driver_name", "receipt_serial_no",
                    "status", "rejection_reason", "form_data", "raw_extracted_facts", "created_at",
                    "media_manifest", "media_version",
                )})
            await cur.execute("RELEASE SAVEPOINT survey_extended_insert")
            if not inserted:
                # An existing ID (including one from before a flag rollback) owns its work.
                raise SubmissionUnavailable("Không thể lưu khảo sát vào cơ sở dữ liệu; vui lòng thử lại sau.")
            if input.license_plate and prepared.phone and record["consent_choice"] != "Tôi không đồng ý":
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"vehicle:{prepared.phone}",),
                )
                await cur.execute(
                    f"SELECT driver_phone FROM driver_registered_vehicles "
                    f"WHERE {phone_normalization_sql('driver_phone')}=%s OR driver_phone=%s FOR UPDATE",
                    (prepared.phone, prepared.phone_hash),
                )
                if await cur.fetchone() is None:
                    await cur.execute(
                        "INSERT INTO driver_registered_vehicles (driver_phone, license_plate, vehicle_type) "
                        "VALUES (%s, %s, %s) ON CONFLICT (driver_phone) DO NOTHING;",
                        (prepared.phone, input.license_plate.upper(), _normalize_vehicle_type(input.vehicle_type)),
                    )
            if durable:
                ids = await enqueue_submission_work(
                    conn, submission_id, schema=input.delivery_schema or input.schema,
                    context={"new_submission": True},
                )
            else:
                detected_plate = eval.ai_result.get("detected_plate")
                ids = await _legacy_event(cur, submission_id, "survey.validated" if eval.is_approved else "survey.rejected", {
                    "submission_id": submission_id,
                    "driver_tag": prepared.driver_tag,
                    "driver_phone_hash": prepared.phone_hash,
                    "driver_name": record["driver_name"],
                    "license_plate": input.license_plate or detected_plate,
                    "license_plate_hash": hash_plate(input.license_plate or detected_plate)
                    if input.license_plate or detected_plate else None,
                    "vehicle_type": input.vehicle_type or "CAR",
                    "gross_revenue": input.gross_revenue,
                    "trip_count": input.trip_count,
                    "status": eval.status,
                    "reason": eval.reason,
                    "ai_result": eval.ai_result,
                })
        if attempt.key:
            await commit_response(conn, attempt.scope, attempt.key, attempt.fingerprint, submission_id, response)
        started = time.monotonic()
        await conn.commit()
        logger.info("submission_commit submission_id=%s owner=%s elapsed_ms=%.3f", submission_id,
                    "outbox_v2" if durable else "legacy", (time.monotonic() - started) * 1000)
    return _commit(response, ids)


async def _recover_interruption(
    input: SubmissionInput, deps: SubmissionDependencies, prepared: _Prepared,
    submission_id: str, manifest: list[dict], snapshot: dict | None, *, durable: bool,
) -> SubmissionCommit | None:
    if input.is_test_mode or deps.db_pool is None:
        return None
    attempt = prepared.attempt
    try:
        async with deps.db_pool.connection() as conn:
            if attempt.key:
                state, prior = await reserve(conn, attempt.scope, attempt.key, attempt.fingerprint)
                if state != "new":
                    await conn.rollback()
                    return _commit(prior, replayed=True) if state == "replay" and prior is not None else None
            interruption = {
                "status": "MANUAL_REVIEW",
                "submission_id": submission_id,
                "mode": "TEST_DEV_MODE" if input.is_test_mode else "PROD_MODE",
                "reason": INTERRUPTED_CHECK_REASON,
                "driver_tag": prepared.driver_tag,
                "driver_phone": prepared.phone,
                "detected_plate": input.license_plate,
                "gross_revenue": input.gross_revenue,
                "net_income": None,
                "trip_count": input.trip_count,
                "ocr_data": None,
            }
            interruption.update(snapshot or {})
            interruption.update(status="MANUAL_REVIEW", submission_id=submission_id, reason=INTERRUPTED_CHECK_REASON)
            dissent = prepared.answers.get("consent_choice") == "Tôi không đồng ý"
            policy = delivery_policy_snapshot(input.delivery_schema or input.schema, dissent=dissent)
            form_data = {"status": "INTERRUPTED", "interrupted_at": datetime.now(timezone.utc).isoformat(),
                         "_delivery_policy": policy}
            if not dissent and policy["source"] == "question" and policy["question_id"] in prepared.form_data:
                form_data[policy["question_id"]] = prepared.form_data[policy["question_id"]]
            columns = {
                "submission_id": submission_id,
                "form_id": input.form_id,
                "ctv_code": str(input.collaborator_id or "").strip() or None,
                "driver_phone": None if dissent else prepared.phone,
                "driver_name": None if dissent else input.driver_name,
                "receipt_serial_no": f"SN-{input.license_plate or 'AUTO'}",
                "status": "MANUAL_REVIEW",
                "rejection_reason": INTERRUPTED_CHECK_REASON,
                "form_data": form_data,
                "created_at": input.received_at,
                "media_manifest": manifest,
                "media_version": 1,
            }
            if durable:
                columns["work_owner"] = "outbox_v2"
            async with conn.cursor() as cur:
                if not await _insert_row(cur, columns):
                    await conn.rollback()
                    return None
                if durable:
                    await enqueue_submission_work(
                        conn, submission_id, schema=input.delivery_schema or input.schema,
                        context={"new_submission": True},
                    )
                else:
                    await _legacy_event(cur, submission_id, "survey.interrupted", {
                        "submission_id": submission_id, "status": "MANUAL_REVIEW",
                    })
            if attempt.key:
                await commit_response(conn, attempt.scope, attempt.key, attempt.fingerprint, submission_id, interruption)
            await conn.commit()
        logger.warning("[SUBMIT] Persisted interruption row for #%s", submission_id)
        return _commit(interruption)
    except Exception as exc:
        logger.error("[SUBMIT] Interruption write failed for #%s: %s", submission_id, type(exc).__name__)
    return None


async def _load_active_image_exceptions(db_pool, form_id):
    """Active form-scoped image exceptions for the intra-submission tier."""
    if not db_pool or not form_id:
        return [], []
    try:
        async with db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT embedding::text AS embedding_text, image_dhash::text AS dhash_text "
                    "FROM image_dinov2_exceptions WHERE form_id = %s AND expires_at > NOW()",
                    (form_id,),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        logger.debug("[IMAGE EXCEPTION] lookup failed: %s", type(exc).__name__)
        return [], []
    embeddings = []
    dhashes = []
    for row in rows:
        if isinstance(row, dict):
            embedding_text, dhash_text = row.get("embedding_text"), row.get("dhash_text")
        else:
            embedding_text, dhash_text = row[0], row[1]
        if embedding_text:
            try:
                embeddings.append(json.loads(embedding_text))
            except (TypeError, ValueError):
                pass
        if dhash_text:
            try:
                dhashes.append(int(str(dhash_text), 2))
            except (TypeError, ValueError):
                pass
    return embeddings, dhashes


def _answer_summary_lines(schema: Any, answers: Mapping[str, Any], limit: int = 6) -> list[str]:
    """Form-driven answer lines for the admin notice; never assumes income/vehicle fields."""
    lines: list[str] = []
    for question in (getattr(schema, "questions", None) or []):
        qtype = str(getattr(getattr(question, "type", ""), "value", getattr(question, "type", "")) or "")
        if qtype in ("signature", "camera_webrtc"):
            continue
        answer = answers.get(getattr(question, "id", ""))
        if isinstance(answer, (list, tuple)):
            answer = ", ".join(str(item) for item in answer if str(item).strip())
        text = str(answer if answer is not None else "").strip()
        if not text:
            continue
        if len(text) > 120:
            text = text[:117] + "..."
        title = str(getattr(question, "title", "") or getattr(question, "id", ""))
        lines.append(f"• <b>{html.escape(title)}:</b> {html.escape(text)}")
        if len(lines) >= limit:
            break
    return lines


def _legacy_work(
    input: SubmissionInput, deps: SubmissionDependencies, prepared: _Prepared,
    eval: SubmissionEvaluation, record: dict,
) -> LegacySubmissionWork:
    submission_id = record["submission_id"]
    driver_name, driver_phone = input.driver_name, prepared.phone
    status_str, ai_res = eval.status, eval.ai_result
    status_emoji = "⏳" if status_str == "MANUAL_REVIEW" else ("✅" if status_str == "APPROVED" else "❌")
    auto_verdict = ai_res.get("status", "MANUAL_REVIEW")
    auto_reason = ai_res.get("reason", "")
    auto_emoji = "✅" if str(auto_verdict).upper() == "APPROVED" else "❌"
    tot_gross_val = record["total_gross_revenue"]
    driver_info_line = (
        f"• <b>Tài xế:</b> <b>{html.escape(driver_name)}</b> (<code>{driver_phone or 'N/A'}</code>)"
        if driver_name and driver_name not in ("Tài xế", "Tài xế Bí Mật", "N/A", "[SCRUBBED]")
        else f"• <b>Số điện thoại:</b> <code>{driver_phone or 'N/A'}</code>"
    )
    headline = "PHIẾU CHỜ DUYỆT THỦ CÔNG" if status_str == "MANUAL_REVIEW" else "PHIẾU KHẢO SÁT MỚI"
    verdict_lines = [f"{status_emoji} <b>{headline} #{submission_id}</b>", ""]
    form_line = submission_form_line(input.form_id)
    if form_line:
        verdict_lines.append(form_line)
    verdict_lines.append(driver_info_line)
    if input.collaborator_id:
        verdict_lines.append(f"• <b>CTV:</b> <code>{html.escape(str(input.collaborator_id))}</code>")
    verdict_lines.extend(_answer_summary_lines(input.delivery_schema or input.schema, prepared.form_data))
    if tot_gross_val:
        verdict_lines.append(f"• <b>Doanh thu:</b> <b>{tot_gross_val:,} VNĐ</b>")
    verdict_lines.append(
        f"• <b>Đề xuất từ Hệ thống AI:</b> {auto_emoji} <b>{auto_verdict}</b> "
        f"({html.escape(auto_reason) if auto_reason else 'Đạt chuẩn'})"
    )
    verdict_lines.append(f"• <b>Trạng thái:</b> <code>{status_str}</code>")
    verdict_text = "\n".join(verdict_lines)
    admin_markup = with_survey_copy({
        "inline_keyboard": [
            [
                {"text": "📋 Mở Phiếu Xét Duyệt", "callback_data": f"admin_review:{submission_id}"}
            ]
        ]
    }, submission_id)
    policy = record["form_data"]["_delivery_policy"]
    notify_source = policy["source"]
    recipient = prepared.answers.get(policy["question_id"]) if notify_source == "question" else input.collaborator_id
    if isinstance(recipient, list):
        recipient = recipient[0] if recipient else None
    party = None
    if notify_source != "none" and recipient:
        party = dict(
            submission_id=submission_id, status_str=status_str, driver_phone=driver_phone,
            driver_name=driver_name, collaborator_id=recipient, reason_str=eval.reason,
            gross_revenue=ai_res.get("gross_revenue") or input.gross_revenue, form_id=input.form_id,
            recipient_kind="driver" if notify_source == "question" else "ctv",
            ctv_ref=input.collaborator_id, submitted_at=input.received_at,
        )
    checklist = dict(
        submission_id=submission_id, db_pool=None if input.is_test_mode else deps.db_pool,
        is_approved=eval.is_approved, raw_bytes=prepared.raw_bytes[0] if prepared.raw_bytes else b"",
        driver_phone=record["driver_phone"] or driver_phone, driver_name=record["driver_name"] or driver_name,
        license_plate=input.license_plate or ai_res.get("detected_plate"), collab_param=input.collaborator_id,
        answers=prepared.form_data, ai_res=ai_res, tot_gross=tot_gross_val,
        tot_net=record["total_net_income"], tot_trips=record["total_trips"], reason_str=eval.reason,
        blur_var=eval.blur_score, is_test_mode=input.is_test_mode, raw_bytes_list=list(prepared.raw_bytes),
        form_id=input.form_id,
    )
    return LegacySubmissionWork(checklist, verdict_text, admin_markup, party)


async def _cached_response(input: SubmissionInput, deps: SubmissionDependencies, prepared: _Prepared) -> SubmissionCommit | None:
    if not prepared.attempt.key or (deps.db_pool and not input.is_test_mode):
        return None
    key = f"idemp:{prepared.attempt.scope}:{prepared.attempt.key}"
    payload = None
    if deps.redis is not None:
        try:
            payload = await deps.redis.get(key)
        except Exception as exc:
            logger.warning("Redis idempotency lookup error: %s", type(exc).__name__)
    if not payload and deps.legacy_cache is not None:
        payload = deps.legacy_cache.get(key)
    if not payload:
        return None
    try:
        cached = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(cached, dict) or not isinstance(cached.get("response"), dict) or not cached.get("fingerprint"):
        return None
    if cached["fingerprint"] != prepared.attempt.fingerprint:
        raise SubmissionConflict("Idempotency key was used with a different request payload.")
    return _commit(cached["response"], replayed=True)


async def _cache_response(input: SubmissionInput, deps: SubmissionDependencies, prepared: _Prepared, response: dict) -> None:
    if not prepared.attempt.key:
        return
    key = f"idemp:{prepared.attempt.scope}:{prepared.attempt.key}"
    serialized = json.dumps({"fingerprint": prepared.attempt.fingerprint, "response": response})
    if deps.legacy_cache is not None:
        deps.legacy_cache[key] = serialized
    if deps.redis is not None:
        try:
            await deps.redis.set(key, serialized, ex=86400)
        except Exception as exc:
            logger.warning("Redis idempotency caching error: %s", type(exc).__name__)


def _stage_photos(directory: str, prepared: _Prepared) -> list[dict]:
    # Never replace another submission's private evidence if an ID already exists.
    os.makedirs(directory, exist_ok=False)
    manifest = _store_submission_photos(directory, prepared.raw_bytes, prepared.upload_items)
    _fsync_directory(os.path.dirname(directory))
    return manifest


async def process_submission(input: SubmissionInput, dependencies: SubmissionDependencies) -> SubmissionCommit:
    deps = dependencies
    prepared = _prepare(input)
    declined = str(prepared.answers.get("consent_choice") or "").strip() == "Tôi không đồng ý"
    if input.schema:
        declined = declined or any(
            not _is_blank(prepared.answers.get(gate.id))
            and not gate_satisfied(gate, prepared.answers[gate.id])
            for gate in effective_gates(input.schema)
        )
    if declined:
        # A refusal is not an accepted submission: no evidence, PII, or work is retained.
        return _commit({
            "status": "REJECTED",
            "submission_id": f"{'DEV' if input.is_test_mode else 'VN'}-{secrets.token_hex(8).upper()}",
            "mode": "TEST_DEV_MODE" if input.is_test_mode else "PROD_MODE",
            "reason": "Biểu mẫu yêu cầu tài xế đồng ý điều khoản xử lý dữ liệu Nghị định 13 để tiếp tục!",
            "driver_tag": None, "driver_phone": None, "detected_plate": None,
            "gross_revenue": None, "net_income": None, "trip_count": None, "ocr_data": None,
        })
    durable = deps.durable_enabled and not input.is_test_mode
    if not await _writer_ready(deps, durable=durable):
        raise SubmissionUnavailable("Cơ sở dữ liệu chưa sẵn sàng; vui lòng thử lại sau.")
    cached = await _cached_response(input, deps, prepared)
    if cached is not None:
        return cached
    attempt = prepared.attempt
    if attempt.key and deps.db_pool and not input.is_test_mode:
        try:
            state, previous = await _lookup_attempt(deps.db_pool, attempt)
        except Exception as exc:
            logger.warning("[SUBMIT] attempt lookup failed: %s", type(exc).__name__)
            if deps.production or durable:
                raise SubmissionUnavailable("Không thể xác minh lượt gửi; vui lòng thử lại sau.") from exc
        else:
            prior = _attempt_outcome(state, previous)
            if prior is not None:
                return prior

    submission_id = f"{'DEV' if input.is_test_mode else 'VN'}-{secrets.token_hex(8).upper()}"
    directory = os.path.join(deps.media_root, submission_id)
    staging = asyncio.create_task(asyncio.to_thread(_stage_photos, directory, prepared))
    manifest = None
    response = None
    try:
        manifest = await asyncio.shield(staging)
        evaluation = await _evaluate(input, deps, prepared)
        response = _response(input, prepared, evaluation, submission_id)
        record, weeks = _record(input, prepared, evaluation, submission_id, manifest)
        if not await _writer_ready(deps, durable=durable):
            raise SubmissionUnavailable("Cơ sở dữ liệu chưa sẵn sàng; khảo sát chưa được lưu.")
        if input.is_test_mode:
            deps.append_dev_record(_dev_record(input, record, prepared, evaluation, weeks))
            committed = _commit(response)
        elif deps.db_pool:
            try:
                committed = await _persist(input, deps, prepared, evaluation, record, response, durable=durable)
            except (SubmissionConflict, SubmissionUnavailable, embedding_executor.InferenceBusyError):
                raise
            except Exception as exc:
                logger.error("[SUBMIT] DB write failed: %s", type(exc).__name__)
                if not (deps.production or durable):
                    raise
                if attempt.key:
                    try:
                        state, prior = await _lookup_attempt(deps.db_pool, attempt)
                        if state == "replay" and prior is not None:
                            return _commit(prior, replayed=True)
                    except Exception as lookup_exc:
                        logger.warning("[SUBMIT] uncertain commit lookup failed: %s", type(lookup_exc).__name__)
                raise SubmissionUnavailable("Không thể lưu khảo sát vào cơ sở dữ liệu; vui lòng thử lại sau.") from exc
        else:
            committed = _commit(response)
    except embedding_executor.InferenceBusyError:
        # Retain received evidence and the same attempt identity for a later retry.
        raise
    except SubmissionConflict:
        if manifest is not None:
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
        raise
    except SubmissionUnavailable:
        raise
    except asyncio.CancelledError:
        if manifest is None:
            try:
                manifest = await asyncio.shield(staging)
            except Exception:
                pass  # A failed write has no complete manifest to acknowledge.
        if manifest is not None:
            await asyncio.shield(_recover_interruption(input, deps, prepared, submission_id, manifest, response, durable=durable))
        raise
    except Exception:
        if manifest is not None:
            recovered = await asyncio.shield(_recover_interruption(
                input, deps, prepared, submission_id, manifest, response, durable=durable,
            ))
            if recovered is not None:
                return recovered
        raise

    if committed.replayed:
        if committed.submission_id != submission_id:
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
        return committed
    if not durable:
        if deps.schedule_legacy is not None:
            try:
                deps.schedule_legacy(_legacy_work(input, deps, prepared, evaluation, record))
            except Exception as exc:
                logger.error("[SUBMIT] Legacy scheduling failed: %s", type(exc).__name__)
        await _cache_response(input, deps, prepared, response)
    return committed
