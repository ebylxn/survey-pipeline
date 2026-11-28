# -*- coding: utf-8 -*-
"""
Suite D: User-Facing Behavior & Simulation Test Suite.
Verifies all human-facing interactions, UI behaviors, and multi-party routing by simulation:
1. Form & modal buttons: signature confirmation with cooldown, reset prompts, CTV selection activation.
2. Admin decision buttons & two-step confirmation flows (Duyệt/Từ chối).
3. Admin routing simulation: 1-to-1 conversation bindings between multiple admins and CTVs.
4. 1-Admin-1-Convo Cap: strict restriction preventing ticket hoarding.
5. User-facing information display: driver name suppression when absent/placeholder, license plate stripping.
"""

import os
import pytest
from unittest.mock import patch
from contextlib import asynccontextmanager

from config import settings
from core.embedding_executor import embedding_executor_lifespan
from fastapi import FastAPI
from psycopg import errors as pg_errors
from routers import survey as survey_router


@asynccontextmanager
async def _bounded_test_lifespan(_app):
    async with embedding_executor_lifespan():
        yield


def _submission_test_client(**kwargs):
    isolated = FastAPI(lifespan=_bounded_test_lifespan)
    isolated.include_router(survey_router.router)
    return TestClient(isolated, **kwargs)
from fastapi.testclient import TestClient

from main import app
from tests.real_test_samples import get_real_distinct_test_photos
from services.dynamic_form_html import render_dynamic_form_html
from services.form_engine import DEFAULT_TRUST_BRIDGE_SCHEMA
from services.admin_moderator import render_dossier_message
from services.telegram_polling import (
    TelegramPollingRunner,
    ACTIVE_SUPPORT_CONVERSATIONS,
    ADMIN_GROUP_ACTIVE_SESSION,
    ADMIN_ACTIVE_CONVERSATIONS,
)


# ==============================================================================
# 1. User-Facing Buttons & Modals (Client UI)
# ==============================================================================

def test_signature_modal_button_event_wiring_and_cooldown():
    """
    Touchscreen Ghost-Click Prevention:
    On touchscreens, mobile browsers synthesize a fake mouse click ~300ms after touchend.
    Having both ontouchend and onclick caused double-tap ghost clicks that closed and instantly
    reopened modals. We strip ontouchend and enforce a 400ms cooldown (sigModalCooldown).
    """
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    # 1. Ensure ontouchend is stripped
    assert 'ontouchend="confirmModalSig()"' not in html
    assert 'onclick="confirmModalSig(event)"' in html
    # 2. Cooldown timer exists in script
    assert "sigModalCooldown" in html


def test_submit_button_recovers_from_hung_request():
    """
    A stalled submit must abort instead of leaving "Đang gửi..." disabled forever:
    the fetch carries an AbortSignal and the abort path shows a retryable message.
    """
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert "AbortController" in html
    assert "controller.abort()" in html
    assert "submit_timeout" in html


# ==============================================================================
# 2. Admin Decision Buttons & Two-Step Confirmation
# ==============================================================================

@pytest.mark.asyncio
async def test_admin_decision_buttons_require_confirmation_before_committing():
    """Admin tapping Duyệt/Từ chối triggers confirmation prompt before modifying submission state."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    os.makedirs(os.path.dirname(dev_storage_file), exist_ok=True)
    dev_records = [{"submission_id": "SIM-TEST-01", "status": "MANUAL_REVIEW"}]
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump(dev_records, f)

    review_bundles = []
    async def mock_bundle(chat_id, key, dossier, caption, txt_bytes, reply_markup=None):
        review_bundles.append({"caption": caption, "txt": txt_bytes.decode("utf-8"), "reply_markup": reply_markup})
        return True
    runner.send_dossier_bundle = mock_bundle

    edited_markups = []
    async def mock_edit(chat_id, msg_id, reply_markup=None):
        edited_markups.append(reply_markup)
        return True
    runner.edit_message_reply_markup = mock_edit

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    # 1. Open review bundle presents request-confirmation buttons + raw data txt
    await runner.execute_submission_review(123456, "SIM-TEST-01")
    assert len(review_bundles) >= 1
    assert "SIM-TEST-01" in review_bundles[-1]["caption"]
    assert "Số Liệu" not in review_bundles[-1]["caption"]
    assert '"submission_id"' in review_bundles[-1]["txt"]
    assert '"raw_data"' not in review_bundles[-1]["txt"]
    rev_btns = review_bundles[-1]["reply_markup"]["inline_keyboard"]
    assert any("admin_req_approve:SIM-TEST-01" in b.get("callback_data", "") for row in rev_btns for b in row)

    # 2. Clicking Duyệt prompts confirmation dialog with CONFIRM and CANCEL
    cq_req = {
        "id": "cq_1",
        "data": "admin_req_approve:SIM-TEST-01",
        "message": {"message_id": 100, "chat": {"id": 123456}},
        "from": {"id": 888, "first_name": "Admin1"},
    }
    await runner.handle_callback_query(cq_req)
    assert len(edited_markups) == 1
    conf_btns = edited_markups[0]["inline_keyboard"]
    assert any("admin_approve:SIM-TEST-01" in b.get("callback_data", "") for row in conf_btns for b in row)
    assert any("admin_cancel_decision:SIM-TEST-01" in b.get("callback_data", "") for row in conf_btns for b in row)

    # 3. Confirming commits status change and permanently locks card
    cq_confirm = {
        "id": "cq_2",
        "data": "admin_approve:SIM-TEST-01",
        "message": {"message_id": 100, "chat": {"id": 123456}},
        "from": {"id": 888, "first_name": "Admin1"},
    }
    await runner.handle_callback_query(cq_confirm)
    assert len(edited_markups) == 2
    final_btns = edited_markups[1]["inline_keyboard"]
    assert any("resolved_notice" in b.get("callback_data", "") for row in final_btns for b in row)


@pytest.mark.asyncio
async def test_admin_review_callback_answers_before_dossier_uploads(monkeypatch):
    """
    Telegram keeps the inline-button spinner until answerCallbackQuery lands.
    The review callback must ack first, then upload sendMediaGroup -> sendDocument
    (which can take 20-30s and previously made the bot look frozen).
    """
    import asyncio
    from services import telegram_polling as tp

    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "123456")
    events = []
    bundles = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        events.append("answer_callback_query")
        return True
    runner.answer_callback_query = mock_answer_cq

    async def mock_bundle(*args, **kwargs):
        bundles.append(kwargs)
        started.set()
        events.append("sendMediaGroup")
        await release.wait()
        events.append("sendDocument")
        return True
    runner.send_dossier_bundle = mock_bundle

    async def mock_dossier(sub_id, db_pool=None):
        return {"submission_id": sub_id, "status": "MANUAL_REVIEW", "photo_paths": ["/tmp/x.jpg"]}

    with patch("services.telegram_polling.query_submission_dossier", new=mock_dossier):
        await runner.handle_callback_query({
            "id": "cq_order",
            "data": "admin_review:VN-ORDER1",
            "message": {"message_id": 7, "chat": {"id": 123456}},
            "from": {"id": 888},
        })
        tasks = list(tp.BACKGROUND_REVIEW_TASKS)
        await asyncio.wait_for(started.wait(), 1)
        # The webhook already returned while the media upload is still in flight,
        # and the callback was acknowledged before any upload started.
        assert events == ["answer_callback_query", "sendMediaGroup"]
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)

    assert events == ["answer_callback_query", "sendMediaGroup", "sendDocument"]
    # Data document still carries the approve/reject buttons under it.
    keyboard = bundles[0]["reply_markup"]["inline_keyboard"]
    assert any("admin_req_approve:VN-ORDER1" in b.get("callback_data", "") for row in keyboard for b in row)


@pytest.mark.asyncio
async def test_admin_review_callback_answers_once_when_upload_fails(monkeypatch):
    """A failing dossier upload is logged, never re-answers or crashes the webhook."""
    import asyncio
    from services import telegram_polling as tp

    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "123456")
    answers = []

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        answers.append(text)
        return True
    runner.answer_callback_query = mock_answer_cq

    async def failing_bundle(*args, **kwargs):
        raise RuntimeError("telegram upload unavailable")
    runner.send_dossier_bundle = failing_bundle

    async def mock_dossier(sub_id, db_pool=None):
        return {"submission_id": sub_id, "status": "MANUAL_REVIEW", "photo_paths": []}

    with patch("services.telegram_polling.query_submission_dossier", new=mock_dossier):
        await runner.handle_callback_query({
            "id": "cq_fail",
            "data": "admin_review:VN-FAIL1",
            "message": {"message_id": 8, "chat": {"id": 123456}},
            "from": {"id": 888},
        })
        tasks = list(tp.BACKGROUND_REVIEW_TASKS)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

    assert answers == ["Đang mở phiếu..."]


@pytest.mark.asyncio
async def test_support_ticket_markup_has_no_dead_close_button():
    """Support ticket button card contains ONLY active locked status; no dead close button."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    edited_markups = []
    async def mock_edit(chat_id, msg_id, reply_markup=None):
        edited_markups.append(reply_markup)
        return True
    runner.edit_message_reply_markup = mock_edit

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    cq_claim = {
        "id": "cq_c",
        "data": "admin_reply_chat:1001",
        "message": {"message_id": 999, "chat": {"id": -100999}},
        "from": {"id": 888, "first_name": "Alex"},
    }
    await runner.handle_callback_query(cq_claim)

    assert len(edited_markups) == 1
    buttons = edited_markups[0]["inline_keyboard"]
    all_callbacks = [b.get("callback_data", "") for row in buttons for b in row]
    assert any("admin_locked_info:1001" in cb for cb in all_callbacks)
    assert not any("admin_close_ticket" in cb for cb in all_callbacks)


