# -*- coding: utf-8 -*-
"""
Survey Ingress & Verification Router.
Handles public dynamic form rendering, CTV referral resolution,
live WebRTC photo pre-checks, AI compliance audits, anti-fraud screening,
and driver appeal workflows.
"""

import os
import math
import httpx
import html
import re
import json
import time
import logging
import asyncio
from typing import Optional, Dict, Any, Union, List, Tuple
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Request, BackgroundTasks, Depends, File, UploadFile, Form, HTTPException, status
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from config import settings
from core.state import get_db_pool, get_redis_client, get_http_client, FORM_REGISTRY, DEV_STORAGE_FILE, vision_client, refresh_form_registry_from_disk, verify_admin_auth, verify_staff_request
from core.cache import BoundedTTLCache
from core.quotas import is_form_response_limit_reached
from core.ctv import (
    resolve_ctv_identifier,
    extract_ctv_short_code,
    get_ctv_options_for_form,
    get_assigned_ctvs_for_form,
    is_ctv_assigned_to_form,
    resolve_ctv_from_slug,
)
from core.phone_links import (
    get_notification_telegram_target,
    normalize_vn_phone,
    phone_normalization_sql,
)
from core.submission_ids import normalize_submission_id
from core.repositories import append_dev_submission
from services.privacy_crypto import hash_phone
from services.normalizer import sanitize_and_normalize_image, compute_laplacian_blur_score, detect_image_manipulation
from services.action_checklist import dispatch_action_checklist
from services.telegram_ui import submission_form_line, with_survey_copy
from services.dynamic_form_html import render_dynamic_form_html, render_ctv_selector_landing_html
from services.form_engine import get_effective_image_checks

import sys
from core.embedding_executor import InferenceBusyError
from services.submission_pipeline import (
    LegacySubmissionWork,
    SubmissionConflict,
    SubmissionDependencies,
    SubmissionInput,
    SubmissionUnavailable,
    SubmissionUpload,
    process_submission,
)

logger = logging.getLogger("routers.survey")
router = APIRouter(tags=["Survey Ingress"])
SUBMISSION_PHOTOS_DIR = os.path.join(os.getcwd(), "data", "submission_photos")
os.makedirs(SUBMISSION_PHOTOS_DIR, exist_ok=True)

# Rate limiters & in-memory caches
HISTORY_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)
LOCAL_IDEMP_CACHE = BoundedTTLCache(max_size=2048, ttl_seconds=86400.0)
PRE_CHECK_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)
ACTIVE_APPEALS_RATE_LIMITER = BoundedTTLCache(max_size=1024, ttl_seconds=7200.0)
STATUS_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)

def _is_private_form(schema: Any) -> bool:
    return bool(
        schema
        and getattr(schema, "settings", None)
        and getattr(schema.settings, "access_control", "public") != "public"
    )


def _private_form_forbidden_html(request: Request) -> HTMLResponse:
    next_url = request.url.path + (("?" + request.url.query) if request.url.query else "")
    login_href = "/login?next=" + quote(next_url, safe="")
    return HTMLResponse(
        content=(
            "<!doctype html><html lang='vi'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Biểu mẫu riêng tư</title>"
            "<body style='font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;line-height:1.6'>"
            "<h1>Biểu mẫu riêng tư</h1>"
            "<p>Khảo sát này chỉ dành cho quản trị viên/nhân sự nội bộ. Vui lòng đăng nhập tài khoản quản trị để truy cập.</p>"
            f"<p><a href='{html.escape(login_href, quote=True)}'>Đăng nhập quản trị</a></p>"
            "</body></html>"
        ),
        status_code=403,
        headers={"Cache-Control": "no-store"},
    )


