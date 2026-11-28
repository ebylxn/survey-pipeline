"""Transaction-owned durable intent creation. Provider I/O belongs to workers."""

import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row

from config import settings
from services.telegram_ui import submission_form_line, with_survey_copy
from core.phone_links import (
    CONFLICTED_PHONE_LINK_SOURCE,
    VERIFIED_PHONE_LINK_SOURCES,
    normalize_vn_phone,
)


def _dict(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


async def audit(conn, action, entity_id, actor, details):
    actor_type = "SYSTEM" if str(actor) in {"system", "scheduler", "recovery"} else "DRIVER" if str(actor).startswith("driver:") else "ADMIN"
    identity = str(actor)
    if len(identity) > 64:
        identity = hashlib.sha256(identity.encode()).hexdigest()
    await conn.execute(
        "INSERT INTO system_audit_log(component_name,action,entity_type,entity_id,actor_type,actor_id,details) "
        "VALUES ('durable_work',%s,'SURVEY',%s,%s,%s,%s::jsonb)",
        (action, str(entity_id), actor_type, identity, _json(details)),
    )


async def require_delivery_schema(conn):
    from core.schema_gate import MIGRATION_001_CHECKSUM

    migration = Path(__file__).resolve().parents[1] / "scripts/migrations/002_outbox_delivery.sql"
    expected = {"001": MIGRATION_001_CHECKSUM, "002": hashlib.sha256(migration.read_bytes()).hexdigest()}
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT version,checksum FROM schema_migrations WHERE version IN ('001','002')")
        if {row["version"]: row["checksum"] for row in await cur.fetchall()} != expected:
            raise ValueError("DELIVERY_SCHEMA_REQUIRED")
        await cur.execute("SELECT action_key,result,lease_until,attempts FROM outbox_events LIMIT 0")
        await cur.execute("SELECT work_owner,decision_version,media_manifest,media_version FROM survey_submissions LIMIT 0")


async def enqueue_action(conn, aggregate_id, target, event_type, payload, *, aggregate_kind="submission", action_key=None):
    await require_delivery_schema(conn)
    if action_key is None:
        material = [aggregate_kind, str(aggregate_id), target, event_type, payload]
        action_key = hashlib.sha256(_json(material).encode()).hexdigest()
    cur = await conn.execute(
        "INSERT INTO outbox_events(aggregate_kind,aggregate_id,flow_target,event_type,payload,action_key) "
        "VALUES (%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT "
        "(aggregate_kind,aggregate_id,event_type,flow_target,action_key) WHERE action_key IS NOT NULL "
        "DO UPDATE SET action_key=outbox_events.action_key RETURNING id",
        (aggregate_kind, str(aggregate_id), target, event_type, _json(payload), action_key),
    )
    row = await cur.fetchone()
    return str(row["id"] if isinstance(row, dict) else row[0])


async def _row(conn, sid):
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM survey_submissions WHERE UPPER(submission_id)=UPPER(%s) FOR UPDATE", (sid,))
        return await cur.fetchone()


def delivery_policy_snapshot(schema, *, dissent=False):
    form = getattr(schema, "settings", None)
    return {"source": "none" if dissent else (getattr(form, "tg_response_source", None) or "ctv").strip().lower(),
            "question_id": None if dissent else getattr(form, "tg_response_question_id", None),
            "recorded_at_submission": True,
            "version": hashlib.sha256((schema.model_dump_json() if schema else "default").encode()).hexdigest()}


async def _recipient(conn, row, policy):
    if policy["source"] == "none":
        return None, "NOTIFICATIONS_DISABLED"
    if policy["source"] == "question":
        answer = _dict(row.get("form_data")).get(policy.get("question_id"))
        phone = normalize_vn_phone(answer[0] if isinstance(answer, list) and answer else answer)
        if not phone or not row.get("created_at"):
            return None, "UNLINKED_RECIPIENT"
        cur = await conn.execute(
            "SELECT telegram_user_id, updated_at, source FROM phone_telegram_links "
            "WHERE phone_normalized=%s AND telegram_user_id>0 "
            "AND COALESCE(source, '') <> %s AND updated_at<=%s "
            "ORDER BY (source IN ('verified_contact','admin_verified')) DESC, updated_at DESC LIMIT 1",
            (phone, CONFLICTED_PHONE_LINK_SOURCE, row["created_at"]),
        )
    else:
        ctv_code = str(row.get("ctv_code") or "").strip()
        if ctv_code:
            exists = await conn.execute("SELECT to_regclass('ctv_directory')")
            found = await exists.fetchone()
            if not (found[0] if not isinstance(found, dict) else next(iter(found.values()))):
                return None, "UNLINKED_RECIPIENT"
            from core.ctv import extract_ctv_short_code, normalize_ctv_code
            candidates = [ctv_code, normalize_ctv_code(ctv_code), extract_ctv_short_code(ctv_code)]
            cur = await conn.execute("SELECT telegram_user_id FROM ctv_directory WHERE UPPER(code)=ANY(%s) AND is_active", ([value.upper() for value in candidates if value],))
        else:
            collaborator_id = row.get("collaborator_id")
            raw_id = str(collaborator_id).strip() if collaborator_id is not None else ""
            if not raw_id or not (raw_id.isascii() and raw_id.isdigit()):
                return None, "UNLINKED_RECIPIENT"
            tg_id = int(raw_id)
            if tg_id <= 0 or tg_id > (1 << 63) - 1:
                return None, "UNLINKED_RECIPIENT"
            cur = await conn.execute(
                "SELECT telegram_user_id FROM authorized_users WHERE telegram_user_id=%s AND is_active AND 'COLLABORATOR' = ANY(roles)",
                (tg_id,),
            )
    value = await cur.fetchone()
    if value:
        if isinstance(value, dict):
            ident = value.get("telegram_user_id")
            epoch = value.get("updated_at")
            source = value.get("source")
        else:
            ident = value[0]
            epoch = value[1] if len(value) > 1 else None
            source = value[2] if len(value) > 2 else None
    else:
        ident = epoch = source = None
    if ident and policy["source"] == "question":
        row["_recipient_binding"] = {
            "phone": phone,
            "telegram_user_id": int(ident),
            "verified_at": epoch.isoformat(),
            "submitted_at": row["created_at"].isoformat(),
            "verified": str(source or "") in VERIFIED_PHONE_LINK_SOURCES,
        }
    return (int(ident), None) if ident and int(ident) > 0 else (None, "UNLINKED_RECIPIENT")


async def _billing_collaborator(conn, row):
    ident = row.get("collaborator_id")
    if ident is None and row.get("ctv_code"):
        cur = await conn.execute("SELECT to_regclass('ctv_directory')")
        exists = await cur.fetchone()
        if exists and (next(iter(exists.values())) if isinstance(exists, dict) else exists[0]):
            cur = await conn.execute("SELECT telegram_user_id FROM ctv_directory WHERE UPPER(code)=UPPER(%s) AND is_active", (row["ctv_code"],))
            found = await cur.fetchone()
            ident = (next(iter(found.values())) if isinstance(found, dict) else found[0]) if found else None
    if ident is None:
        return None
    from core.ctv_onboarding import billing_collaborator_is_eligible

    if not await billing_collaborator_is_eligible(
        conn, ident, ctv_code=row.get("ctv_code"), submitted_at=row.get("created_at"),
    ):
        return None
    await conn.execute("UPDATE survey_submissions SET collaborator_id=%s WHERE submission_id=%s", (ident, row["submission_id"]))
    return ident


async def enqueue_submission_work(conn, submission_id, transition="SUBMITTED", *, schema=None, context=None, transfer=False):
    row = await _row(conn, submission_id)
    if not row:
        raise ValueError("SUBMISSION_NOT_FOUND")
    if row.get("work_owner") != "outbox_v2" and not transfer:
        raise ValueError("LEGACY_RECONCILIATION_REQUIRED")
    context = context or {}
    selected = context.get("verified_missing_actions")
    if transfer and row.get("work_owner") != "outbox_v2" and context.get("new_submission") is not True:
        if not isinstance(selected, list) or not selected or any(not isinstance(item, str) for item in selected):
            raise ValueError("LEGACY_RECONCILIATION_REQUIRED")
    if transfer and row.get("work_owner") != "outbox_v2" and not row.get("media_manifest") and context.get("new_submission") is not True:
        import asyncio
        from services.submission_media import legacy_manifest

        row["media_manifest"] = await asyncio.to_thread(legacy_manifest, row["submission_id"], int(row["media_version"] or 1))
        await conn.execute("UPDATE survey_submissions SET media_manifest=%s::jsonb WHERE submission_id=%s",
                           (_json(row["media_manifest"]), row["submission_id"]))
    answers = _dict(row.get("form_data"))
    policy = _dict(answers.get("_delivery_policy"))
    if not policy:
        if schema is None:
            from core.state import FORM_REGISTRY
            schema = FORM_REGISTRY.get(row.get("form_id"))
        policy = delivery_policy_snapshot(schema)
        policy.pop("recorded_at_submission")
        if transfer:
            policy["inferred"] = True
        answers["_delivery_policy"] = policy
    await conn.execute("UPDATE survey_submissions SET work_owner='outbox_v2',form_data=%s::jsonb WHERE submission_id=%s", (_json(answers), row["submission_id"]))
    row["work_owner"], row["form_data"] = "outbox_v2", answers
    if answers.get("_test_data") is True or answers.get("is_test_mode") is True:
        return []
    base = {"decision_version": row["decision_version"], "media_version": row["media_version"],
            "media_manifest": row.get("media_manifest") or [], "policy": {"enabled": True, "version": policy["version"]}}
    sid, status = row["submission_id"], row["status"]
    safe_sid, safe_status = html.escape(sid), html.escape(status)
    reason = html.escape(str(row.get("rejection_reason") or "Hợp lệ"))
    form_line = submission_form_line(row.get("form_id"))
    form_suffix = f"\n{form_line}" if form_line else ""
    ids = []

    async def add(target, payload):
        if selected is not None and target not in selected and payload.get("message_class") not in selected:
            return
        if selected is not None and _dict(payload.get("policy", base["policy"])).get("enabled") is not True:
            return
        ids.append(await enqueue_action(conn, sid, target, transition, {**base, **payload}))

    admin_role = "admin" if settings.TELEGRAM_ADMIN_BOT_TOKEN else "primary"
    default_admin_markup = {"inline_keyboard": [[{"text": "Mở Phiếu Xét Duyệt", "callback_data": f"admin_review:{sid}"}]]}
    if transition != "PARTY_ONLY":
        await add("TELEGRAM_NOTIFY", {"bot_role": admin_role, "chat_id": settings.TELEGRAM_ADMIN_GROUP_ID,
            "message_class": "ADMIN_REVIEW" if transition in {"SUBMITTED", "LEGACY_RECONCILED"} else transition + "_ADMIN",
            "parse_mode": "HTML", "text": context.get("admin_text") or f"Phiếu khảo sát #{safe_sid}: <b>{safe_status}</b>{form_suffix}\n{reason}",
            "reply_markup": with_survey_copy(context.get("admin_markup") or default_admin_markup, sid)})
    recipient, skip = await _recipient(conn, row, policy)
    driver_role = "driver" if settings.TELEGRAM_DRIVER_BOT_TOKEN else "primary"
    party = {"bot_role": driver_role, "chat_id": recipient, "message_class": "PARTY_VERDICT" if transition in {"SUBMITTED", "LEGACY_RECONCILED"} else transition + "_PARTY",
             "parse_mode": "HTML", "text": context.get("party_text") or f"Cập Nhật Khảo Sát #{safe_sid}\nTrạng thái: <b>{safe_status}</b>{form_suffix}\n{reason}"}
    if skip:
        party["policy"] = {"enabled": False, "version": policy["version"], "skip_reason": skip}
    elif settings.TELEGRAM_ADMIN_BOT_TOKEN and driver_role != "admin":
        party["alternate_bot_role"] = "admin"
    binding = row.get("_recipient_binding")
    if binding and not binding.get("verified"):
        party["text"] += (
            "\n\nℹ️ <b>Đây là hệ thống xác minh khảo sát thu nhập của Trust Bridge.</b>\n"
            f"Thông báo này được gửi vì số <code>{html.escape(str(binding.get('phone') or ''))}</code> "
            f"đã được nhập vào phiếu <code>{safe_sid}</code>.\n"
            "Nếu số này <b>không phải của bạn</b>, bạn không cần làm gì.\n"
            "Nếu đây là số của bạn và bạn muốn xem hoặc quản lý phiếu, hãy xác thực số điện thoại bằng nút bên dưới."
        )
        party["reply_markup"] = {
            "keyboard": [
                [{"text": "📱 Xác Thực Số Điện Thoại", "request_contact": True}],
                [{"text": "📋 Khảo Sát Của Tôi"}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
    if binding:
        party["recipient_binding"] = binding
    await add("TELEGRAM_NOTIFY", party)
    row["collaborator_id"] = await _billing_collaborator(conn, row)
    for target in ("R2_UPLOAD", "LARK_SYNC", "GDRIVE_EXPORT", "BILLING"):
        enabled = status == "APPROVED"
        payload = {}
        if target == "R2_UPLOAD":
            enabled = enabled and bool(settings.R2_PRIVATE_BUCKET_NAME) and settings.R2_PRIVATE_BUCKET_NAME != settings.R2_BUCKET_NAME and bool(base["media_manifest"])
            payload["destination"] = {"private": True, "bucket": settings.R2_PRIVATE_BUCKET_NAME}
        elif target == "LARK_SYNC":
            enabled = enabled and bool(
                settings.LARK_APP_ID and settings.LARK_APP_ID != "cli_mock_id"
                and settings.LARK_APP_SECRET and settings.LARK_BASE_APP_TOKEN and settings.LARK_BASE_TABLE_ID
            )
            payload.update(app_token=settings.LARK_BASE_APP_TOKEN, table_id=settings.LARK_BASE_TABLE_ID,
                           fields={"Mã Phiếu (Submission ID)": sid, "Trạng Thái (Status)": status,
                                   "Doanh Thu (Gross Revenue)": row.get("total_gross_revenue") or 0,
                                   "Thu Nhập Ròng (Net Income)": row.get("total_net_income") or 0,
                                   "Số Chuyến (Total Trips)": row.get("total_trips") or 0})
        elif target == "GDRIVE_EXPORT":
            enabled = enabled and bool(settings.GOOGLE_DRIVE_ROOT_FOLDER_ID)
            payload["form_id"] = row.get("form_id")
            payload["destination"] = {"private": True, "root_folder_id": settings.GOOGLE_DRIVE_ROOT_FOLDER_ID}
        else:
            amount = settings.BILLING_PAYOUT_VND
            enabled = enabled and row.get("collaborator_id") is not None and type(amount) is int and amount > 0
            payload.update(collaborator_id=row.get("collaborator_id"), amount_vnd=amount)
        payload["policy"] = {"enabled": enabled, "version": settings.BILLING_POLICY_VERSION if target == "BILLING" else policy["version"],
                             "skip_reason": "INELIGIBLE_OR_UNCONFIGURED" if not enabled else None}
        await add(target, payload)
    return ids


async def transition_submission(conn, submission_id, new_status, reason, *, actor="system", expected_statuses=None, schema=None):
    row = await _row(conn, submission_id)
    if not row:
        return False, "NOT_FOUND"
    if row.get("work_owner") != "outbox_v2":
        raise ValueError("LEGACY_RECONCILIATION_REQUIRED")
    current = row["status"]
    if expected_statuses is not None and current not in expected_statuses:
        return False, current
    if new_status == current:
        return False, current
    await conn.execute(
        "UPDATE survey_submissions SET status=%s,rejection_reason=%s,moderated_at=NOW(),decision_version=decision_version+1 WHERE submission_id=%s",
        (new_status, reason or None, row["submission_id"]),
    )
    await audit(conn, "STATUS_TRANSITION", row["submission_id"], actor, {"from": current, "to": new_status, "decision_version": row["decision_version"] + 1})
    await enqueue_submission_work(conn, row["submission_id"], new_status, schema=schema)
    return True, new_status


async def queue_drive_sync(conn, actor="system", *, action_key=None):
    await require_delivery_schema(conn)
    job_id = "DRIVE-" + (hashlib.sha256(action_key.encode()).hexdigest()[:58] if action_key else uuid4().hex)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM survey_submissions WHERE status='APPROVED' ORDER BY submission_id")
        records = await cur.fetchall()
    from services.google_drive_delivery import is_test_submission
    records = [dict(row) for row in records if not is_test_submission(row["submission_id"])]
    payload = {"policy": {"enabled": bool(settings.GOOGLE_DRIVE_ROOT_FOLDER_ID), "version": "drive-full-v1", "skip_reason": "DRIVE_NOT_CONFIGURED"}, "destination": {"private": True, "folder_id": settings.GOOGLE_DRIVE_ROOT_FOLDER_ID}, "submissions": records}
    await enqueue_action(conn, job_id, "GDRIVE_EXPORT", "drive.full_sync", payload, aggregate_kind="drive_sync", action_key=action_key)
    await audit(conn, "DRIVE_SYNC_REQUESTED", job_id, actor, {"records": len(records)})
    return job_id