# ==============================================================================
# 3. Admin Routing & 1-to-1 Multi-Party Simulation
# ==============================================================================

@pytest.mark.asyncio
async def test_simulated_multi_admin_routing_isolation(monkeypatch):
    """
    Simulation:
    CTV A and CTV B request support simultaneously.
    Admin C accepts A; Admin D accepts B.
    - Admin C messages route exclusively to A.
    - Admin D messages route exclusively to B.
    - Accidental cross-replies are blocked.
    """
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    from unittest.mock import AsyncMock
    monkeypatch.setattr(settings, "TELEGRAM_DRIVER_BOT_TOKEN", "offline:relay-test")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_BOT_TOKEN", "offline:admin-relay-test")
    runner.send_message = AsyncMock()
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    relayed_messages = []
    class MockClient:
        async def post(self, url, **kwargs):
            class Resp:
                status_code = 200
            relayed_messages.append(kwargs)
            return Resp()
    async def get_mock_client():
        return MockClient()
    runner.get_client = get_mock_client

    # 1. Admin C (1001) takes CTV A (1111)
    await runner.handle_callback_query({
        "id": "c1",
        "data": "admin_reply_chat:1111",
        "message": {"message_id": 10, "chat": {"id": -100999}},
        "from": {"id": 1001, "first_name": "Admin_C"},
    })

    # 2. Admin D (2002) takes CTV B (2222)
    await runner.handle_callback_query({
        "id": "d1",
        "data": "admin_reply_chat:2222",
        "message": {"message_id": 20, "chat": {"id": -100999}},
        "from": {"id": 2002, "first_name": "Admin_D"},
    })

    assert ADMIN_ACTIVE_CONVERSATIONS[1001] == 1111
    assert ADMIN_ACTIVE_CONVERSATIONS[2002] == 2222

    # 3. Admin C sends message -> goes ONLY to CTV A
    relayed_messages.clear()
    await runner.handle_update({
        "message": {
            "message_id": 101,
            "chat": {"id": -100999},
            "from": {"id": 1001, "first_name": "Admin_C"},
            "text": "Message to CTV A",
        }
    })
    a_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 1111]
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(a_msgs) == 1
    assert len(b_msgs) == 0

    # 4. Admin D sends message -> goes ONLY to CTV B
    relayed_messages.clear()
    await runner.handle_update({
        "message": {
            "message_id": 102,
            "chat": {"id": -100999},
            "from": {"id": 2002, "first_name": "Admin_D"},
            "text": "Message to CTV B",
        }
    })
    a_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 1111]
    b_msgs = [m for m in relayed_messages if m.get("json", {}).get("chat_id") == 2222]
    assert len(a_msgs) == 0
    assert len(b_msgs) == 1
    relayed_messages.clear()
    await runner.handle_update({"message": {
        "chat": {"id": -100999}, "from": {"id": 1001, "first_name": "Admin_C"},
        "text": "Do not send to B", "reply_to_message": {"text": "User ID: 2222"},
    }})
    assert not relayed_messages
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()