def _response_limit_reached_html(schema: Any, approved_count: int, response_limit: Optional[int]) -> HTMLResponse:
    title_text = html.escape(getattr(schema, "title", None) or "Khảo sát")
    if response_limit and response_limit > 0:
        detail = (
            f"Khảo sát đã đạt giới hạn {response_limit} phản hồi được duyệt "
            f"({approved_count}/{response_limit}) và tạm dừng tiếp nhận thêm phiếu."
        )
    else:
        detail = "Khảo sát hiện đã đóng (đạt giới hạn) và ngừng tiếp nhận phản hồi mới."
    return HTMLResponse(
        content=(
            "<!doctype html><html lang='vi'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title_text}</title>"
            "<body style='font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;line-height:1.6'>"
            "<h1>Khảo sát đã đủ số lượng phản hồi</h1>"
            f"<p><strong>{title_text}</strong></p>"
            f"<p>{detail}</p>"
            "</body></html>"
        ),
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/camera-form", response_class=HTMLResponse)
@router.get("/forms/{form_id}", response_class=HTMLResponse)
async def get_dynamic_form(request: Request, form_id: str = "default"):
    """Renders dynamic forms with conditional logic, signature modal, and GPS authorization."""
    refresh_form_registry_from_disk()
    host = (request.headers.get("host") or "").lower()
    if any(host.startswith(p) for p in ("audit.", "logs.", "dev-audit.", "audit-dev.", "audit.dev.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/audit", status_code=status.HTTP_302_FOUND)
    if any(host.startswith(p) for p in ("admin.", "dev-admin.", "admin-dev.", "admin.dev.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/forms", status_code=status.HTTP_302_FOUND)
    if any(host.startswith(p) for p in ("view.", "data.", "dev-data.", "data-dev.", "data.dev.")) and form_id in ("default", "camera-form"):
        return RedirectResponse(url="/admin/submissions", status_code=status.HTTP_302_FOUND)
    # 1. Primary: Check ?ref= query parameter (also supports ?ctv=, ?collaborator_id=)
    ref_param = (
        request.query_params.get("ref")
        or request.query_params.get("ctv")
        or request.query_params.get("collaborator_id")
    )

    matched_ctv = None
    active_slug = None
    active_ref = None
    if ref_param and ref_param.strip():
        matched_ctv = resolve_ctv_identifier(ref_param.strip())
        active_ref = extract_ctv_short_code(ref_param.strip())
        active_slug = f"ref-{active_ref}"
    else:
        # 2. Secondary fallback: check deterministic slug in URL path e.g. /forms/income-survey-59443eedf25522
        matched_ctv = resolve_ctv_from_slug(form_id)
        if matched_ctv:
            active_slug = form_id
            active_ref = extract_ctv_short_code(matched_ctv)
    # 3. Resolve schema (a matched CTV must not override an explicit form id)
    effective_form_id = form_id if form_id in FORM_REGISTRY else "income_survey"
    schema = FORM_REGISTRY.get(effective_form_id) or next(iter(FORM_REGISTRY.values()))

    # 3a. Private forms require authenticated staff/admin access.
    if _is_private_form(schema):
        if not await verify_staff_request(request):
            return _private_form_forbidden_html(request)

    # 3b. Campaign-wide response limit (counts APPROVED submissions only).
    db_pool = get_db_pool()
    try:
        limit_reached, approved_count, response_limit = await is_form_response_limit_reached(
            db_pool,
            effective_form_id,
            schema,
            strict=(db_pool is not None or settings.ENVIRONMENT == "production"),
        )
    except Exception:
        logger.exception("Response limit check failed for form %s", effective_form_id)
        return HTMLResponse(
            content="<!doctype html><html lang='vi'><meta charset='utf-8'><body><h1>Tạm thời không thể kiểm tra trạng thái khảo sát</h1><p>Vui lòng thử lại sau.</p></body></html>",
            status_code=503,
        )
    if limit_reached:
        return _response_limit_reached_html(schema, approved_count, response_limit)

    # Per-form switch: when disabled, ignore ?ref= and skip the CTV selector
    ctv_ref_enabled = bool(getattr(schema.settings, "ctv_ref_enabled", True))
    if not ctv_ref_enabled:
        ref_param = None
        matched_ctv = None
        active_slug = None
        active_ref = None

    # ctv_ref_enabled is the per-form switch: when on, a direct link without ?ref=
    # must ask for the CTV first, otherwise submissions carry no collaborator and
    # approval verdicts have nowhere to go.
    has_ctv_tracking = ctv_ref_enabled

    # 4. Enforce per-CTV survey assignments when CTV tracking is enabled.
    if has_ctv_tracking:
        if matched_ctv:
            if db_pool is not None:
                try:
                    assigned = await is_ctv_assigned_to_form(db_pool, matched_ctv, effective_form_id)
                except Exception:
                    logger.exception("CTV assignment check failed for form %s", effective_form_id)
                    return HTMLResponse(
                        content="<!doctype html><html lang='vi'><meta charset='utf-8'><body><h1>Tạm thời không thể xác nhận phân công khảo sát</h1><p>Vui lòng thử lại sau.</p></body></html>",
                        status_code=503,
                    )
                if not assigned:
                    return HTMLResponse(
                        content="<!doctype html><html lang='vi'><meta charset='utf-8'><body><h1>Không có quyền truy cập khảo sát</h1><p>Mã CTV này chưa được phân công khảo sát hoặc quyền truy cập đã bị thu hồi.</p></body></html>",
                        status_code=403,
                    )
            elif settings.ENVIRONMENT == "production":
                return HTMLResponse(
                    content="<!doctype html><html lang='vi'><meta charset='utf-8'><body><h1>Tạm thời không thể xác nhận phân công khảo sát</h1><p>Vui lòng thử lại sau.</p></body></html>",
                    status_code=503,
                )
        else:
            if db_pool is not None:
                try:
                    ctv_options = await get_assigned_ctvs_for_form(db_pool, effective_form_id)
                except Exception:
                    logger.exception("Assigned CTV lookup failed for form %s", effective_form_id)
                    return HTMLResponse(
                        content="<!doctype html><html lang='vi'><meta charset='utf-8'><body><h1>Tạm thời không thể tải danh sách CTV</h1><p>Vui lòng thử lại sau.</p></body></html>",
                        status_code=503,
                    )
            elif settings.ENVIRONMENT == "production":
                return HTMLResponse(
                    content="<!doctype html><html lang='vi'><meta charset='utf-8'><body><h1>Tạm thời không thể tải danh sách CTV</h1><p>Vui lòng thử lại sau.</p></body></html>",
                    status_code=503,
                )
            else:
                ctv_options = get_ctv_options_for_form(schema)
            return HTMLResponse(
                content=render_ctv_selector_landing_html(
                    schema=schema,
                    ctv_options=ctv_options,
                    form_id=effective_form_id,
                    query_params=dict(request.query_params),
                ),
                status_code=200,
            )
    # 5. Render individual form with locked CTV code
    return HTMLResponse(
        content=render_dynamic_form_html(schema, ctv_code=matched_ctv, ctv_slug=active_slug, ref_code=active_ref),
        status_code=200
    )

@router.get("/api/v1/forms")
async def list_public_forms_api():
    """Lists all active public survey forms for the catalog."""
    refresh_form_registry_from_disk()
    items = []
    for fid, s in FORM_REGISTRY.items():
        if not s.settings or s.settings.access_control == "public":
            items.append({
                "form_id": fid,
                "title": s.title,
                "description": s.description or "",
                "question_count": len(s.questions),
                "is_test_mode": bool(s.settings and s.settings.is_test_mode),
            })
    return {"status": "success", "count": len(items), "forms": items}


@router.get("/api/v1/forms/{form_id}")
async def get_form_schema_api(request: Request, form_id: str):
    """Public schema endpoint for dynamic form rendering (staff-only for private forms)."""
    refresh_form_registry_from_disk()
    schema = FORM_REGISTRY.get(form_id)
    if not schema:
        raise HTTPException(status_code=404, detail="Biểu mẫu không tồn tại")
    if _is_private_form(schema):
        if not await verify_staff_request(request):
            raise HTTPException(
                status_code=403,
                detail="Biểu mẫu riêng tư yêu cầu đăng nhập quản trị viên.",
            )
    return schema.model_dump()
@router.get("/api/v1/surveys/history")
async def get_survey_history(
    request: Request,
    phone: str,
    admin_user: str = Depends(verify_admin_auth),
):
    """Admin-only phone history; Telegram verification never grants web access."""
    if not phone:
        raise HTTPException(status_code=400, detail="Vui lòng cung cấp số điện thoại hợp lệ.")

    clean_phone = re.sub(r"[\s\-\.\(\)]", "", phone.strip())
    if not clean_phone or not re.match(r"^\+?[0-9]{8,15}$", clean_phone):
        raise HTTPException(status_code=400, detail="Số điện thoại không hợp lệ.")

    rate_limited = False
    principal_key = f"{admin_user}:{request.client.host if request.client else 'unknown'}"
    redis_client = get_redis_client()
    if redis_client:
        try:
            rkey = f"rate_limit:history:{principal_key}"
            count = await redis_client.incr(rkey)
            if count == 1:
                await redis_client.expire(rkey, 60)
            if count > 30:
                rate_limited = True
        except Exception as e:
            logger.warning(f"Redis rate limiting error: {e}")
            req_count = HISTORY_RATE_LIMITER.incr(principal_key)
            if req_count > 30:
                rate_limited = True
    else:
        if HISTORY_RATE_LIMITER.incr(principal_key) > 30:
            rate_limited = True

    if rate_limited:
        raise HTTPException(status_code=429, detail="Quá nhiều yêu cầu tra cứu lịch sử. Vui lòng thử lại sau!")

    phone = normalize_vn_phone(clean_phone) or clean_phone
    results = []
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if normalize_vn_phone(r.get("driver_phone")) == phone:
                    results.append({
                        "submission_id": r.get("submission_id"),
                        "mode": "DEV_TEST",
                        "status": r.get("status"),
                        "reason": r.get("reason"),
                        "submitted_at": r.get("submitted_at")
                    })
        except Exception as e:
            logger.warning(f"Error reading dev history: {e}")

    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        SELECT submission_id, status, rejection_reason, created_at
                        FROM survey_submissions AS s
                        WHERE {phone_normalization_sql("s.driver_phone")} = %s
                        ORDER BY created_at DESC
                        LIMIT 20;
                        """,
                        (phone,)
                    )
                    rows = await cur.fetchall()
                    for row in rows:
                        results.append({
                            "submission_id": row[0],
                            "mode": "PRODUCTION",
                            "status": row[1],
                            "reason": row[2] or "Hợp lệ",
                            "submitted_at": row[3].isoformat() if row[3] else None
                        })
        except Exception as e:
            logger.warning(f"Error reading DB history: {e}")

    return {"phone": phone, "count": len(results), "attempts": results}

def _clear_active_appeal_after_verdict(submission_id: str, status_str: str) -> None:
    """Best-effort closure of a stale in-memory appeal lock after an AI verdict.

    The DB status is the source of truth; this just removes the bot-process lock
    immediately so a terminal verdict does not leave an "active check" behind.
    """
    if str(status_str or "").upper() not in ("APPROVED", "REJECTED"):
        return
    try:
        from routers import webhooks
        runner = webhooks.get_driver_bot_runner()
        if runner is None:
            return
        active = getattr(runner, "active_appeals", None)
        if not active:
            return
        canonical = normalize_submission_id(submission_id)
        for uid, sid in list(active.items()):
            if normalize_submission_id(sid) == canonical:
                active.pop(uid, None)
                logger.info(
                    f"Cleared active appeal check for #{canonical} after AI verdict "
                    f"{str(status_str).upper()} (source=ai)"
                )
    except Exception as e:
        logger.debug(f"Could not clear active appeal after verdict for #{submission_id}: {e}")


def _read_dev_submission_status(clean_id: str):
    for fpath in (
        os.path.join(os.getcwd(), "data", "dev_test_submissions.json"),
        os.path.join(os.getcwd(), "data", "dev_submissions.json"),
    ):
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                records = json.load(f)
            for r in records if isinstance(records, list) else []:
                if normalize_submission_id(r.get("submission_id")) != clean_id:
                    continue
                # dev_test_submissions.json also holds action-checklist stubs
                # ({submission_id, action_status}); they are not submission records.
                if not (r.get("status") or r.get("reason") or r.get("rejection_reason") or r.get("submitted_at")):
                    continue
                return {
                    "submission_id": clean_id,
                    "status": (r.get("status") or "MANUAL_REVIEW").upper(),
                    "reason": r.get("reason") or r.get("rejection_reason") or "",
                    "updated_at": r.get("moderated_at") or r.get("updated_at") or r.get("submitted_at"),
                    "is_test_mode": True,
                }
        except Exception as e:
            logger.warning(f"Error reading dev submission status from {fpath}: {e}")
    return None


@router.get("/api/v1/survey/status/{submission_id}")
async def get_submission_status(request: Request, submission_id: str):
    """Public, PII-free approval status lookup for a submitted survey."""
    client_ip = request.client.host if request.client else "unknown"
    if STATUS_RATE_LIMITER.incr(client_ip) > 60:
        raise HTTPException(status_code=429, detail="Quá nhiều yêu cầu tra cứu trạng thái. Vui lòng thử lại sau!")

    clean_id = normalize_submission_id(submission_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", clean_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu khảo sát.")

    is_dev_id = clean_id.upper().startswith(("DEV-", "TEST-"))
    if is_dev_id:
        dev_status = _read_dev_submission_status(clean_id)
        if dev_status:
            return dev_status
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu khảo sát.")

    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT status, rejection_reason, COALESCE(moderated_at, created_at)
                        FROM survey_submissions
                        WHERE UPPER(submission_id) = UPPER(%s);
                        """,
                        (clean_id,),
                    )
                    row = await cur.fetchone()
            if row:
                return {
                    "submission_id": clean_id,
                    "status": (row[0] or "MANUAL_REVIEW").upper(),
                    "reason": row[1] or "",
                    "updated_at": row[2].isoformat() if hasattr(row[2], "isoformat") else (row[2] or None),
                    "is_test_mode": False,
                }
        except Exception as e:
            logger.warning(f"Error reading DB submission status: {e}")

    # No pool configured (or DB miss): local dev storage still holds test records.
    dev_status = _read_dev_submission_status(clean_id)
    if dev_status:
        return dev_status

    raise HTTPException(status_code=404, detail="Không tìm thấy phiếu khảo sát.")
PRE_CHECK_RATE_LIMITER = BoundedTTLCache(max_size=2048, ttl_seconds=60.0)

@router.post("/api/v1/survey/pre-check")
async def pre_check_image_slot(
    request: Request,
    photo: UploadFile = File(...),
    slot_index: int = Form(0),
    role: Optional[str] = Form(""),
    form_id: Optional[str] = Form("income_survey"),
    question_id: Optional[str] = Form("camera_receipt"),
):
    """Lightweight instant pre-check on photo capture: blur variance, format, and quick role check."""
    refresh_form_registry_from_disk()
    client_ip = request.client.host if request.client else "127.0.0.1"
    rate_key = f"precheck:{client_ip}"
    
    # Rate limiting: max 20 pre-checks per minute per client IP
    redis_client = get_redis_client()
    if redis_client:
        try:
            cnt = await redis_client.incr(rate_key)
            if cnt == 1:
                await redis_client.expire(rate_key, 60)
            if cnt > 20:
                return JSONResponse(status_code=429, content={"status": "RATE_LIMITED", "detail": "Pre-check rate limit exceeded. Please wait a moment."})
        except Exception:
            pass
    else:
        if PRE_CHECK_RATE_LIMITER.incr(client_ip) > 20:
            return JSONResponse(status_code=429, content={"status": "RATE_LIMITED", "detail": "Pre-check rate limit exceeded."})

    try:
        raw_bytes = await photo.read()
        if not raw_bytes:
            return {"status": "ERROR", "message": "Ảnh rỗng hoặc không đọc được."}

        # 1. Format & Normalization
        norm_bytes, pil_im = sanitize_and_normalize_image(raw_bytes, max_dimension=settings.IMAGE_MAX_DIMENSION)
        
        # 2. Blur / Sharpness Check (~20ms)
        schema = FORM_REGISTRY.get(form_id)
        checks = get_effective_image_checks(schema, question_id)
        blur_thresh = checks.blur_threshold if checks.blur_threshold is not None else settings.BLUR_THRESHOLD
        sharp, b_var = compute_laplacian_blur_score(pil_im, threshold=blur_thresh)
        
        if not sharp:
            return {
                "status": "BLURRY",
                "is_sharp": False,
                "blur_score": round(b_var, 1),
                "threshold": blur_thresh,
                "message": f"Ảnh hơi mờ (độ nét {b_var:.1f} < ngưỡng {blur_thresh:.0f}). Bạn nên giữ chắc tay chụp lại!"
            }

        # 3. Fast Metadata Forensics Check (Photoshop/Canva)
        is_tampered, tamper_reason = detect_image_manipulation(raw_bytes, pil_im)
        if is_tampered:
            return {
                "status": "TAMPERED",
                "is_sharp": True,
                "message": f"Cảnh báo: Phát hiện dấu hiệu chỉnh sửa ảnh ({tamper_reason})."
            }

        return {
            "status": "PASS",
            "is_sharp": True,
            "blur_score": round(b_var, 1),
            "message": "Ảnh đạt chuẩn độ nét và chất lượng!"
        }
    except Exception as e:
        logger.warning(f"Pre-check error: {e}")
        return {"status": "PASS", "is_sharp": True, "message": "Đã ghi nhận ảnh."}


def _build_retry_form_link(
    form_id: Optional[str],
    ctv_ref: Optional[str],
    collaborator_id: Optional[Union[str, int]],
    recipient_kind: str = "ctv",
) -> str:
    """Retry/submit-again link that keeps the original submission's CTV ref."""
    from core.ctv import extract_ctv_short_code, build_ctv_form_url

    ref_source = ctv_ref or (collaborator_id if recipient_kind != "driver" else None)
    ref = extract_ctv_short_code(str(ref_source)) if ref_source else ""
    return build_ctv_form_url(ref, str(form_id or ""))


_UPLOAD_FILE_KEY_RE = re.compile(r"^(?P<qid>.+)__file_(?P<slot>\d+)$")
_LEGACY_PHOTO_KEY_RE = re.compile(r"^survey_photo(?:_(?P<slot>\d+))?$")


class UploadGroupError(ValueError):
    """Raised when an uploaded file key cannot be mapped to a question slot."""


def _group_uploaded_files(form_data: Any, schema: Any, explicit_question_id: Optional[str]) -> List[Dict[str, Any]]:
    """Group uploaded file parts by camera question and slot, preserving legacy names."""
    camera_questions = [
        q for q in (getattr(schema, "questions", None) or [])
        if getattr(q, "type", None) == "camera_webrtc"
    ] if schema else []
    by_id = {q.id: q for q in camera_questions}
    explicit = (explicit_question_id or "").strip() or None
    if explicit and explicit not in by_id:
        raise UploadGroupError(f"question_id '{explicit}' không phải câu hỏi camera hợp lệ.")

    groups: Dict[str, Dict[int, Any]] = {}
    next_slot: Dict[str, int] = {}

    def _slot_limit(qid: str) -> int:
        question = by_id.get(qid)
        return len(getattr(question, "image_slots", None) or []) if question else 0

    def _put(qid: str, slot: int, upload: Any) -> None:
        limit = _slot_limit(qid)
        if limit and slot >= limit:
            raise UploadGroupError(f"Ảnh cho câu hỏi '{qid}' tham chiếu slot {slot} vượt quá {limit} slot.")
        if not limit and slot != 0:
            raise UploadGroupError(f"Ảnh cho câu hỏi '{qid}' tham chiếu slot {slot}, nhưng câu hỏi không khai báo image_slots.")
        groups.setdefault(qid, {})[slot] = upload
        next_slot[qid] = max(next_slot.get(qid, 0), slot + 1)

    for key, upload in form_data.multi_items():
        if not hasattr(upload, "read"):
            continue
        key = str(key)
        match = _UPLOAD_FILE_KEY_RE.match(key)
        if match:
            qid = match.group("qid")
            slot = int(match.group("slot"))
            if qid not in by_id:
                raise UploadGroupError(f"Tệp '{key}' tham chiếu câu hỏi không tồn tại: '{qid}'.")
            if explicit and qid != explicit:
                raise UploadGroupError(
                    f"Tệp '{key}' thuộc câu hỏi '{qid}', không khớp question_id '{explicit}'."
                )
            _put(qid, slot, upload)
            continue

        legacy = _LEGACY_PHOTO_KEY_RE.match(key)
        if legacy or explicit or len(camera_questions) == 1:
            if explicit:
                qid = explicit
            elif len(camera_questions) == 1:
                qid = camera_questions[0].id
            elif camera_questions:
                qid = camera_questions[0].id
            else:
                qid = "__legacy__"
            if legacy and legacy.group("slot") is not None:
                slot = int(legacy.group("slot"))
            else:
                slot = next_slot.get(qid, 0)
            if qid == "__legacy__":
                groups.setdefault(qid, {})[slot] = upload
                next_slot[qid] = slot + 1
            else:
                _put(qid, slot, upload)
            continue

        raise UploadGroupError(
            f"Không xác định được câu hỏi cho tệp '{key}'. Dùng dạng '<question_id>__file_<slot>'."
        )

    ordered: List[Dict[str, Any]] = []
    for question in camera_questions:
        if explicit and question.id != explicit:
            continue
        for slot in sorted(groups.get(question.id, {})):
            upload = groups[question.id][slot]
            ordered.append({
                "question": question,
                "question_id": question.id,
                "slot_index": slot,
                "upload": upload,
                "filename": getattr(upload, "filename", "") or "",
            })
    for slot in sorted(groups.get("__legacy__", {})):
        upload = groups["__legacy__"][slot]
        ordered.append({
            "question": None,
            "question_id": "__legacy__",
            "slot_index": slot,
            "upload": upload,
            "filename": getattr(upload, "filename", "") or "",
        })
    for flat_index, item in enumerate(ordered):
        item["flat_index"] = flat_index
    return ordered


def _has_valid_gps(lat_raw: Any, lon_raw: Any) -> bool:
    """Validates finite, in-range, non-(0,0) GPS coordinates."""
    if lat_raw is None or lon_raw is None:
        return False
    lat_s = str(lat_raw).strip()
    lon_s = str(lon_raw).strip()
    if not lat_s or not lon_s:
        return False
    try:
        lat = float(lat_s)
        lon = float(lon_s)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    return True


def _authorize_test_flags(schema: Any, is_test_mode: bool, bypass_img_check: bool) -> None:
    """Server-side authority for client test/bypass flags.

    Non-production environments may use labelled DEV/test storage. Production
    only permits these flags when the server-side form schema itself is a test
    form; a client flag never authorizes a normal production form.
    """
    wants_test = bool(is_test_mode)
    wants_bypass = bool(bypass_img_check)
    if not wants_test and not wants_bypass:
        return
    if wants_bypass and not wants_test:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bypass_img_check requires an authorized test mode",
        )
    server_test_form = bool(
        schema
        and getattr(schema, "settings", None)
        and getattr(schema.settings, "is_test_mode", False)
    )
    if settings.ENVIRONMENT == "production" and not server_test_form:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="test/bypass flags are not authorized for this production form",
        )