@pytest.mark.asyncio
async def test_simulated_one_admin_one_convo_cap():
    """An admin already holding an active support session cannot claim another ticket."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    ACTIVE_SUPPORT_CONVERSATIONS.clear()
    ADMIN_GROUP_ACTIVE_SESSION.clear()
    ADMIN_ACTIVE_CONVERSATIONS.clear()

    alerts = []
    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        alerts.append({"text": text, "show_alert": show_alert})
        return True
    runner.answer_callback_query = mock_answer_cq

    # 1. Admin accepts Ticket 1
    await runner.handle_callback_query({
        "id": "c1",
        "data": "admin_reply_chat:1001",
        "message": {"message_id": 1, "chat": {"id": -100999}},
        "from": {"id": 555, "first_name": "Admin_X"},
    })
    assert ADMIN_ACTIVE_CONVERSATIONS[555] == 1001

    # 2. Admin attempts to claim Ticket 2 -> BLOCKED
    alerts.clear()
    await runner.handle_callback_query({
        "id": "c2",
        "data": "admin_reply_chat:2002",
        "message": {"message_id": 2, "chat": {"id": -100999}},
        "from": {"id": 555, "first_name": "Admin_X"},
    })
    assert any("đang trong phiên hỗ trợ với thành viên khác" in a.get("text", "") for a in alerts)
    assert 2002 not in ACTIVE_SUPPORT_CONVERSATIONS


# ==============================================================================
# 4. Exposed & Tamperable Information Protection Checks
# ==============================================================================
def test_user_facing_driver_name_suppression_when_placeholder():
    """When driver name is missing or placeholder ('Tài xế'), dossier displays 'Số điện thoại:' directly."""
    # Placeholder
    dossier_anon = {
        "submission_id": "VN-01",
        "status": "APPROVED",
        "driver_name": "Tài xế",
        "driver_phone": "0912345678",
    }
    out_anon = render_dossier_message(dossier_anon)
    assert "• <b>Số điện thoại:</b> <code>0912345678</code>" in out_anon
    assert "Tài xế: Tài xế" not in out_anon

    # Verified
    dossier_named = {
        "submission_id": "VN-02",
        "status": "APPROVED",
        "driver_name": "Trần Văn C",
        "driver_phone": "0987654321",
    }
    out_named = render_dossier_message(dossier_named)
    assert "• <b>Tài xế:</b> <b>Trần Văn C</b> (<code>0987654321</code>)" in out_named


@pytest.mark.asyncio
async def test_telegram_admin_notification_license_plate_stripped(monkeypatch):
    """Admin group alert notification strips license plate to prevent privacy leakage."""
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    dispatched = []
    async def mock_send_tg(chat_id, text, reply_markup=None):
        dispatched.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})

    patch_target = "routers.survey.send_telegram_notification" if os.path.exists("routers/survey.py") else "main.send_telegram_notification"
    with patch(patch_target, side_effect=mock_send_tg), \
         patch("config.settings.TELEGRAM_ADMIN_GROUP_ID", "-1001234567890"):
        real_photos = get_real_distinct_test_photos(7)
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "driver_phone": "0988999888",
                "license_plate": "29E-999.88",
                "consent_choice": "Tôi đồng ý",
                "signature_png": "data:image/png;base64,mock",
                "vehicle_type": "Ô tô",
                "car_model": "VF 5 Plus",
                "gsm_duration": "Từ 6 – 12 tháng",
                "gsm_contract_type": "Xe Taxi xanh GSM",
                "daily_trips": "18",
                "monthly_income_range": "12 – 20 triệu VNĐ",
                "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
                "is_test_mode": "false",
                "bypass_img_check": "false",
                "gps_latitude": "10.7769",
                "gps_longitude": "106.7009",
            },
            files={
                f"survey_photo_{i}": (f"slot_{i}.jpg", real_photos[i][1], "image/jpeg")
                for i in range(min(7, len(real_photos)))
            },
        )
        assert resp.status_code == 200

    admin_msg = next((m for m in dispatched if str(m["chat_id"]) == "-1001234567890"), None)
    assert admin_msg is not None
    assert "29E-999.88" not in admin_msg["text"]
    assert "Biển số xe" not in admin_msg["text"]
    assert admin_msg["reply_markup"] is not None
def test_tamperable_and_exposed_info_protection_in_urls_and_drafts():
    """
    Generalized protection against exposed or tamperable information across the user-facing surface:
    1. Geolocation Tampering in URLs: Coordinates (lat, lon, acc) must NEVER be exposed or
       passed via URL query parameters, preventing trivial client-side geofence spoofing.
    2. Draft Keystroke Leakage: Server drafts are client-only (localStorage) to prevent
       driver phone numbers or inputs leaking to subsequent drivers opening shared referral links.
    3. Secret Exposure: Client HTML templates must never leak backend secrets or tokens.
    """
    from services.dynamic_form_html import render_ctv_selector_landing_html
    ctv_html = render_ctv_selector_landing_html(DEFAULT_TRUST_BRIDGE_SCHEMA, ["CTV001_Dũng_HN"])

    # 1. URL Query String Tampering Prevention:
    # CTV selection must delete any spoofed lat/lon/acc params and store coordinates in storage only!
    assert 'params.delete("lat")' in ctv_html
    assert 'params.delete("lon")' in ctv_html
    assert 'params.delete("acc")' in ctv_html
    assert 'params.set("lat"' not in ctv_html
    assert 'params.set("lon"' not in ctv_html

    # 2. Draft Storage Isolation:
    client = TestClient(app)
    draft_resp = client.get("/api/v1/forms/draft/ref-ctv004")
    assert draft_resp.status_code == 200
    assert draft_resp.json().get("status") == "client_only"

    # 3. Secret Exposure:
    form_html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert "postgresql://" not in form_html
    assert "mock_webhook_secret" not in form_html
    assert "service_account" not in form_html
@pytest.mark.asyncio
async def test_auto_delete_telegram_messages_past_14_days():
    """
    Simulates automated message deletion past 14 days:
    1. Internal chat logs older than 14 days are purged from database.
    2. Tracked sent messages past 14 days in supergroups/channels trigger deleteMessage.
    3. Private DM records past 14 days are cleanly pruned from retention storage.
    """
    from services.audit_and_privacy import purge_14day_telegram_retention
    from unittest.mock import AsyncMock, MagicMock

    mock_cur = AsyncMock()
    mock_cur.rowcount = 42
    # Simulate finding old messages: one group (-100123) and one private DM (8888)
    mock_cur.fetchall.return_value = [
        ("rec_01", -1001234567890, 501),
        ("rec_02", 88889999, 502),
    ]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__aenter__.return_value = mock_cur
    mock_conn.commit = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.connection.return_value.__aenter__.return_value = mock_conn

    deleted_api_calls = []
    class MockClient:
        async def post(self, url, **kwargs):
            deleted_api_calls.append(kwargs.get("json"))
            class Resp:
                status_code = 200
            return Resp()

    res = await purge_14day_telegram_retention(mock_pool, MockClient(), bot_token="123:mock_token")

    assert res["chat_history_purged"] == 42
    assert res["messages_deleted"] == 2
    # deleteMessage triggered for the group chat (-1001234567890)
    assert any(call.get("chat_id") == -1001234567890 and call.get("message_id") == 501 for call in deleted_api_calls)
# ==============================================================================
# 5. Simulated User Actions & Edge-Case Input Checks
# ==============================================================================

def test_simulated_user_floating_reset_button_clearing():
    """Simulates user filling form and clicking floating reset: all state, inputs, and drafts are purged."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    # 1. Reset button exists as floating overlay with prompt handler
    assert "btn-floating-reset" in html
    assert "promptResetForm()" in html
    # 2. Reset execution purges localStorage draft and deep-resets camera slots
    assert "executeResetForm(" in html
    assert "localStorage.removeItem(storageKey)" in html
    assert "delete answers[k]" in html
    assert "selectCameraSlot(q.id, 0)" in html


@pytest.mark.asyncio
async def test_simulated_user_bypass_checkbox_toggle_not_sticky(monkeypatch):
    """
    Simulates user toggling 'Bỏ qua kiểm tra ảnh':
    1. Submitting with bypass_img_check=true approves test mode.
    2. Submitting with bypass_img_check=false immediately enforces real checks (not sticky!).
    """
    monkeypatch.setenv("MOCK_AI", "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    client = TestClient(app)

    # Submission 1: Checked bypass -> passes
    resp_bypassed = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0911222333",
            "is_test_mode": "true",
            "bypass_img_check": "true",
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "vehicle_type": "Ô tô",
            "car_model": "VF 5 Plus",
            "gsm_duration": "Từ 6 – 12 tháng",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "18",
            "monthly_income_range": "12 – 20 triệu VNĐ",
            "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        },
    )
    assert resp_bypassed.status_code == 200
    assert resp_bypassed.json().get("status") == "APPROVED"

    # Submission 2: User explicitly unchecks bypass -> must NOT remain bypassed!
    resp_unbypassed = client.post(
        "/api/v1/survey/submit-webrtc",
        data={
            "driver_phone": "0911222333",
            "is_test_mode": "true",
            "bypass_img_check": "false",
            "consent_choice": "Tôi đồng ý",
            "signature_png": "data:image/png;base64,mock",
            "vehicle_type": "Ô tô",
            "car_model": "VF 5 Plus",
            "gsm_duration": "Từ 6 – 12 tháng",
            "gsm_contract_type": "Xe Taxi xanh GSM",
            "daily_trips": "18",
            "monthly_income_range": "12 – 20 triệu VNĐ",
            "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        },
    )
    assert resp_unbypassed.status_code == 200
    # Must reject because photos were not attached!
    assert resp_unbypassed.json().get("status") == "REJECTED"


def test_simulated_user_gallery_upload_keeps_camera_off_and_advances_slot():
    """
    Simulates user choosing files from the gallery:
    slot selection keeps the camera off (shouldActivateCamera = false) and advances
    to the next uncaptured slot instead of auto-starting the camera.
    """
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert "shouldActivateCamera = false" in html
    assert "function advanceToNextUncapturedSlot(qid)" in html
    assert "activeSlotByQId[qid] = targetIdx;" in html


def test_simulated_user_ctv_draft_isolation_client_only():
    """Simulates draft persistence: verifies draft endpoint is client_only to prevent server-side phone leaks."""
    client = TestClient(app)
    resp = client.get("/api/v1/forms/draft/ref-ctv004")
    assert resp.status_code == 200
    assert resp.json().get("status") == "client_only"

    # Saving drafts also returns client_only
    resp_post = client.post(
        "/api/v1/forms/draft",
        json={"ctv_slug": "ref-ctv004", "draft_data": {"driver_phone": "0580800056"}},
    )
    assert resp_post.status_code == 200
    assert resp_post.json().get("status") == "client_only"


@pytest.mark.asyncio
async def test_simulated_admin_swipe_reply_check_extracts_sub_id(monkeypatch, tmp_path):
    """Simulates admin swiping right on a Telegram notification to reply /check: extracts sub_id seamlessly."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "123456")
    monkeypatch.chdir(tmp_path)

    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    os.makedirs(os.path.dirname(dev_storage_file), exist_ok=True)
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump([{"submission_id": "VN-SWIPE-99", "status": "MANUAL_REVIEW"}], f)

    sent_msgs = []
    async def mock_bundle(chat_id, key, dossier, caption, txt_bytes, reply_markup=None):
        sent_msgs.append({"chat_id": chat_id, "text": caption, "reply_markup": reply_markup})
        return True
    runner.send_dossier_bundle = mock_bundle

    # Admin sends /check replying to a message containing the submission ID
    msg_reply = {
        "text": "/check",
        "reply_to_message": {
            "text": "⏳ PHIẾU KHẢO SÁT CHỜ DUYỆT THỦ CÔNG #VN-SWIPE-99\n• Số điện thoại: 0988111222\n• Doanh thu: 15,000,000 VNĐ"
        }
    }
    await runner.handle_command(123456, "/check", reply_to_message=msg_reply["reply_to_message"], from_user_id=888)

    assert len(sent_msgs) >= 1
    resp = sent_msgs[-1]
    assert "VN-SWIPE-99" in resp["text"]
    assert resp["reply_markup"] is not None




@pytest.mark.asyncio
async def test_simulated_admin_decision_cancel_flow():
    """Simulates admin clicking Duyệt then clicking HỦY BỎ: restores original buttons without touching status."""
    runner = TelegramPollingRunner(bot_token="mock_token", role="admin")

    import os, json
    dev_storage_file = os.path.join(os.getcwd(), "data", "dev_test_submissions.json")
    os.makedirs(os.path.dirname(dev_storage_file), exist_ok=True)
    with open(dev_storage_file, "w", encoding="utf-8") as f:
        json.dump([{"submission_id": "VN-CANCEL-01", "status": "MANUAL_REVIEW"}], f)

    edited_markups = []
    async def mock_edit(chat_id, msg_id, reply_markup=None):
        edited_markups.append(reply_markup)
        return True
    runner.edit_message_reply_markup = mock_edit

    async def mock_answer_cq(cq_id, text=None, show_alert=False):
        return True
    runner.answer_callback_query = mock_answer_cq

    # 1. Click Duyệt -> prompts confirmation
    await runner.handle_callback_query({
        "id": "cq_1",
        "data": "admin_req_approve:VN-CANCEL-01",
        "message": {"message_id": 200, "chat": {"id": 123456}},
        "from": {"id": 777, "first_name": "Admin1"},
    })
    assert len(edited_markups) == 1

    # 2. Click HỦY BỎ -> restores original decision buttons
    await runner.handle_callback_query({
        "id": "cq_2",
        "data": "admin_cancel_decision:VN-CANCEL-01",
        "message": {"message_id": 200, "chat": {"id": 123456}},
        "from": {"id": 777, "first_name": "Admin1"},
    })
    assert len(edited_markups) == 2
    restored_btns = edited_markups[1]["inline_keyboard"]
    assert any("admin_req_approve:VN-CANCEL-01" in b.get("callback_data", "") for row in restored_btns for b in row)

    # 3. Status in dev storage is still MANUAL_REVIEW!
    with open(dev_storage_file, "r", encoding="utf-8") as f:
        recs = json.load(f)
    assert recs[0]["status"] == "MANUAL_REVIEW"


# ==============================================================================
# 6. Image links: inline preview + fullscreen viewer (no new tab / download)
# ==============================================================================

def test_description_image_links_embed_preview_and_open_fullscreen_view():
    """Image URLs in a description expand inline; clicking the preview opens a full-viewport viewer."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)

    # Image URLs are rendered as expandable links, not new-tab downloads
    assert "function isImageUrl(url)" in html
    assert 'onclick="return showInlineDescriptionImage(event, this)"' in html
    assert "data-img-src" in html
    assert "target=\"_blank\"" in html  # non-image links keep new-tab behavior
    assert "if (isImageUrl(url)) return imageLinkHtml(url, escapeHtml(label));" in html

    # Inline preview is scaled down and appended below the link's text line
    assert ".desc-inline-preview" in html
    assert "height: 180px" in html
    assert "lineBreak.parentElement.insertBefore(img, lineBreak.nextSibling);" in html

    # Full view has a back button and closes on backdrop click
    assert "function openImageFullView(src)" in html
    assert "function closeImageFullView()" in html
    assert "img-fullview-back" in html
    assert "e.target === overlay" in html


# ==============================================================================
# 7. Upload auto-advance and list-based choice option editing
# ==============================================================================

def test_photo_upload_advances_to_next_uncaptured_slot():
    """Uploading photos advances the active slot like live camera capture does."""
    html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA)
    assert "function advanceToNextUncapturedSlot(qid)" in html
    # Definition + single-file branch + multi-file branch
    assert html.count("advanceToNextUncapturedSlot(") >= 3
    assert "activeSlotByQId[qid] = targetIdx;" in html
    assert "uploadableSlots[startPos + maxFill - 1].idx" in html