def _schedule_legacy_submission(background_tasks: BackgroundTasks, work: LegacySubmissionWork) -> None:
    try:
        background_tasks.add_task(dispatch_action_checklist, **work.checklist)
    except Exception as exc:
        logger.error("Failed to schedule submission checklist: %s", exc)
    if settings.TELEGRAM_ADMIN_GROUP_ID:
        try:
            background_tasks.add_task(
                send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, work.admin_text, work.admin_markup,
            )
        except Exception as exc:
            logger.error("Failed to schedule Telegram admin notification: %s", exc)
    if work.party_notification is not None:
        try:
            dispatch = getattr(
                sys.modules.get("main"), "dispatch_submission_telegram_notifications",
                dispatch_submission_telegram_notifications,
            )
            background_tasks.add_task(dispatch, **work.party_notification)
        except Exception as exc:
            logger.error("Failed to schedule Telegram party notification: %s", exc)


@router.post("/api/v1/survey/submit-webrtc")
async def submit_webrtc_survey(
    request: Request,
    background_tasks: BackgroundTasks,
    survey_photo: Optional[UploadFile] = File(None),
    driver_phone: Optional[str] = Form(None),
    license_plate: Optional[str] = Form(None),
    driver_name: Optional[str] = Form(None),
    collaborator_id: Optional[str] = Form(None),
    vehicle_type: Optional[str] = Form("CAR"),
    gross_revenue: Optional[str] = Form("0"),
    trip_count: Optional[str] = Form("0"),
    odometer_km: Optional[str] = Form("0"),
    signature_png: Optional[str] = Form(None),
    is_test_mode: Optional[bool] = Form(False),
    idempotency_key: Optional[str] = Form(None),
    bypass_img_check: Optional[bool] = Form(False),
    form_id: Optional[str] = Form(None),
    question_id: Optional[str] = Form(None),
):
    """Normalize multipart transport and invoke the submission use case."""
    refresh_form_registry_from_disk()
    requested_form = (form_id or "").strip()
    active_form = requested_form or "income_survey"
    schema = FORM_REGISTRY.get(requested_form) if requested_form else None
    effective_schema = schema or FORM_REGISTRY.get(active_form)
    if _is_private_form(effective_schema):
        if not await verify_staff_request(request):
            return JSONResponse(
                status_code=403,
                content={"detail": "Biểu mẫu riêng tư yêu cầu đăng nhập nhân sự quản trị."},
            )
    _authorize_test_flags(schema, bool(is_test_mode), bool(bypass_img_check))
    received_at = datetime.now(timezone.utc)
    if requested_form and schema is None:
        logger.warning("[SUBMIT] form_id '%s' is not registered; schema validation skipped.", requested_form)
    db_pool = get_db_pool()
    if effective_schema is not None:
        try:
            limit_reached, _, _ = await is_form_response_limit_reached(
                db_pool,
                active_form,
                effective_schema,
                strict=(db_pool is not None or settings.ENVIRONMENT == "production"),
            )
        except Exception:
            logger.exception("Response limit check failed on submit for form %s", active_form)
            return JSONResponse(
                status_code=503,
                content={"detail": "Tạm thời không thể kiểm tra giới hạn số lượng phiếu của khảo sát."},
            )
        if limit_reached:
            return JSONResponse(
                status_code=409,
                content={"detail": "Khảo sát đã đạt giới hạn số lượng phiếu được duyệt."},
            )
    form = await request.form()
    ctv_ref = (
        collaborator_id
        or form.get("collaborator_id")
        or form.get("ctv_code")
        or form.get("ctv_id")
        or form.get("collaborator_tg_id")
        or request.query_params.get("collaborator_id")
        or request.query_params.get("ctv")
    )
    # Enforce live CTV assignment at submission time (revoking an assignment blocks immediately).
    ctv_ref_enabled = bool(getattr(schema.settings, "ctv_ref_enabled", True)) if schema else False
    if ctv_ref_enabled and (db_pool is not None or settings.ENVIRONMENT == "production"):
        if db_pool is None:
            return JSONResponse(status_code=503, content={"detail": "Cơ sở dữ liệu chưa sẵn sàng để xác nhận phân công CTV."})
        resolved_ctv = resolve_ctv_identifier(str(ctv_ref).strip()) if ctv_ref and str(ctv_ref).strip() else None
        try:
            assigned = bool(resolved_ctv) and await is_ctv_assigned_to_form(db_pool, resolved_ctv, active_form)
        except Exception:
            logger.exception("CTV assignment verification failed on submit for form %s", active_form)
            return JSONResponse(status_code=503, content={"detail": "Tạm thời không thể xác nhận phân công khảo sát."})
        if not assigned:
            return JSONResponse(
                status_code=403,
                content={"detail": "CTV chưa được phân công khảo sát này hoặc quyền truy cập đã bị thu hồi."},
            )
    if not bool(is_test_mode):
        if not _has_valid_gps(form.get("gps_latitude"), form.get("gps_longitude")):
            return JSONResponse(
                status_code=400,
                content={"detail": "Bắt buộc bật định vị GPS để gửi khảo sát."},
            )
    explicit_question = (question_id or "").strip() or None
    uploads = []
    upload_error = None
    try:
        items = _group_uploaded_files(form, schema, explicit_question)
    except UploadGroupError as exc:
        upload_error = str(exc)
        logger.warning("[SUBMIT] Upload grouping failed: %s", exc)
    else:
        for item in items:
            upload = item["upload"]
            await upload.seek(0)
            content = await upload.read()
            uploads.append(SubmissionUpload(item["question_id"], item["slot_index"], item["filename"], content))

    submission = SubmissionInput(
        form_id=active_form, schema=schema, delivery_schema=FORM_REGISTRY.get(active_form),
        fields={k: v for k, v in form.multi_items() if isinstance(v, str)},
        uploads=tuple(uploads), upload_error=upload_error, question_id=explicit_question,
        driver_phone=driver_phone, driver_name=driver_name, license_plate=license_plate,
        collaborator_id=str(ctv_ref) if ctv_ref else None, vehicle_type=vehicle_type,
        gross_revenue=gross_revenue, trip_count=trip_count, signature_png=signature_png,
        is_test_mode=bool(is_test_mode), bypass_img_check=bool(bypass_img_check),
        idempotency_key=idempotency_key, received_at=received_at,
    )
    dependencies = SubmissionDependencies(
        vision=vision_client, db_pool=get_db_pool(), redis=get_redis_client(),
        legacy_cache=LOCAL_IDEMP_CACHE, media_root=SUBMISSION_PHOTOS_DIR,
        dev_storage_file=DEV_STORAGE_FILE, append_dev_record=append_dev_submission,
        image_max_dimension=settings.IMAGE_MAX_DIMENSION, blur_threshold=settings.BLUR_THRESHOLD,
        normalize_image=sanitize_and_normalize_image,
        production=settings.ENVIRONMENT == "production", durable_enabled=settings.DURABLE_WEBRTC_ENABLED,
        schedule_legacy=lambda work: _schedule_legacy_submission(background_tasks, work),
    )
    try:
        committed = await process_submission(submission, dependencies)
    except SubmissionConflict as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})
    except SubmissionUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except InferenceBusyError:
        return JSONResponse(
            status_code=503, headers={"Retry-After": "1"},
            content={"detail": "Hệ thống kiểm tra ảnh đang bận; vui lòng thử lại sau."},
        )
    if not committed.replayed:
        _clear_active_appeal_after_verdict(committed.submission_id, committed.status)
    return JSONResponse(status_code=200, content=committed.response)