def test_editor_choice_options_use_add_remove_list():
    """single_choice/dropdown options are edited as rows, not a comma-separated string."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "static" / "ui" / "form_editor.html").read_text(encoding="utf-8")
    assert "renderChoiceOptionsBuilder(q, idx)" in src
    assert "function addChoiceOption(idx)" in src
    assert "function removeChoiceOption(idx, oi)" in src
    assert "split(',').map(s=>s.trim())" not in src
    assert "phân cách bằng dấu phẩy" not in src


# ==============================================================================
# 8. Order-agnostic image object coverage
# ==============================================================================

def test_image_object_coverage_is_order_agnostic():
    """Profile/income objects are matched by role, not upload position."""
    import io
    from unittest.mock import AsyncMock, patch
    from PIL import Image
    from main import FORM_REGISTRY
    from services.form_engine import (
        FormSchema, FormQuestion, FormSettings, QuestionType, ImageCheckConfig,
    )

    def jpeg_bytes():
        buf = io.BytesIO()
        Image.new("RGB", (200, 300), (30, 160, 180)).save(buf, format="JPEG")
        return buf.getvalue()

    form_id = "SIM-COVERAGE-ORDER"
    FORM_REGISTRY[form_id] = FormSchema(
        form_id=form_id,
        title="Coverage Order Simulation",
        settings=FormSettings(
            decree13_required=False,
            is_test_mode=True,
            image_checks=ImageCheckConfig(enable_blur_check=False, enable_ai_vision=True),
        ),
        questions=[FormQuestion(
            id="q_cam", title="Anh", type=QuestionType.CAMERA_WEBRTC, required=True,
            image_slots=[
                {"slot": 0, "title": "Ho so", "role": "app_profile", "required": True},
                {"slot": 1, "title": "Thu nhap", "role": "app_income", "required": True},
            ],
            min_images=2, max_images=2, exact_image_count=2,
        )],
    )

    client = _submission_test_client()
    client.__enter__()
    data = {"form_id": form_id, "is_test_mode": "true", "bypass_img_check": "false",
            "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"}
    with patch.dict(os.environ, {"MOCK_AI": "1"}), \
         patch("routers.survey.send_telegram_notification", new=AsyncMock()), \
         patch("routers.survey.dispatch_submission_telegram_notifications", new=AsyncMock()), \
         patch("routers.survey.dispatch_action_checklist", new=AsyncMock()):
        try:
            # Profile/income uploaded in reverse order must still pass coverage
            reversed_files = [
                ("survey_photo_0", ("income.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("profile.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")),
            ]
            body = client.post("/api/v1/survey/submit-webrtc", data=data, files=reversed_files).json()
            assert "Thiếu ảnh" not in (body.get("reason") or ""), body

            # Two income shots and no profile must fail coverage by object type
            missing_files = [
                ("survey_photo_0", ("income.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")),
                ("survey_photo_1", ("income2.jpg", io.BytesIO(jpeg_bytes()), "image/jpeg")),
            ]
            body = client.post("/api/v1/survey/submit-webrtc", data=data, files=missing_files).json()
            assert "Thiếu ảnh đúng loại" in (body.get("reason") or ""), body
        finally:
            FORM_REGISTRY.pop(form_id, None)
            client.__exit__(None, None, None)


class _FakeSubmissionDB:
    """Minimal psycopg-shaped pool that records committed verdict rows."""

    def __init__(self, fail_rich_insert=False):
        self.fail_rich_insert = fail_rich_insert
        self.executed = []
        self.stored = {}
        self.rowcount = -1
        self.result = None

    def reset(self):
        self.executed.clear()
        self.stored.clear()

    def connection(self):
        return self

    def cursor(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, query, params=()):
        self.executed.append(query)
        self.result = None
        self.rowcount = -1
        if "INSERT INTO survey_submissions" in query:
            columns = query.split("(", 1)[1].split(")", 1)[0].split(", ")
            values = dict(zip(columns, params, strict=True))
            if self.fail_rich_insert and "image_dhash" in values:
                raise pg_errors.UndefinedColumn("legacy schema has no extended columns")
            submission_id = values["submission_id"]
            self.rowcount = int(submission_id not in self.stored)
            if self.rowcount:
                self.stored[submission_id] = {
                    **values,
                    "status": values["status"],
                    "reason": values.get("rejection_reason"),
                }
        elif "INSERT INTO outbox_events" in query:
            self.result = ("legacy-event",)

    async def fetchone(self):
        return self.result

    async def commit(self):
        return None

def _register_persist_probe_form(form_id: str):
    import io
    from PIL import Image
    from main import FORM_REGISTRY
    from services.form_engine import (
        FormSchema, FormQuestion, FormSettings, QuestionType, ImageCheckConfig,
    )

    def jpeg_bytes(color):
        buf = io.BytesIO()
        Image.new("RGB", (200, 260), color).save(buf, format="JPEG")
        return buf.getvalue()

    FORM_REGISTRY[form_id] = FormSchema(
        form_id=form_id,
        title="Persistence Probe",
        settings=FormSettings(
            decree13_required=False,
            ctv_ref_enabled=False,
            is_test_mode=False,
            image_checks=ImageCheckConfig(enable_blur_check=False, enable_ai_vision=True),
        ),
        questions=[FormQuestion(
            id="q_cam", title="Anh", type=QuestionType.CAMERA_WEBRTC, required=True,
            image_slots=[
                {"slot": 0, "title": "Ho so", "role": "app_profile", "required": True},
                {"slot": 1, "title": "Thu nhap", "role": "app_income", "required": True},
            ],
            min_images=2, max_images=2, exact_image_count=2,
        )],
    )
    data = {"form_id": form_id, "is_test_mode": "false", "bypass_img_check": "false",
            "driver_phone": "0988111222", "driver_name": "Nguyen Van A",
            "gps_latitude": "10.7769", "gps_longitude": "106.7009",
            "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"}
    files = [
        ("survey_photo_0", ("profile.jpg", io.BytesIO(jpeg_bytes((20, 60, 200))), "image/jpeg")),
        ("survey_photo_1", ("income.jpg", io.BytesIO(jpeg_bytes((220, 40, 40))), "image/jpeg")),
    ]
    return data, files


def test_no_row_is_visible_while_checks_run_and_one_final_row_after():
    """A slow AI must not expose a placeholder row; the single row appears with the verdict."""
    import asyncio
    import threading
    import time
    from unittest.mock import AsyncMock

    db = _FakeSubmissionDB()
    form_id = "SIM-VERDICT-FIRST"
    data, files = _register_persist_probe_form(form_id)
    result = {}
    classify_calls = []
    entered = threading.Event()
    release = threading.Event()

    async def slow_classify(image_bytes, filename=None, expected_slots=None):
        classify_calls.append(1)
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        return "app_profile" if "profile" in (filename or "") else "app_income"

    async def fake_profile(image_bytes):
        return {"membership_type": "Xanh SM"}

    async def fake_facts(image_bytes):
        return {}

    async def fake_income(image_bytes, week_idx=None):
        return {"gross_revenue": 1000000, "net_income": 800000, "discount_and_tax": 0,
                "total_trips": 10, "online_hours": "10h", "period": "Tuần 1",
                "period_tabs": ["Tuần 1"], "is_legible": True, "confidence_score": 0.9,
                "detected_app": "XANH_SM", "is_authentic_gsm_ui": True}

    def run_request():
        with _submission_test_client() as client:
            result["resp"] = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)

    try:
        with patch.dict(os.environ, {"MOCK_AI": "1"}), \
             patch("routers.survey.get_db_pool", return_value=db), \
             patch("routers.survey.send_telegram_notification", new=AsyncMock()), \
             patch("routers.survey.dispatch_submission_telegram_notifications", new=AsyncMock()), \
             patch("routers.survey.dispatch_action_checklist", new=AsyncMock()), \
             patch.object(survey_router.vision_client, "classify_image_role", new=slow_classify), \
             patch.object(survey_router.vision_client, "ocr_driver_app_profile", new=fake_profile), \
             patch.object(survey_router.vision_client, "extract_facts", new=fake_facts), \
             patch.object(survey_router.vision_client, "ocr_driver_app_income", new=fake_income):
            worker = threading.Thread(target=run_request)
            worker.start()
            assert entered.wait(5), "slow classifier was not reached"
            assert db.stored == {}, f"placeholder leaked while checks ran: {db.stored}"
            release.set()
            worker.join(15)

        body = result["resp"].json()
        assert result["resp"].status_code == 200
        assert classify_calls, "slow AI stub was not exercised"
        assert len(db.stored) == 1, f"expected exactly one final row, got {db.stored}"
        row = db.stored[body["submission_id"]]
        assert row["status"] == body["status"]
        expected_reason = None if body["status"] == "APPROVED" else body.get("reason")
        assert row["reason"] == expected_reason
        assert not any("'MANUAL_REVIEW'" in q for q in db.executed), "no placeholder insert allowed"
    finally:
        release.set()
        from main import FORM_REGISTRY
        FORM_REGISTRY.pop(form_id, None)


def test_undefined_column_fallback_preserves_final_verdict_row():
    """Extended-column failure rolls back its savepoint and stores the final verdict."""
    from unittest.mock import AsyncMock

    db = _FakeSubmissionDB(fail_rich_insert=True)
    form_id = "SIM-FK-FALLBACK"
    data, files = _register_persist_probe_form(form_id)
    try:
        with patch.dict(os.environ, {"MOCK_AI": "1"}), \
             patch("routers.survey.get_db_pool", return_value=db), \
             patch("routers.survey.send_telegram_notification", new=AsyncMock()), \
             patch("routers.survey.dispatch_submission_telegram_notifications", new=AsyncMock()), \
             patch("routers.survey.dispatch_action_checklist", new=AsyncMock()):
            with _submission_test_client() as client:
                resp = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)

        body = resp.json()
        assert resp.status_code == 200
        assert len(db.stored) == 1
        row = db.stored[body["submission_id"]]
        assert row["status"] == body["status"]
        assert row["status"] != "MANUAL_REVIEW" or body["status"] == "MANUAL_REVIEW"

        fallback = next(q for q in db.executed
                        if "INSERT INTO survey_submissions" in q and "image_dhash" not in q)
        assert "driver_id" not in fallback, "fallback insert must stay FK-safe"
    finally:
        from main import FORM_REGISTRY
        FORM_REGISTRY.pop(form_id, None)


def test_verdict_crash_writes_exactly_one_interruption_row():
    """Crash after photos are saved still leaves one MANUAL_REVIEW row, never zero."""
    from unittest.mock import AsyncMock
    from services.submission_pipeline import INTERRUPTED_CHECK_REASON

    db = _FakeSubmissionDB()
    form_id = "SIM-CRASH-GUARD"
    data, files = _register_persist_probe_form(form_id)
    try:
        with patch.dict(os.environ, {"MOCK_AI": "1"}), \
             patch("routers.survey.get_db_pool", return_value=db), \
             patch("routers.survey.send_telegram_notification", new=AsyncMock()), \
             patch("routers.survey.dispatch_submission_telegram_notifications", new=AsyncMock()), \
             patch("routers.survey.dispatch_action_checklist", new=AsyncMock()), \
             patch("services.submission_pipeline._classification_slots", side_effect=RuntimeError("vision stage crashed")):
            with _submission_test_client(raise_server_exceptions=False) as client:
                resp = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)

        assert resp.json()["mode"] == "PROD_MODE"
        assert resp.json()["driver_tag"]
        assert len(db.stored) == 1, f"expected one interruption row, got {db.stored}"
        row = next(iter(db.stored.values()))
        assert row["status"] == "MANUAL_REVIEW"
        assert row["reason"] == INTERRUPTED_CHECK_REASON
        inserts = [q for q in db.executed if "INSERT INTO survey_submissions" in q]
        assert len(inserts) == 1
    finally:
        from main import FORM_REGISTRY
        FORM_REGISTRY.pop(form_id, None)


def test_multi_image_submit_decodes_each_upload_once():
    """
    Every uploaded image is decoded/downscaled exactly once per submission.
    The old flow decoded each image up to four times (target frame, blur loop,
    AI normalisation pass, duplicate detector), which added seconds of CPU-bound
    work to every submit before the AI verdict could even start.
    """
    import io
    from unittest.mock import AsyncMock
    from PIL import Image
    from main import FORM_REGISTRY
    from services.form_engine import (
        FormSchema, FormQuestion, FormSettings, QuestionType, ImageCheckConfig,
    )
    from services import normalizer as normalizer_mod

    def jpeg_bytes(color):
        buf = io.BytesIO()
        Image.new("RGB", (240, 320), color).save(buf, format="JPEG")
        return buf.getvalue()

    form_id = "SIM-DECODE-ONCE"
    FORM_REGISTRY[form_id] = FormSchema(
        form_id=form_id,
        title="Decode Once Simulation",
        settings=FormSettings(
            decree13_required=False,
            is_test_mode=True,
            image_checks=ImageCheckConfig(enable_blur_check=True, enable_ai_vision=True),
        ),
        questions=[FormQuestion(
            id="q_cam", title="Anh", type=QuestionType.CAMERA_WEBRTC, required=True,
            image_slots=[
                {"slot": 0, "title": "Ho so", "role": "app_profile", "required": True},
                {"slot": 1, "title": "Thu nhap", "role": "app_income", "required": True},
            ],
            min_images=2, max_images=2, exact_image_count=2,
        )],
    )

    real_sanitize = normalizer_mod.sanitize_and_normalize_image
    decode_calls = []

    def counting_sanitize(*args, **kwargs):
        decode_calls.append(1)
        return real_sanitize(*args, **kwargs)

    client = _submission_test_client()
    client.__enter__()
    data = {"form_id": form_id, "is_test_mode": "true", "bypass_img_check": "false",
            "q_cam_0": "uploaded_0", "q_cam_1": "uploaded_1"}
    files = [
        ("survey_photo_0", ("profile.jpg", io.BytesIO(jpeg_bytes((20, 60, 200))), "image/jpeg")),
        ("survey_photo_1", ("income.jpg", io.BytesIO(jpeg_bytes((220, 40, 40))), "image/jpeg")),
    ]
    try:
        with patch.dict(os.environ, {"MOCK_AI": "1"}), \
             patch("routers.survey.send_telegram_notification", new=AsyncMock()), \
             patch("routers.survey.dispatch_submission_telegram_notifications", new=AsyncMock()), \
             patch("routers.survey.dispatch_action_checklist", new=AsyncMock()), \
             patch("routers.survey.sanitize_and_normalize_image", side_effect=counting_sanitize):
            resp = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)
        assert resp.status_code == 200
        assert len(decode_calls) == 2, f"expected 1 decode per upload, got {len(decode_calls)}"
    finally:
        FORM_REGISTRY.pop(form_id, None)
        client.__exit__(None, None, None)


def test_editor_conditions_use_hidden_constant_uids():
    """
    Simulated id edit: conditions are wired to an auto-generated hidden uid per
    question, so editing the visible Question ID (even through an empty intermediate)
    can never break conditional visibility. The uid is stripped from the payload.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "static" / "ui" / "form_editor.html").read_text(encoding="utf-8")

    assert "function newQuestionUid()" in src
    assert "function initializeConditionUids()" in src
    assert "rule.depends_on_uid = idToUid.get(rule.depends_on_question_id) || '';" in src
    assert "const parentId = uidToId.get(rule.depends_on_uid) || '';" in src
    assert "delete cleanQ.__uid;" in src
    assert "depends_on_uid: parent.__uid," in src
    assert "p.__uid === rule.depends_on_uid ? 'selected' : ''" in src
    assert "renameConditionalParents" not in src
    assert "__originalId" not in src

    # server validation failures are surfaced, not masked as a connection error
    assert "Lỗi lưu (HTTP " in src
    assert "await resp.json()" in src
    assert "res.detail" not in src


def test_multi_camera_questions_store_all_images():
    """Every camera question's slot files are persisted, not just the last question's."""
    import io, os, shutil
    from unittest.mock import AsyncMock, patch
    from PIL import Image
    from main import FORM_REGISTRY
    from services.form_engine import (
        FormSchema, FormQuestion, FormSettings, QuestionType, ImageCheckConfig,
    )

    def jpeg():
        b = io.BytesIO(); Image.new("RGB", (120, 180), (20, 120, 180)).save(b, format="JPEG")
        return b.getvalue()

    fid = "SIM-MULTI-CAM-IMAGES"
    FORM_REGISTRY[fid] = FormSchema(
        form_id=fid, title="Multi camera", settings=FormSettings(
            decree13_required=False, is_test_mode=True,
            image_checks=ImageCheckConfig(enable_blur_check=False, enable_ai_vision=False)),
        questions=[
            FormQuestion(id="q_a", title="A", type=QuestionType.CAMERA_WEBRTC, required=True,
                image_slots=[{"slot": 0, "title": "A0", "required": True},
                             {"slot": 1, "title": "A1", "required": True}],
                min_images=2, max_images=2, exact_image_count=2),
            FormQuestion(id="q_b", title="B", type=QuestionType.CAMERA_WEBRTC, required=True,
                image_slots=[{"slot": 0, "title": "B0", "required": True},
                             {"slot": 1, "title": "B1", "required": True}],
                min_images=2, max_images=2, exact_image_count=2),
        ],
    )
    client = _submission_test_client()
    client.__enter__()
    data = {"form_id": fid, "is_test_mode": "true", "bypass_img_check": "true",
            "q_a_0": "uploaded_0", "q_a_1": "uploaded_1",
            "q_b_0": "uploaded_0", "q_b_1": "uploaded_1"}
    files = [(f"q_{name}__file_{i}", (f"{name}{i}.jpg", io.BytesIO(jpeg()), "image/jpeg"))
             for name in ("a", "b") for i in range(2)]
    sub_id = None
    try:
        with patch("routers.survey.send_telegram_notification", new=AsyncMock()), \
             patch("routers.survey.dispatch_submission_telegram_notifications", new=AsyncMock()), \
             patch("routers.survey.dispatch_action_checklist", new=AsyncMock()):
            body = client.post("/api/v1/survey/submit-webrtc", data=data, files=files).json()
        sub_id = body.get("submission_id") or (body.get("data") or {}).get("submission_id")
        photo_dir = os.path.join(os.getcwd(), "data", "submission_photos", sub_id)
        stored = sorted(os.listdir(photo_dir))
        assert len(stored) == 4, stored
    finally:
        if sub_id:
            shutil.rmtree(os.path.join(os.getcwd(), "data", "submission_photos", sub_id), ignore_errors=True)
        FORM_REGISTRY.pop(fid, None)
        client.__exit__(None, None, None)