LOCAL_APPEAL_TRACKER: Dict[str, List[float]] = {}

class AppealRequest(BaseModel):
    submission_id: str
    driver_phone: Optional[str] = "ANONYMOUS"
    reason: Optional[str] = "Yêu cầu người thật kiểm tra lại trên web form"

@router.post("/api/v1/survey/appeal")
async def handle_survey_appeal(req: AppealRequest, background_tasks: BackgroundTasks):
    """Web appeal quota: 20 attempts per hour, separate from Telegram quotas."""
    phone = (req.driver_phone or "ANONYMOUS").strip()
    sub_id = normalize_submission_id(req.submission_id)

    if not sub_id:
        return JSONResponse(status_code=404, content={
            "status": "NOT_FOUND",
            "message": "Không tìm thấy phiếu khảo sát.",
        })

    # 1. Validate the submission and check only a genuinely open appeal/check.
    current_status = None
    durable_owner = False
    db_pool = get_db_pool()
    if db_pool is None and settings.ENVIRONMENT == "production":
        return JSONResponse(status_code=503, content={"detail": "Database required for appeal"})
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT status, to_jsonb(s)->>'work_owner' FROM survey_submissions s "
                        "WHERE UPPER(submission_id) = UPPER(%s) LIMIT 1;",
                        (sub_id,),
                    )
                    row = await cur.fetchone()
                    if row:
                        current_status = str(row[0] or "").upper()
                        durable_owner = len(row) > 1 and row[1] == "outbox_v2"
        except Exception as e:
            logger.warning(f"Appeal status lookup failed for #{sub_id}: {e}")
            if settings.ENVIRONMENT == "production" or settings.DURABLE_APPEALS_ENABLED:
                return JSONResponse(status_code=503, content={"detail": "Appeal state unavailable"})

    if current_status is None and os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records if isinstance(dev_records, list) else []:
                if normalize_submission_id(r.get("submission_id")) != sub_id:
                    continue
                if not (r.get("status") or r.get("reason") or r.get("rejection_reason") or r.get("submitted_at")):
                    continue
                current_status = str(r.get("status") or "MANUAL_REVIEW").upper()
                break
        except Exception as e:
            logger.warning(f"Dev appeal status lookup failed for #{sub_id}: {e}")

    if current_status is None:
        return JSONResponse(status_code=404, content={
            "status": "NOT_FOUND",
            "submission_id": sub_id,
            "message": f"Không tìm thấy phiếu #{sub_id}.",
        })

    if current_status == "APPEAL_PENDING":
        return JSONResponse(status_code=200, content={
            "status": "ALREADY_ACTIVE",
            "submission_id": sub_id,
            "message": f"Phiếu #{sub_id} đã có yêu cầu kiểm tra đang hoạt động! Bạn không thể gửi thêm yêu cầu mới khi yêu cầu cũ chưa hoàn tất."
        })
    if current_status == "APPROVED":
        return JSONResponse(status_code=200, content={
            "status": "APPROVED_LOCKED",
            "submission_id": sub_id,
            "message": f"Phiếu #{sub_id} đã được duyệt và không thể mở lại để phúc khảo."
        })
    if current_status == "MANUAL_REVIEW":
        return JSONResponse(status_code=200, content={
            "status": "UNDER_REVIEW",
            "submission_id": sub_id,
            "message": f"Phiếu #{sub_id} đang được Ban Quản Trị kiểm tra. Vui lòng chờ kết quả."
        })
    if current_status != "REJECTED":
        return JSONResponse(status_code=200, content={
            "status": "NOT_APPEALABLE",
            "submission_id": sub_id,
            "message": f"Phiếu #{sub_id} chưa thể phúc khảo ở trạng thái {current_status}."
        })

    rate_key = f"rate_limit:appeal:{phone}"
    now = time.time()
    window = 3600  # 1 hour window
    max_appeals = 20  # Relaxed quota per window
    remaining_appeals = max_appeals - 1

    rate_limited = False
    redis_client = get_redis_client()
    if redis_client:
        try:
            count = await redis_client.incr(rate_key)
            if count == 1:
                await redis_client.expire(rate_key, window)
            if count > max_appeals:
                rate_limited = True
            remaining_appeals = max(0, max_appeals - count)
        except Exception as e:
            logger.warning(f"Redis appeal rate limit error: {e}")
            history = [t for t in LOCAL_APPEAL_TRACKER.get(phone, []) if now - t < window]
            if len(history) >= max_appeals:
                rate_limited = True
            else:
                history.append(now)
                LOCAL_APPEAL_TRACKER[phone] = history
                remaining_appeals = max(0, max_appeals - len(history))
    else:
        history = [t for t in LOCAL_APPEAL_TRACKER.get(phone, []) if now - t < window]
        if len(history) >= max_appeals:
            rate_limited = True
        else:
            history.append(now)
            LOCAL_APPEAL_TRACKER[phone] = history
            remaining_appeals = max(0, max_appeals - len(history))

    if rate_limited:
        return JSONResponse(status_code=200, content={
            "status": "RATE_LIMITED",
            "remaining_appeals": 0,
            "message": f"Bạn đã gửi tối đa {max_appeals} lượt yêu cầu kiểm tra trong vòng 1 giờ này. Vui lòng thử lại sau!"
        })

    # 2. Update status in Database
    appeal_note = f"Tài xế yêu cầu người thật đối soát lại: {req.reason}"
    if durable_owner or settings.DURABLE_APPEALS_ENABLED:
        if not db_pool:
            return JSONResponse(status_code=503, content={"detail": "Database required for appeal"})
        from core.durable_work import transition_submission
        try:
            async with db_pool.connection() as conn:
                changed, state = await transition_submission(conn, sub_id, "APPEAL_PENDING", appeal_note,
                    actor="driver:" + hash_phone(phone), expected_statuses={"REJECTED"})
                if not changed:
                    return JSONResponse(status_code=200, content={"status": "ALREADY_ACTIVE" if state == "APPEAL_PENDING" else "NOT_APPEALABLE", "submission_id": sub_id})
                await conn.commit()
        except Exception:
            return JSONResponse(status_code=503, content={"detail": "Appeal could not be committed"})
        return JSONResponse(status_code=200, content={"status": "APPEAL_SUBMITTED", "submission_id": sub_id,
            "remaining_appeals": remaining_appeals, "message": f"Đã gửi yêu cầu kiểm tra lại thành công! Bạn còn {remaining_appeals}/20 lượt trong 1 giờ."})
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = 'APPEAL_PENDING', rejection_reason = %s, moderated_at = NOW() WHERE UPPER(submission_id) = UPPER(%s);",
                        (appeal_note, sub_id)
                    )
                    await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update survey_submissions for appeal: {e}")

    # 3. Update status in Dev Test Storage
    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if normalize_submission_id(r.get("submission_id")) == sub_id:
                    r["status"] = "APPEAL_PENDING"
                    r["reason"] = appeal_note
                    r["rejection_reason"] = appeal_note
                    r["moderated_at"] = datetime.now(timezone.utc).isoformat()
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(dev_records, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to update dev_storage for appeal: {e}")

    # 4. Dispatch Alert to Telegram Admin Group
    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        alert_text = (
            f"🚨 <b>YÊU CẦU NGƯỜI THẬT KIỂM TRA LẠI (KHIẾU NẠI) #{sub_id}</b>\n\n"
            f"• <b>Số điện thoại:</b> <code>{phone}</code>\n"
            f"• <b>Lượt khiếu nại còn lại:</b> <b>{remaining_appeals}/20 lượt</b> (trong 1h)\n"
            f"• <b>Lý do:</b> {html.escape(req.reason)}\n\n"
            f"👉 <i>Vui lòng vào <a href='https://survey.trustbridgevn.com/admin/submissions'>Quản trị phiếu</a> để đối soát ảnh gốc!</i>"
        )
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, alert_text)
        except Exception as e:
            logger.warning(f"Failed to alert admin group: {e}")

    return JSONResponse(status_code=200, content={
        "status": "APPEAL_SUBMITTED",
        "submission_id": sub_id,
        "remaining_appeals": remaining_appeals,
        "message": f"Đã gửi yêu cầu kiểm tra lại thành công! Bạn còn {remaining_appeals}/20 lượt khiếu nại trong 1 giờ này."
    })