def test_description_media_path_linkifier_does_not_rewrite_inside_absolute_urls():
    """A /media path inside an absolute image URL must not be wrapped in a nested anchor."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "static" / "ui" / "dynamic_form.html").read_text(encoding="utf-8")
    assert "raw = raw.replace(/(^|[\\s(])(\\/media\\/[^\\s<)\"']+)/g" in src
    assert "(^|[^\"'])(\\/media\\/" not in src


def test_admin_viewers_fetch_from_apis_instead_of_missing_placeholders():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "static" / "ui"
    subs = (root / "submissions_viewer.html").read_text(encoding="utf-8")
    audit = (root / "audit_viewer.html").read_text(encoding="utf-8")
    assert "{forms_json}" not in subs and "{active_form_id_str}" not in subs
    assert "await loadSubmissions();" in subs and "/api/v1/submissions" in subs
    assert "{logs_json}" not in audit
    assert "/api/v1/audit/logs" in audit and "filterData();" in audit


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_count", [0, 1, 2])
async def test_dossier_bundle_sends_data_after_images(tmp_path, photo_count):
    """Choose the valid Telegram photo method, then send the document and retain all IDs."""
    import asyncio, json, os
    from PIL import Image
    from services.telegram_polling import TelegramPollingRunner

    calls = []
    active = 0
    max_active = 0

    class Resp:
        status_code = 200
        text = ""
        def __init__(self, payload): self._p = payload
        def json(self): return {"result": self._p}

    class Client:
        async def post(self, url, data=None, files=None, json=None, timeout=None):
            nonlocal active, max_active
            method = url.rsplit("/", 1)[-1]
            calls.append((method, data or {}))
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            if method == "sendMediaGroup":
                return Resp([{"message_id": 11}, {"message_id": 12}])
            if method == "sendPhoto":
                assert data["caption"] == "cap" and data["parse_mode"] == "HTML"
                with open(paths[0], "rb") as photo:
                    assert files["photo"][1] == photo.read()
                return Resp({"message_id": 11})
            if method == "sendDocument":
                return Resp({"message_id": 13})
            return Resp(True)

    runner = TelegramPollingRunner(bot_token="123:abc")
    async def get_client(): return Client()
    runner.get_client = get_client

    tmp = str(tmp_path)
    paths = []
    for i in range(photo_count):
        p = os.path.join(tmp, f"slot_{i}.jpg")
        Image.new("RGB", (40, 40), (i * 40, 90, 140)).save(p)
        paths.append(p)

    review_keyboard = {"inline_keyboard": [[
        {"text": "✅ Duyệt (Approve)", "callback_data": "admin_req_approve:VN-T"},
        {"text": "❌ Từ chối (Reject)", "callback_data": "admin_req_reject:VN-T"},
    ]]}
    ok = await runner.send_dossier_bundle(
        1, "k", {"submission_id": "VN-T", "photo_paths": paths}, "cap", b"{}",
        review_keyboard,
    )
    assert ok
    assert max_active == 1, "images and data must be sent sequentially"
    methods = [c[0] for c in calls]
    assert methods == (["sendPhoto"] if photo_count == 1 else ["sendMediaGroup"] if photo_count > 1 else []) + ["sendDocument"]
    data_call = [c for c in calls if c[0] == "sendDocument"][0]
    assert "reply_to_message_id" not in data_call[1]
    # approve/reject keyboard must ride on the data message
    assert "admin_req_approve" in data_call[1]["reply_markup"]
    assert "admin_req_reject" in data_call[1]["reply_markup"]
    assert runner.prompt_registry["k"] == list(range(11, 11 + photo_count)) + [13]


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_count,drive_url,expected", [
    (9, "https://drive.google.com/drive/folders/FOLDER1", "photos"),
    (10, "https://drive.google.com/drive/folders/FOLDER1", "link"),
    (10, None, "truncated"),
])
async def test_dossier_bundle_links_drive_above_photo_limit(tmp_path, photo_count, drive_url, expected):
    """Over 9 photos the dossier links the Drive images folder instead of uploading them."""
    import json
    from PIL import Image
    from services import telegram_polling
    from services.telegram_polling import TelegramPollingRunner

    calls = []

    class Resp:
        status_code = 200
        text = ""
        def __init__(self, payload): self._p = payload
        def json(self): return {"result": self._p}

    class Client:
        async def post(self, url, data=None, files=None, json=None, timeout=None):
            calls.append((url.rsplit("/", 1)[-1], data or {}, files or {}))
            if url.endswith("sendMediaGroup"):
                return Resp([{"message_id": 1}, {"message_id": 2}])
            return Resp({"message_id": 7})

    async def fake_url(_pool, _sid):
        return drive_url

    paths = []
    for index in range(photo_count):
        path = os.path.join(str(tmp_path), f"slot_{index}.jpg")
        Image.new("RGB", (40, 40), (index * 20, 90, 140)).save(path)
        paths.append(path)

    runner = TelegramPollingRunner(bot_token="123:abc")
    async def get_client(): return Client()
    runner.get_client = get_client
    with patch.object(telegram_polling, "submission_images_folder_url", fake_url):
        ok = await runner.send_dossier_bundle(
            1, "k", {"submission_id": "VN-CAP", "photo_paths": paths}, "cap", b"{}",
            {"inline_keyboard": [[{"text": "Duyệt", "callback_data": "admin_req_approve:VN-CAP"}]]},
        )

    assert ok
    methods = [call[0] for call in calls]
    document = next(call for call in calls if call[0] == "sendDocument")
    assert "admin_req_approve" in document[1]["reply_markup"]
    group = next((call for call in calls if call[0] == "sendMediaGroup"), None)
    group_caption = json.loads(group[1]["media"])[0].get("caption") if group else ""

    if expected == "link":
        assert methods == ["sendDocument"]
        assert "https://drive.google.com/drive/folders/FOLDER1" in document[1]["caption"]
        assert "10 ảnh" in document[1]["caption"]
    elif expected == "truncated":
        assert methods == ["sendMediaGroup", "sendDocument"]
        assert len(json.loads(group[1]["media"])) == 9
        assert "9/10" in group_caption
    else:
        assert methods == ["sendMediaGroup", "sendDocument"]
        assert len(json.loads(group[1]["media"])) == 9
        assert group_caption == "cap"


@pytest.mark.asyncio
async def test_submission_images_folder_url_prefers_the_images_tree():
    """The ledger lookup returns the Images/<submission> folder, else nothing."""
    from services.telegram_polling import submission_images_folder_url

    queries = []

    class Cursor:
        def __init__(self, row): self._row = row
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def execute(self, sql, params=()): queries.append((sql, params))
        async def fetchone(self): return self._row

    class Conn:
        def __init__(self, row): self._row = row
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def cursor(self): return Cursor(self._row)

    class Pool:
        def __init__(self, row): self._row = row
        def connection(self): return Conn(self._row)

    url = await submission_images_folder_url(Pool(("FOLDER1",)), "vn-1")
    assert url == "https://drive.google.com/drive/folders/FOLDER1"
    assert "'Images'" in queries[0][0] and queries[0][1] == ("vn-1",)

    assert await submission_images_folder_url(Pool(None), "VN-1") is None
    assert await submission_images_folder_url(None, "VN-1") is None
    assert await submission_images_folder_url(Pool(("F",)), "") is None


@pytest.mark.asyncio
async def test_decision_recipient_follows_tg_response_source():
    """Form setting chooses the Telegram verdict recipient: none, CTV, or a question's answer."""
    from unittest.mock import patch
    from main import FORM_REGISTRY
    from services.form_engine import FormSchema, FormQuestion, FormSettings, QuestionType
    from services.telegram_polling import TelegramPollingRunner

    from core.state import FORM_REGISTRY as STATE_FORMS

    def make_schema(source, qid=None):
        schema = FormSchema(
            form_id="SIM_TG_SRC", title="TG source",
            settings=FormSettings(tg_response_source=source, tg_response_question_id=qid),
            questions=[FormQuestion(id="phone_q", title="So dien thoai", type=QuestionType.TEXT)],
        )
        STATE_FORMS["SIM_TG_SRC"] = schema
        return schema

    calls = []
    async def fake_dispatch(**kwargs):
        calls.append(kwargs)
    async def fake_dossier(sub_id, db_pool=None):
        return {
            "submission_id": sub_id, "form_id": "SIM_TG_SRC", "status": "APPROVED",
            "collaborator_id": "CTV01", "driver_phone": "0912345678", "driver_name": "A",
            "raw_data": {"form_data": {"phone_q": "0912345678"}},
            "submitted_at": "2021-01-01T00:00:00+00:00", "moderated_at": "2099-01-01T00:00:00+00:00",
        }

    runner = TelegramPollingRunner(bot_token="mock")
    try:
        with patch("services.telegram_polling.query_submission_dossier", side_effect=fake_dossier), \
             patch("routers.survey.dispatch_submission_telegram_notifications", side_effect=fake_dispatch):
            FORM_REGISTRY["SIM_TG_SRC"] = make_schema("ctv")
            await runner._notify_parties_of_decision("S1", "APPROVED")
            assert calls[-1]["collaborator_id"] == "CTV01"

            FORM_REGISTRY["SIM_TG_SRC"] = make_schema("question", "phone_q")
            await runner._notify_parties_of_decision("S2", "APPROVED")
            assert calls[-1]["collaborator_id"] == "0912345678"
            assert calls[-1]["recipient_kind"] == "driver"
            assert calls[-1]["submitted_at"] == "2021-01-01T00:00:00+00:00"

            before = len(calls)
            FORM_REGISTRY["SIM_TG_SRC"] = make_schema("none")
            await runner._notify_parties_of_decision("S3", "APPROVED")
            assert len(calls) == before
    finally:
        FORM_REGISTRY.pop("SIM_TG_SRC", None)
        STATE_FORMS.pop("SIM_TG_SRC", None)


def test_editor_exposes_tg_response_source_setting():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "static" / "ui" / "form_editor.html").read_text(encoding="utf-8")
    assert 'id="tgResponseSource"' in src
    assert 'id="tgResponseQuestion"' in src
    assert 'value="none"' in src and 'value="ctv"' in src and 'value="question"' in src
    assert "tg_response_source:" in src
    assert "tg_response_question_id:" in src


@pytest.mark.asyncio
async def test_ctv_bot_command_menu_includes_forms_without_privacy():
    from services.telegram_polling import DriverBotRunner

    runner = DriverBotRunner(bot_token="123:abc")
    payloads = []
    class Resp:
        status_code = 200
        text = ""
        def json(self): return {"ok": True}
    class Client:
        async def post(self, url, json=None, timeout=None):
            payloads.append((url, json))
            return Resp()
    async def get_client(): return Client()
    runner.get_client = get_client

    await runner.configure_bot_commands()
    url, payload = payloads[0]
    names = [c["command"] for c in payload["commands"]]
    assert url.endswith("/setMyCommands")
    assert "forms" in names and "privacy" not in names


@pytest.mark.asyncio
async def test_submission_notification_uses_configured_question_source():
    import io, os, shutil
    from unittest.mock import AsyncMock, patch
    from PIL import Image
    from main import FORM_REGISTRY
    from services.form_engine import (
        FormSchema, FormQuestion, FormSettings, QuestionType, ImageCheckConfig,
    )

    fid = "SIM_TG_SUBMIT_SRC"
    FORM_REGISTRY[fid] = FormSchema(
        form_id=fid, title="TG submit source", settings=FormSettings(
            decree13_required=False, is_test_mode=True,
            tg_response_source="question", tg_response_question_id="phone_q",
            image_checks=ImageCheckConfig(enable_blur_check=False, enable_ai_vision=False)),
        questions=[
            FormQuestion(id="phone_q", title="So dien thoai", type=QuestionType.TEXT, required=False),
            FormQuestion(id="cam", title="Anh", type=QuestionType.CAMERA_WEBRTC, required=True,
                image_slots=[{"slot": 0, "title": "A", "required": True}],
                min_images=1, max_images=1, exact_image_count=1),
        ],
    )
    buf = io.BytesIO(); Image.new("RGB", (80, 80), (20, 90, 150)).save(buf, format="JPEG")

    captured = {}
    async def fake_dispatch(**kwargs):
        captured.update(kwargs)

    sub_id = None
    try:
        with patch("routers.survey.dispatch_submission_telegram_notifications", side_effect=fake_dispatch), \
             patch("main.dispatch_submission_telegram_notifications", side_effect=fake_dispatch), \
             patch("routers.survey.send_telegram_notification", new=AsyncMock()), \
             patch("routers.survey.dispatch_action_checklist", new=AsyncMock()):
            body = TestClient(app).post(
                "/api/v1/survey/submit-webrtc",
                data={"form_id": fid, "is_test_mode": "true", "bypass_img_check": "true",
                      "phone_q": "0961281310", "ctv_code": "CTV01", "cam_0": "uploaded_0"},
                files={"cam__file_0": ("a.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")},
            ).json()
        sub_id = body.get("submission_id") or (body.get("data") or {}).get("submission_id")
        assert captured.get("collaborator_id") == "0961281310", captured
        assert captured.get("recipient_kind") == "driver"
        assert captured.get("ctv_ref") == "CTV01", captured
    finally:
        if sub_id:
            shutil.rmtree(os.path.join(os.getcwd(), "data", "submission_photos", sub_id), ignore_errors=True)
        FORM_REGISTRY.pop(fid, None)


@pytest.mark.asyncio
async def test_direct_telegram_send_uses_configured_http_client(monkeypatch):
    """Regression: send_telegram_direct_message must not reference an undefined client."""
    from routers import survey as survey_router

    class Resp:
        status_code = 200
        text = ""
    class Client:
        def __init__(self): self.calls = []
        async def post(self, url, json=None):
            self.calls.append((url, json))
            return Resp()

    client = Client()
    monkeypatch.setattr(survey_router, "get_http_client", lambda: client)
    ok = await survey_router.send_telegram_direct_message("123:abc", 8094247707, "hello")
    assert ok is True
    assert client.calls and client.calls[0][1]["chat_id"] == 8094247707


def test_retry_link_keeps_original_ctv_ref_for_driver_recipient():
    """Rejected-submission retry link must carry the original CTV ref, not the phone recipient."""
    from routers.survey import _build_retry_form_link

    link = _build_retry_form_link("TB_GSM_2026_Nov_Test", "TUAN_LM_HCM_001", "0961281310", "driver")
    assert link.startswith("https://survey.trustbridgevn.com/forms/TB_GSM_2026_Nov_Test?ref=")
    assert "0961281310" not in link
    assert "TUAN_LM_HCM_001" in link.upper()

    # CTV recipients keep using the collaborator when no explicit ref is supplied.
    ctv_link = _build_retry_form_link("TB_GSM_2026_Nov_Test", None, "DUNG_NV_HCM_002", "ctv")
    assert "DUNG_NV_HCM_002" in ctv_link.upper()


def test_simulated_user_submission_reaches_provider_dispatch():
    """A real multipart user submission must hand off to the provider boundary once."""
    from unittest.mock import AsyncMock
    from main import FORM_REGISTRY

    db = _FakeSubmissionDB()
    form_id = "SIM-PROVIDER-DISPATCH"
    data, files = _register_persist_probe_form(form_id)
    dispatch = AsyncMock()
    try:
        with patch.dict(os.environ, {"MOCK_AI": "1"}), \
             patch("routers.survey.get_db_pool", return_value=db), \
             patch("routers.survey.dispatch_action_checklist", new=dispatch):
            with _submission_test_client() as client:
                response = client.post("/api/v1/survey/submit-webrtc", data=data, files=files)

        assert response.status_code == 200
        body = response.json()
        dispatch.assert_awaited_once()
        kwargs = dispatch.await_args.kwargs
        assert kwargs["submission_id"] == body["submission_id"]
        assert kwargs["is_test_mode"] is False
        assert kwargs["form_id"] == form_id
    finally:
        FORM_REGISTRY.pop(form_id, None)


def test_survey_copy_button_is_added_once_and_preserves_reply_keyboards():
    from services.telegram_ui import with_survey_copy
    from services.templates_vn import render_appeal_keyboard

    inline = {"inline_keyboard": [[{"text": "Mở", "callback_data": "admin_review:VN-1"}]]}
    once = with_survey_copy(inline, "VN-1")
    twice = with_survey_copy(once, "VN-1")
    copy_rows = [
        row for row in twice["inline_keyboard"]
        if row == [{"text": "Sao chép mã phiếu", "copy_text": {"text": "VN-1"}}]
    ]
    assert len(copy_rows) == 1

    reply_keyboard = {"keyboard": [[{"text": "Khảo sát của tôi"}]], "resize_keyboard": True}
    assert with_survey_copy(reply_keyboard, "VN-1") == reply_keyboard
    assert render_appeal_keyboard("VN-2")["inline_keyboard"][-1][0]["copy_text"] == {"text": "VN-2"}


def test_driver_verdict_messages_include_copy_button():
    from routers.survey import render_driver_submission_verdict_message

    for status in ("APPROVED", "REJECTED", "MANUAL_REVIEW"):
        _, markup = render_driver_submission_verdict_message("VN-TEST-COPY", status)
        buttons = [
            button
            for row in (markup or {}).get("inline_keyboard", [])
            for button in row
            if isinstance(button, dict) and button.get("copy_text")
        ]
        assert buttons and buttons[0]["copy_text"] == {"text": "VN-TEST-COPY"}


@pytest.mark.asyncio
async def test_admin_flagged_survey_payload_has_copy_button(monkeypatch):
    from services import admin_moderator
    from config import settings

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setattr(settings, "TELEGRAM_ADMIN_GROUP_ID", "-100123")

    class Client:
        payload = None

        async def post(self, _url, json=None):
            self.payload = json

    client = Client()
    await admin_moderator.forward_flagged_survey_to_admin_group(
        "VN-FLAG-COPY", "0900000000", "Test Driver", "TEST_FLAG", "test reason",
        http_client=client,
    )
    buttons = [
        button
        for row in client.payload["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert any(button.get("copy_text") == {"text": "VN-FLAG-COPY"} for button in buttons)