@router.post("/api/v1/survey/appeal/cancel")
async def cancel_survey_appeal(req: AppealRequest, background_tasks: BackgroundTasks):
    """Allows cancelling an active manual review / appeal request."""
    sub_id = normalize_submission_id(req.submission_id)
    db_pool = get_db_pool()
    durable_cancel = settings.DURABLE_APPEALS_ENABLED
    if db_pool is None and (settings.ENVIRONMENT == "production" or durable_cancel):
        return JSONResponse(status_code=503, content={"detail": "Database required for cancellation"})
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                state_cur = await conn.execute("SELECT status,to_jsonb(s)->>'work_owner' FROM survey_submissions s WHERE UPPER(submission_id)=UPPER(%s) FOR UPDATE", (sub_id,))
                state_row = await state_cur.fetchone()
                durable_cancel = durable_cancel or bool(state_row and len(state_row) > 1 and state_row[1] == "outbox_v2")
                if state_row and durable_cancel:
                    from core.durable_work import transition_submission
                    changed, state = await transition_submission(conn, sub_id, "REJECTED", "Yêu cầu kiểm tra lại đã bị hủy bởi người dùng",
                        actor="driver:" + hash_phone(req.driver_phone or "ANONYMOUS"), expected_statuses={"APPEAL_PENDING"})
                    if not changed:
                        return JSONResponse(status_code=200, content={"status": "NOT_APPEALABLE", "submission_id": sub_id, "current_status": state})
                    await conn.commit()
                    _clear_active_appeal_after_verdict(sub_id, "REJECTED")
                    return JSONResponse(status_code=200, content={"status": "CANCELLED", "submission_id": sub_id,
                        "message": f"Đã hủy yêu cầu kiểm tra lại cho phiếu #{sub_id} thành công!"})
                if state_row and state_row[0] != "APPEAL_PENDING":
                    return JSONResponse(status_code=200, content={"status": "NOT_APPEALABLE", "submission_id": sub_id, "current_status": state_row[0]})
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE survey_submissions SET status = 'REJECTED', rejection_reason = 'Yêu cầu kiểm tra lại đã bị hủy bởi người dùng', moderated_at = NOW() WHERE UPPER(submission_id) = UPPER(%s) AND status='APPEAL_PENDING';",
                        (sub_id,)
                    )
                    await conn.commit()
        except Exception as e:
            logger.warning(f"Failed to cancel appeal in DB: {e}")
            if durable_cancel or settings.ENVIRONMENT == "production":
                return JSONResponse(status_code=503, content={"detail": "Cancellation could not be committed"})

    if os.path.exists(DEV_STORAGE_FILE):
        try:
            with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
                dev_records = json.load(f)
            for r in dev_records:
                if normalize_submission_id(r.get("submission_id")) == sub_id:
                    r["status"] = "REJECTED"
                    r["reason"] = "Yêu cầu kiểm tra lại đã bị hủy bởi người dùng"
                    r["moderated_at"] = datetime.now(timezone.utc).isoformat()
            with open(DEV_STORAGE_FILE, "w", encoding="utf-8") as f:
                json.dump(dev_records, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to cancel appeal in dev storage: {e}")

    _clear_active_appeal_after_verdict(sub_id, "REJECTED")

    if getattr(settings, "TELEGRAM_ADMIN_GROUP_ID", None):
        alert_text = f"ℹ️ <b>Người dùng đã HỦY yêu cầu kiểm tra lại cho phiếu #{sub_id}.</b>"
        try:
            background_tasks.add_task(send_telegram_notification, settings.TELEGRAM_ADMIN_GROUP_ID, alert_text)
        except Exception:
            pass

    return JSONResponse(status_code=200, content={
        "status": "CANCELLED",
        "submission_id": sub_id,
        "message": f"Đã hủy yêu cầu kiểm tra lại cho phiếu #{sub_id} thành công!"
    })

async def send_telegram_notification(chat_id: Union[int, str], text: str, reply_markup: Optional[dict] = None):
    """Dispatches standardized HTML verdict message to admin group or collaborator on Telegram."""
    bot_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    if not bot_token or bot_token == "mock_tg_token":
        logger.info(f"[MOCK TELEGRAM DISPATCH] to {chat_id}:\n{text}")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        client = get_http_client() or httpx.AsyncClient(timeout=10.0)
        resp = await client.post(url, json=payload)
        if resp.status_code == 429:
            logger.warning("telegram 429 hit; backing off")
    except Exception as e:
        logger.error(f"failed to push telegram message: {e}")


async def lookup_telegram_user_by_phone(phone: str) -> Optional[int]:
    """Looks up telegram_user_id in authorized_users by driver_phone."""
    clean = re.sub(r"[^\d+]", "", phone.strip())
    if clean.startswith("+84"):
        clean = "0" + clean[3:]
    elif clean.startswith("84") and len(clean) == 11:
        clean = "0" + clean[2:]

    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users WHERE (driver_phone = %s OR driver_phone = %s) AND is_active = TRUE LIMIT 1;",
                        (clean, phone),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception as e:
            logger.debug(f"DB lookup failed for phone {phone}: {e}")
    return None


async def lookup_telegram_user_for_collaborator(collab_id: Union[str, int]) -> Optional[int]:
    """Resolves a collaborator code / full CTV id to a telegram_user_id.

    ctv_directory is the source of truth; falls back to the legacy
    authorized_users username/phone match for codes not yet migrated.
    """
    from core.ctv import extract_ctv_short_code, normalize_ctv_code

    collab_str = str(collab_id).strip()
    code = normalize_ctv_code(collab_str)
    db_pool = get_db_pool()
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id, phone FROM ctv_directory "
                        "WHERE (UPPER(code) = UPPER(%s) OR UPPER(code) = UPPER(%s)) "
                        "AND is_active = TRUE LIMIT 1;",
                        (collab_str, code),
                    )
                    row = await cur.fetchone()
                    if row:
                        phone, linked_tg_id = row[1], row[0]
                        if linked_tg_id:
                            return int(linked_tg_id)
                        if phone:
                            return await lookup_telegram_user_by_phone(str(phone))
        except Exception as e:
            logger.debug(f"ctv_directory lookup failed for collaborator {collab_str}: {e}")

    candidates = {collab_str.lower(), extract_ctv_short_code(collab_str).lower()}
    if db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users "
                        "WHERE is_active = TRUE AND 'COLLABORATOR' = ANY(roles) "
                        "AND (LOWER(username) = ANY(%s) OR LOWER(driver_phone) = ANY(%s)) "
                        "LIMIT 1;",
                        (list(candidates), list(candidates)),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception as e:
            logger.debug(f"DB lookup failed for collaborator {collab_str}: {e}")
    # A bare number is not a chat id: phone-shaped refs must never be sent to
    # Telegram as chat_id=961281318 (which yields "chat not found"). Only accept
    # a numeric collaborator when it is a known active Telegram user.
    if collab_str.isdigit() and db_pool:
        try:
            async with db_pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT telegram_user_id FROM authorized_users "
                        "WHERE telegram_user_id = %s AND is_active = TRUE AND 'COLLABORATOR' = ANY(roles) LIMIT 1;",
                        (int(collab_str),),
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return int(row[0])
        except Exception as e:
            logger.debug(f"Telegram-id lookup failed for {collab_str}: {e}")
    return None


async def send_telegram_direct_message(
    bot_token: str,
    chat_id: Union[int, str],
    text: str,
    parse_mode: str = "HTML",
    reply_markup: Optional[dict] = None,
) -> bool:
    """Dispatches a direct message using the specified bot token (Driver Bot or Admin Bot)."""
    if not bot_token or bot_token == "mock_tg_token":
        logger.info(f"[MOCK DIRECT MESSAGE DISPATCH] to {chat_id}:\n{text}")
        return True

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async def _post() -> httpx.Response:
        client = get_http_client()
        if client is not None:
            return await client.post(url, json=payload)
        async with httpx.AsyncClient(timeout=10.0) as temp_client:
            return await temp_client.post(url, json=payload)

    try:
        resp = await _post()
        if resp.status_code == 429:
            try:
                retry_after = int((resp.json() or {}).get("parameters", {}).get("retry_after", 1))
            except Exception:
                retry_after = 1
            retry_after = max(1, min(retry_after, 5))
            logger.warning(f"Telegram 429 for chat {chat_id}; retrying after {retry_after}s")
            await asyncio.sleep(retry_after)
            resp = await _post()
        if resp.status_code != 200:
            logger.error(
                f"Telegram send to chat {chat_id} failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
            return False
        try:
            body = resp.json()
        except Exception:
            body = {}
        message_id = (body.get("result") or {}).get("message_id") if isinstance(body, dict) else None
        logger.info(f"Telegram send to chat {chat_id} ok: message_id={message_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to dispatch Telegram message to {chat_id}: {e}")
        return False


def render_driver_submission_verdict_message(
    submission_id: str,
    status_str: str,
    driver_name: Optional[str] = None,
    reason_str: Optional[str] = None,
    gross_revenue: Optional[Union[str, int]] = None,
) -> Tuple[str, Optional[dict]]:
    """Renders user-friendly, supportive verdict notifications for drivers/collaborators."""
    name_display = f" {driver_name}" if driver_name else ""
    if status_str == "APPROVED":
        rev_text = ""
        if gross_revenue and str(gross_revenue) not in ("0", ""):
            try:
                num = int(re.sub(r"[^\d]", "", str(gross_revenue)))
                rev_text = f"• <b>Doanh thu ghi nhận:</b> <code>{num:,}đ</code>\n"
            except Exception:
                pass
        text = (
            f"🎉 <b>Khảo sát #{submission_id} đã được DUYỆT THÀNH CÔNG!</b>\n\n"
            f"Chào bạn{name_display}, kết quả khảo sát của bạn đã hợp lệ và đạt chuẩn quy định.\n"
            f"{rev_text}"
            f"• <b>Trạng thái:</b> <b>HỢP LỆ ✅</b>\n\n"
            f"Cảm ơn bạn đã tham gia. Tiền thưởng đang được đưa vào hàng đợi chi trả tự động!"
        )
        return text, with_survey_copy(None, submission_id)

    elif status_str == "REJECTED":
        text = (
            f"⚠️ <b>Thông báo kết quả khảo sát #{submission_id}:</b>\n\n"
            f"Chào bạn{name_display}, phiếu khảo sát của bạn chưa đạt yêu cầu.\n"
            f"• <b>Trạng thái:</b> <b>CHƯA ĐẠT ❌</b>\n"
            f"• <b>Lý do:</b> <i>{reason_str or 'Ảnh chụp hoặc số liệu chưa hợp lệ'}</i>\n\n"
            f"👉 <i>Bạn có thể bấm nút bên dưới để gửi lại ảnh chụp rõ nét hơn hoặc gửi yêu cầu phúc khảo:</i>"
        )
        reply_markup = with_survey_copy({
            "inline_keyboard": [
                [
                    {"text": f"📸 Gửi lại ảnh #{submission_id}", "callback_data": f"driver_fix:{submission_id}:slot_photo"},
                    {"text": f"⚖️ Phúc khảo #{submission_id}", "callback_data": f"driver_appeal:{submission_id}"},
                ],
                [
                    {"text": "🆘 Yêu Cầu Hỗ Trợ", "callback_data": f"driver_support:{submission_id}"},
                ]
            ]
        }, submission_id)
        return text, reply_markup

    else:
        text = (
            f"⏳ <b>Khảo sát #{submission_id} đang được kiểm tra:</b>\n\n"
            f"Chào bạn{name_display}, hồ sơ của bạn đang được chuyển tới Giám Sát Viên duyệt bổ sung.\n"
            f"• <b>Trạng thái:</b> <b>ĐANG XỬ LÝ ⏳</b>\n"
            f"• <b>Ghi chú:</b> <i>{reason_str or 'Hệ thống đang đối soát dữ liệu'}</i>\n\n"
            f"Kết quả cuối cùng sẽ được thông báo trực tiếp tại đây ngay khi hoàn tất."
        )
        return text, with_survey_copy(None, submission_id)


def _verification_reply_markup() -> dict:
    """Reply keyboard used when an unverified phone receives a verdict."""
    return {
        "keyboard": [
            [{"text": "📱 Xác Thực Số Điện Thoại", "request_contact": True}],
            [{"text": "📋 Khảo Sát Của Tôi"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


async def dispatch_submission_telegram_notifications(
    submission_id: str,
    status_str: str,
    driver_phone: Optional[str] = None,
    driver_name: Optional[str] = None,
    collaborator_id: Optional[Union[str, int]] = None,
    reason_str: Optional[str] = None,
    gross_revenue: Optional[Union[str, int]] = None,
    form_id: Optional[str] = None,
    recipient_kind: str = "ctv",
    ctv_ref: Optional[str] = None,
    submitted_at: Optional[Union[str, datetime]] = None,
):
    """
    Notifies the configured Telegram recipient for a submission: the owning CTV
    (default) or the driver identified by the configured phone question.
    """
    driver_bot_token = getattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", None) or settings.TELEGRAM_BOT_TOKEN
    if not driver_bot_token or driver_bot_token == "mock_tg_token":
        logger.info(f"[MOCK TG NOTIFY CTV] sub={submission_id} status={status_str} collab={collaborator_id}")
        return

    import sys
    main_mod = sys.modules.get("main")
    _send_direct = getattr(main_mod, "send_telegram_direct_message", send_telegram_direct_message) if main_mod else send_telegram_direct_message
    _lookup_collab = getattr(main_mod, "lookup_telegram_user_for_collaborator", lookup_telegram_user_for_collaborator) if main_mod else lookup_telegram_user_for_collaborator

    if collaborator_id is None or str(collaborator_id).strip() == "":
        logger.info(f"Submission #{submission_id} has no Telegram recipient; nothing to notify.")
        return

    if recipient_kind == "driver":
        # A question's phone answer authorizes delivery only through a verified
        # standalone binding. Never interpret its digits as a chat id or fall
        # back to a driver/CTV record or an arbitrary @username.
        normalized_phone = normalize_vn_phone(collaborator_id)
        if not normalized_phone:
            logger.warning(
                "TG question source for #%s: invalid phone answer; verdict delivery skipped.",
                submission_id,
            )
            return
        try:
            original_time = submitted_at if isinstance(submitted_at, datetime) else datetime.fromisoformat(str(submitted_at or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            original_time = None
        if original_time is None or original_time.utcoffset() is None:
            logger.warning("TG question source for #%s: original submission time unknown; phone verdict skipped.", submission_id)
            return
        collab_tg_id, recipient_verified = (
            await get_notification_telegram_target(normalized_phone, submitted_at=original_time) or (None, False)
        )
        if not collab_tg_id:
            logger.warning(
                "TG question source for #%s: no phone_telegram_links mapping; "
                "valid for the original submission time; party verdict skipped (no driver/CTV fallback). Admin notification is independent.",
                submission_id,
            )
            return
        if recipient_verified:
            logger.info("TG question source for #%s resolved through a verified phone link.", submission_id)
        else:
            logger.info("TG question source for #%s resolved through an unverified phone link; sending notice with verification prompt.", submission_id)
    else:
        collab_tg_id = await _lookup_collab(collaborator_id)
        if not collab_tg_id:
            logger.info(f"Collaborator '{collaborator_id}' not linked to Telegram (open the CTV_Info Telegram Bot invitation and verify own contact).")
            return
        recipient_verified = True

    status_emoji = "✅" if status_str == "APPROVED" else ("⚠️" if status_str == "MANUAL_REVIEW" else "❌")
    clean_phone = driver_phone or "N/A"
    masked_phone = clean_phone[:3] + "****" + clean_phone[-3:] if len(clean_phone) >= 7 else clean_phone
    form_line = submission_form_line(form_id)
    collab_msg = (
        f"📋 <b>Cập Nhật Khảo Sát #{html.escape(str(submission_id))}</b>\n\n"
        + (f"{form_line}\n" if form_line else "")
        + f"• <b>Đối tác / Khách hàng:</b> {html.escape(str(driver_name or 'Tài xế'))} (<code>{html.escape(str(masked_phone))}</code>)\n"
        f"• <b>Trạng thái:</b> {status_emoji} <b>{html.escape(str(status_str))}</b>\n"
        f"• <b>Ghi chú:</b> {html.escape(str(reason_str or 'Hợp lệ'))}"
    )
    if status_str == "REJECTED" and form_id:
        link = _build_retry_form_link(form_id, ctv_ref, collaborator_id, recipient_kind)
        collab_msg += f"\n\n🔁 <b>Nộp lại / mở biểu mẫu:</b> {link}"
    footer = "Tài xế" if recipient_kind == "driver" else "Cộng tác viên phụ trách"
    collab_msg += f"\n\n<i>Thông báo tự động dành cho {footer}.</i>"
    reply_markup = None
    if not recipient_verified:
        collab_msg += (
            "\n\nℹ️ <b>Đây là hệ thống xác minh khảo sát thu nhập của Trust Bridge.</b>\n"
            f"Thông báo này được gửi vì số <code>{html.escape(str(masked_phone))}</code> đã được nhập vào phiếu "
            f"<code>{html.escape(str(submission_id))}</code>.\n"
            "Nếu số này <b>không phải của bạn</b>, bạn không cần làm gì; hệ thống sẽ không yêu cầu bạn xác thực "
            "hay thực hiện thêm bước nào.\n"
            "Nếu đây là số của bạn và bạn muốn xem hoặc quản lý phiếu, hãy xác thực bằng nút bên dưới."
        )
        reply_markup = _verification_reply_markup()
    if reply_markup is None:
        reply_markup = with_survey_copy(None, submission_id)
    sent = await _send_direct(
        bot_token=driver_bot_token,
        chat_id=collab_tg_id,
        text=collab_msg,
        reply_markup=reply_markup,
    )
    if not sent:
        # The CTV/ driver may only have started the admin bot; try it as a fallback.
        admin_token = getattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", None)
        if admin_token and admin_token != driver_bot_token and admin_token != "mock_tg_token":
            logger.info(f"Retrying TG verdict for #{submission_id} via admin bot fallback.")
            sent = await _send_direct(
                bot_token=admin_token,
                chat_id=collab_tg_id,
                text=collab_msg,
                reply_markup=reply_markup,
            )
    if sent:
        logger.info(
            "TG verdict sent for #%s to %s telegram_id=%s",
            submission_id, recipient_kind, collab_tg_id,
        )
    else:
        logger.error(
            "TG verdict send FAILED for #%s to %s telegram_id=%s; user may not have started the bot.",
            submission_id, recipient_kind, collab_tg_id,
        )

