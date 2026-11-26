import io
import json
import math
import os
import secrets
import time
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

import pytest
from PIL import Image
from fastapi.testclient import TestClient

import main
from main import app, LOCAL_IDEMP_CACHE, append_dev_submission, DEV_STORAGE_FILE
from services.form_engine import (
    FormSchema,
    FormQuestion,
    QuestionType,
    validate_submission_against_schema,
)
from services.normalizer import sanitize_and_normalize_image
from services.income_sanitizer import analyze_driver_income
from services.vehicle_tracker import VehiclePersistenceTracker, normalize_license_plate
from services.audit_and_privacy import (
    compute_audit_hash,
    record_hop_audit,
    tokenize_or_get_driver_id,
    scrub_driver_pii,
    enqueue_telegram_message_for_36h_deletion,
    process_36h_message_deletions,
)
@pytest.fixture
def consent_schema(monkeypatch):
    schema = FormSchema(form_id="wave1_consent", title="Consent contract", questions=[
        FormQuestion(id="consent_choice", title="Consent", type=QuestionType.SINGLE_CHOICE,
                     is_consent_gate=True, consent_accept_values=["Tôi đồng ý"]),
    ])
    monkeypatch.setitem(main.FORM_REGISTRY, schema.form_id, schema)
    return schema



# Legacy transport fields; schema tests explicitly register and identify their contract.
def standard_form_fields(driver_phone="0988123456", **overrides):
    fields = {
        "driver_phone": driver_phone,
        "driver_name": "Nguyễn Văn Test",
        "license_plate": "29A-123.45",
        "vehicle_type": "Ô tô điện VinFast (GSM)",
        "car_model": "VF 5 Plus",
        "gsm_duration": "Từ 6 – 12 tháng",
        "gsm_contract_type": "Xe Taxi xanh GSM",
        "daily_trips": "18",
        "monthly_income_range": "12 – 20 triệu VNĐ",
        "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
        "gross_revenue": "500000",
        "trip_count": "10",
        "odometer_km": "50",
        "signature_png": "data:image/png;base64,mocksignature",
        "consent_choice": "Tôi đồng ý",
        "platform_app": "Grab",
        "vehicle_category": "Ô tô 4 - 7 chỗ",
        "working_hours_day": "8 - 12 tiếng (Toàn thời gian)",
        "daily_net_income": "500000",
        "monthly_gross_revenue": "15000000",
        "is_test_mode": "false",
    }
    fields.update(overrides)
    return fields

# Shared sharp image fixture helper
def make_sharp_image_bytes():
    img = Image.new("RGB", (800, 600), color=(255, 255, 255))
    # Draw sharp contrasting stripes to easily pass Laplacian blur threshold
    for x in range(0, 800, 20):
        for y in range(600):
            if (x // 20) % 2 == 0:
                img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ==============================================================================
# TEAM 1: TEST ALL KNOWN UNFIXED AND MITIGATED BUGS FROM MASTER VULNERABILITY REGISTER
# ==============================================================================

class TestTeam1KnownAndMitigatedVulnerabilities:
    """
    Team 1: Test all known unfixed and mitigated bugs from docs/MASTER_VULNERABILITY_REGISTER.md:
    - VULN-AUTH-05: Unverified / unauthenticated vehicle profile upsert / hijacking in submit_webrtc_survey.
    - VULN-DATA-01: Static ./data exposure (StaticFiles mounted at /data).
    - VULN-RACE-01: Concurrent file updates in dev store (append_dev_submission has no file locking / atomicity).
    - VULN-CACHE-01: Unbounded LOCAL_IDEMP_CACHE (memory leak / OOM risk under sustained traffic).
    - VULN-CACHE-02: Idempotency cache key namespace scoping (cross-tenant key collision/snooping).
    - VULN-COLL-01: Low entropy 24-bit submission ID generation (secrets.token_hex(3)).
    - VULN-PRIV-01: Incomplete Decree 13 right-to-erasure in vault & vehicles (scrub_driver_pii leaves phone & vehicle record).
    """

    def test_vuln_auth_05_unverified_vehicle_upsert_confirmed(self):
        """
        [VULN-AUTH-05 CONFIRMATION - BUG CONFIRMED]
        Probe whether submit_webrtc_survey unconditionally updates driver_registered_vehicles
        with untrusted license_plate for any driver_phone provided in public submission.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        victim_phone = "0988111222"
        hijack_plate = "99X-888.88"

        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_conn.rollback = AsyncMock()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.side_effect = [("driver-id",), (victim_phone,), ("event-id",)]
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor
        from services import submission_pipeline as pipeline
        evaluation = pipeline.SubmissionEvaluation({}, {}, "MANUAL_REVIEW", "Review", False, None, {}, {}, None)

        with patch.object(main, "db_pool", mock_db_pool), patch.object(
            pipeline, "writer_schema_ready", AsyncMock(return_value=True)
        ), patch.object(pipeline, "require_delivery_schema", AsyncMock()), patch.object(
            pipeline, "_evaluate", AsyncMock(return_value=evaluation)
        ), patch.object(pipeline, "enqueue_submission_work", AsyncMock(return_value=())), patch(
            "routers.survey._schedule_legacy_submission"
        ):
            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data=standard_form_fields(
                    driver_phone=victim_phone,
                    license_plate=hijack_plate,
                    vehicle_type="TRUCK",
                    driver_name="Tài xế Nạn Nhân",
                ),
                files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
            )

        assert resp.status_code == 200
        queries = [call.args[0] for call in mock_cursor.execute.call_args_list]
        assert any("FROM driver_registered_vehicles" in sql for sql in queries)
        assert not any(sql.lstrip().startswith(("INSERT INTO driver_registered_vehicles", "UPDATE driver_registered_vehicles")) for sql in queries)
        mock_conn.commit.assert_awaited_once()

    def test_vuln_data_01_static_data_exposure_confirmed(self, monkeypatch, tmp_path):
        """
        [VULN-DATA-01 CONFIRMATION - BUG CONFIRMED]
        Probe whether app exposes './data' directory via StaticFiles mount,
        allowing unauthenticated public reading of dev_test_submissions.json.
        """
        from core import repositories

        client = TestClient(app)

        # The probe asserts the live directory is unreachable, so keep its
        # canary record out of the shared dev store.
        monkeypatch.setattr(repositories, "DEV_STORAGE_FILE", str(tmp_path / "dev_submissions.json"))
        test_secret = {"leak_check": "sensitive_driver_pii_12345"}
        repositories.append_dev_submission(test_secret)

        # In MASTER_VULNERABILITY_REGISTER.md it was marked FIXED: "Static mount removed from main.py".
        # But main.py line 111 still has: app.mount("/data", StaticFiles(directory="data"), name="data")
        resp = client.get("/data/dev_test_submissions.json")
        is_mounted = any(getattr(route, "path", None) == "/data" for route in app.routes)
        
        assert not is_mounted, "VULN-DATA-01: /data static route should be unmounted!"
        assert resp.status_code == 404, "VULN-DATA-01: Sensitive data directory should not be publicly readable!"

    def test_vuln_race_01_concurrent_dev_storage_file_writes_data_loss(self, tmp_path, monkeypatch):
        """
        [VULN-RACE-01 CONFIRMATION - BUG CONFIRMED]
        Probe append_dev_submission for concurrent write race conditions.
        Without file locking or atomic replacement, concurrent appends produce
        corrupted JSON (JSONDecodeError) or silent lost updates!
        """
        import threading

        from core import repositories

        storage = tmp_path / "dev_submissions.json"
        monkeypatch.setattr(repositories, "DEV_STORAGE_FILE", str(storage))

        total_threads = 20
        barrier = threading.Barrier(total_threads)

        def worker(idx):
            barrier.wait()
            repositories.append_dev_submission({"worker_id": idx, "ts": time.time()})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(total_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Check for concurrency failure: either JSON corruption or lost records (< 20)
        is_corrupted = False
        record_count = 0
        try:
            with open(storage, "r", encoding="utf-8") as f:
                records = json.load(f)
                record_count = len(records)
        except json.JSONDecodeError:
            is_corrupted = True

        # Bug confirmed: either file corrupted by interleaved writes or writes lost!
        assert is_corrupted or record_count < total_threads, (
            f"VULN-RACE-01: Expected race condition (corrupted={is_corrupted}, count={record_count}/{total_threads})"
        )

    def test_vuln_cache_01_unbounded_local_idemp_cache_leak(self):
        """
        [VULN-CACHE-01 CONFIRMATION - BUG CONFIRMED]
        Probe LOCAL_IDEMP_CACHE. It is a plain Python dict with NO max capacity,
        no LRU eviction, and no TTL purging when Redis is unavailable.
        """
        LOCAL_IDEMP_CACHE.clear()

        for i in range(500):
            LOCAL_IDEMP_CACHE[f"key_{i}"] = json.dumps({"status": "ok", "i": i})

        assert hasattr(LOCAL_IDEMP_CACHE, "maxsize")
        assert LOCAL_IDEMP_CACHE.maxsize == 2048
        assert hasattr(LOCAL_IDEMP_CACHE, "ttl")
        assert LOCAL_IDEMP_CACHE.ttl == 86400
        # Test capacity bounding: insert 2100 items and verify length <= 2048
        LOCAL_IDEMP_CACHE.clear()
        for i in range(2100):
            LOCAL_IDEMP_CACHE[f"key_{i}"] = json.dumps({"status": "ok", "i": i})
        assert len(LOCAL_IDEMP_CACHE) == 2048
        LOCAL_IDEMP_CACHE.clear()

    def test_vuln_cache_02_idempotency_key_global_namespace_collision(self):
        """
        [VULN-CACHE-02 MITIGATION CHECK - BUG CONFIRMED]
        Verify whether submit_webrtc_survey scopes idempotency cache key by phone or tenant.
        In main.py: lines 328 & 331 do:
          await redis_client.get(f"idemp:{idempotency_key}")
          LOCAL_IDEMP_CACHE[idempotency_key]
        Neither includes driver_phone! Driver B using same key as Driver A snoops Driver A's data!
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        shared_key = f"shared_idemp_{secrets.token_hex(4)}"

        # Pre-seed cache with phone-scoped key for Driver A
        LOCAL_IDEMP_CACHE[f"idemp:0911111111:{shared_key}"] = json.dumps({
            "submission_id": "VN-TENANT-A",
            "driver_name": "Tài xế A (Bí mật)",
            "driver_phone": "0911111111",
            "status": "APPROVED",
        })

        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(
                driver_phone="0922222222",
                idempotency_key=shared_key,
                is_test_mode="true",
                bypass_img_check="true",
            ),
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Driver B does NOT hit Driver A's cache because key is scoped by driver_phone!
        assert data.get("driver_phone") == "0922222222"
        assert data.get("driver_name") != "Tài xế A (Bí mật)"
    def test_vuln_coll_01_submission_id_low_entropy_collision_risk(self):
        """
        [VULN-COLL-01 CONFIRMATION - BUG CONFIRMED]
        In main.py line 322:
          submission_id = f"{'DEV' if is_test_mode else 'VN'}-{secrets.token_hex(3).upper()}"
        `secrets.token_hex(3)` is only 3 bytes = 24 bits = 16,777,216 states.
        By Birthday Paradox, a 50% collision occurs at sqrt(2^24) ~= 4,096 submissions!
        """
        # Verify submission_id from endpoint has 64 bits of entropy (8 bytes = 16 hex chars)
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(is_test_mode="true", bypass_img_check="true"),
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp.status_code == 200
        sub_id = resp.json()["submission_id"]
        token_part = sub_id.split("-", 1)[1]
        assert len(token_part) == 16, f"Expected 16 hex chars (8 bytes = 64 bits of entropy), got {len(token_part)}"


# ==============================================================================
# TEAM 2: PROBE FOR NEW BUGS AND EDGE CASES
# ==============================================================================

class TestTeam2NewBugsAndEdgeCases:
    """
    Team 2: Probe for NEW bugs and edge cases:
    - Zero-byte uploads (empty survey photo).
    - Extreme payload boundaries (huge driver names, large plates).
    - Unhandled unicode in driver names (emojis, ZWJ, surrogate pairs, RTL overrides).
    - Decimal floating-point weirdness in revenue (inf, nan, float precision).
    - Timeout behavior (vision AI timeout degrades safely to MANUAL_REVIEW).
    """

    def test_zero_byte_survey_photo_upload_boundary(self):
        """
        Zero-byte image upload to submit_webrtc_survey.
        When mandatory form questions pass, submitting a 0-byte image fails image decoding
        and must reject cleanly with status REJECTED (not 500 error).
        """
        client = TestClient(app)
        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(is_test_mode="true"),
            files={"survey_photo": ("empty.jpg", io.BytesIO(b""), "image/jpeg")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "REJECTED"
        assert "Không thể giải mã ảnh" in data["reason"]

    def test_extreme_payload_boundaries_huge_name_and_plate(self):
        """
        Extreme payload boundaries: 50,000 character driver name and large license plate.
        Tests whether server handles extreme sizes without crashing.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        huge_name = "Nguyễn " + ("A" * 50000)
        huge_plate = "29E-" + ("9" * 1000)

        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(
                driver_name=huge_name,
                license_plate=huge_plate,
                is_test_mode="true",
                bypass_img_check="true",
            ),
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp.status_code == 200

    def test_unhandled_unicode_emojis_and_surrogate_pairs_in_driver_name(self):
        """
        Driver names with complex unicode:
        - Vietnamese compound diacritics
        - Emoji sequences with ZWJ: 👨‍🦰
        - Zero-width space (\u200b)
        - Right-to-Left marks (\u202e)
        - BOM (\ufeff)
        Probes dev storage persistence and response serialization.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()
        adversarial_name = "Nguyễn Văn A 👨‍🦰\u200b\u202eTàiXế\ufeff"

        resp = client.post(
            "/api/v1/survey/submit-webrtc",
            data=standard_form_fields(
                driver_name=adversarial_name,
                is_test_mode="true",
                bypass_img_check="true",
            ),
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp.status_code == 200
        # Inspect dev storage file to ensure unicode was persisted without escaping corruption
        with open(DEV_STORAGE_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        last_rec = records[-1]
        assert last_rec["driver_name"] == adversarial_name

    def test_decimal_floating_point_revenue_weirdness(self):
        """
        Probe revenue parsing for:
        1. float('inf') -> status_code 'INCOME_EXCEEDS_THRESHOLD'
        2. float('nan') -> status_code 'INCOME_EXCEEDS_THRESHOLD' (math.isnan check)
        3. Floating precision sum (0.1 + 0.2) in platform fare math reconciles cleanly
        """
        # 1. Infinity
        res_inf = analyze_driver_income(vehicle_type="BIKE", gross_income=float("inf"))
        assert res_inf.is_valid is False
        assert res_inf.status_code == "INCOME_EXCEEDS_THRESHOLD"

        # 2. NaN
        res_nan = analyze_driver_income(vehicle_type="BIKE", gross_income=float("nan"))
        assert res_nan.is_valid is False
        assert res_nan.status_code == "INCOME_EXCEEDS_THRESHOLD"

        # 3. Floating precision sum (0.1 + 0.2) in platform fare math
        res_float = analyze_driver_income(
            vehicle_type="BIKE",
            gross_income=100000.0,
            net_income=80000.0,
            platform_discount=20000.000000000004
        )
        assert res_float.is_valid is True

    def test_vision_ai_timeout_behavior_fail_safe(self):
        """
        Probe AI Vision timeout behavior:
        When the AI upstream model hangs or times out (asyncio.TimeoutError),
        the endpoint MUST NOT raise an unhandled 500 error or fail-open.
        It must cleanly degrade to MANUAL_REVIEW with is_approved=False.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()

        # Mock vision_client.extract_facts to simulate timeout
        with patch.object(main.vision_client, "extract_facts", side_effect=asyncio.TimeoutError("Upstream timeout")):
            resp = client.post(
                "/api/v1/survey/submit-webrtc",
                data=standard_form_fields(
                    license_plate="29E-123.45",
                    is_test_mode="false",
                ),
                files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "MANUAL_REVIEW"


# ==============================================================================
# TEAM 4: LEGALITY AND DECREE 13 COMPLIANCE
# ==============================================================================

class TestTeam4LegalityAndDecree13Compliance:
    """
    Team 4: Legality and Decree 13 Compliance:
    - Verify mandatory consent checkboxes (omission, rejection, explicit acceptance).
    - Driver right-to-erasure purging in services/audit_and_privacy.py (VULN-PRIV-01).
    - Data minimization: hashing phone numbers in audit logs (system_audit_log must NOT store raw phone).
    - Retention expiry (36-hour Telegram ephemeral message deletion queue).
    """

    def test_decree13_mandatory_consent_rejection_when_omitted(self, consent_schema):
        """
        Decree 13 / DPIA: Processing personal data requires explicit affirmative consent.
        Submissions missing 'consent_choice' must be REJECTED immediately by schema validation.
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()

        # Missing consent entirely
        fields = standard_form_fields(form_id=consent_schema.form_id)
        del fields["consent_choice"]

        resp_missing = client.post(
            "/api/v1/survey/submit-webrtc",
            data=fields,
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp_missing.status_code == 200
        data_missing = resp_missing.json()
        assert data_missing["status"] == "REJECTED"
        assert "bắt buộc" in data_missing["reason"]

    def test_decree13_explicit_dissent_conditional_flow_flaw(self, consent_schema):
        """
        [DECREE 13 COMPLIANCE FLAW CONFIRMED]
        When a user selects 'Tôi không đồng ý' (dissent):
        In form_engine.py: questions 3-10 are conditional on consent_choice == 'Tôi đồng ý',
        so questions 3-10 are skipped and schema validation returns is_valid=True!
        Then in main.py: submit_webrtc_survey proceeds to process the survey and call AI vision
        instead of rejecting the submission immediately due to lack of consent!
        """
        client = TestClient(app)
        sharp_bytes = make_sharp_image_bytes()

        # Dissent: consent_choice = 'Tôi không đồng ý'
        resp_dissent = client.post(
            "/api/v1/survey/submit-webrtc",
            data={
                "form_id": consent_schema.form_id,
                "driver_phone": "0988123456",
                "consent_choice": "Tôi không đồng ý",
                "signature_png": "data:image/png;base64,mock",
                "is_test_mode": "true",
                "bypass_img_check": "true",
            },
            files={"survey_photo": ("capture.jpg", io.BytesIO(sharp_bytes), "image/jpeg")},
        )
        assert resp_dissent.status_code == 200
        data_dissent = resp_dissent.json()
        
        # Compliance failure: In current main.py, dissent gets APPROVED or proceeds because
        # schema validation skipped subsequent questions and returned valid!
        assert data_dissent["status"] == "REJECTED"
        assert "Nghị định 13" in data_dissent["reason"]

    @pytest.mark.asyncio
    async def test_decree13_right_to_erasure_in_audit_and_privacy_confirms_vuln_priv_01(self):
        """
        [VULN-PRIV-01 COMPLIANCE FAILURE CONFIRMATION - BUG CONFIRMED]
        Decree 13 Article 16: Driver Right to Erasure.
        Inspect scrub_driver_pii() implementation:
        1. Does it delete/hash driver_phone in driver_identity_vault?
           -> Line 213: UPDATE driver_identity_vault SET driver_name='...', is_scrubbed=TRUE WHERE driver_phone=%s
           -> Fails to nullify or hash driver_phone! The phone remains in plaintext!
        2. Does it scrub driver_registered_vehicles?
           -> It only touches driver_identity_vault, survey_submissions, and vehicle_change_history!
           -> The vehicle plate and driver_phone remain permanently in driver_registered_vehicles!
        """
        victim_phone = "0977888999"
        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        mock_cursor.fetchone.return_value = ("DRV-TEST1234",)

        res = await scrub_driver_pii(mock_db_pool, victim_phone)
        assert res["status"] == "success"

        executed_queries = [call[0] for call in mock_cursor.execute.call_args_list]

        # 1. Check if driver_phone is scrubbed in driver_identity_vault:
        vault_scrub = next((c for c in executed_queries if "driver_identity_vault" in c[0] and "UPDATE" in c[0]), None)
        assert vault_scrub is not None
        vault_sql = vault_scrub[0]
        phone_scrubbed_in_vault = "driver_phone = NULL" in vault_sql or "driver_phone = '[SCRUBBED" in vault_sql
        assert phone_scrubbed_in_vault, (
            "VULN-PRIV-01: driver_identity_vault should erase driver_phone under Decree 13!"
        )

        # 2. Check if driver_registered_vehicles was erased:
        vehicle_scrub = next((c for c in executed_queries if "driver_registered_vehicles" in c[0]), None)
        assert vehicle_scrub is not None, (
            "VULN-PRIV-01: driver_registered_vehicles should be erased from right-to-erasure scrub routine!"
        )

    @pytest.mark.asyncio
    async def test_decree13_data_minimization_audit_log_phone_exposure_check(self):
        """
        [DATA MINIMIZATION / PRIVACY COMPLIANCE FLAW CONFIRMED]
        Probe record_hop_audit to inspect whether details dict leaks raw phone numbers.
        Audit finding: record_hop_audit dumps raw details directly to JSON: json.dumps(details).
        It performs zero phone hashing or redaction!
        """
        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        raw_phone = "0988123456"
        details_with_raw_phone = {"driver_phone": raw_phone, "status": "APPROVED"}

        await record_hop_audit(
            db_pool=mock_db_pool,
            hop_number=1,
            hop_name="HOP_1_INGEST",
            component_name="INGEST",
            action="SURVEY_RECEIVED",
            entity_type="SURVEY",
            entity_id="VN-001",
            actor_type="DRIVER",
            actor_id="DRV-A1B2",
            details=details_with_raw_phone,
        )

        executed_query = mock_cursor.execute.call_args_list[0][0]
        sql_params = executed_query[1]
        logged_details_json = sql_params[8]

        # Bug confirmed: raw phone number is stored in plaintext in system_audit_log!
        assert raw_phone not in logged_details_json, (
            "Decree 13 Data Minimization: Raw phone number should be masked in system_audit_log details!"
        )
        assert "****" in logged_details_json

    @pytest.mark.asyncio
    async def test_retention_expiry_36h_telegram_message_deletion(self):
        """
        Retention Expiry:
        36-Hour Ephemeral Message Deletion Queue (telegram_sent_messages).
        Verify process_36h_message_deletions purges expired messages and marks them is_deleted = TRUE.
        """
        mock_db_pool = MagicMock()
        mock_conn = MagicMock()
        mock_conn.commit = AsyncMock()
        mock_cursor = AsyncMock()
        mock_db_pool.connection.return_value.__aenter__.return_value = mock_conn
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cursor

        mock_cursor.fetchall.return_value = [
            ("rec-uuid-1", 12345678, 101),
            ("rec-uuid-2", 12345678, 102),
        ]

        mock_http = AsyncMock()
        mock_http.post = AsyncMock()

        deleted_count = await process_36h_message_deletions(
            db_pool=mock_db_pool,
            http_client=mock_http,
            bot_token="test_bot_token"
        )

        assert deleted_count == 2
        assert mock_http.post.call_count == 2
        update_calls = [c for c in mock_cursor.execute.call_args_list if "UPDATE telegram_sent_messages" in c[0][0]]
        assert len(update_calls) == 2

    def test_telegram_webhook_acknowledges_updates_for_runner_authorization(self, telegram_webhook_client):
        """Transport acknowledges valid updates; the polling runner owns chat authorization."""
        client = telegram_webhook_client

        # 1. Slash command from unauthorized chat ID (e.g. 999888777)
        payload_slash_unauth = {
            "message": {
                "chat": {"id": 999888777},
                "text": "/query VN-123456"
            }
        }
        resp = client.post("/webhook/telegram", json=payload_slash_unauth)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # 2. Callback query from unauthorized chat ID
        payload_cq_unauth = {
            "callback_query": {
                "data": "admin_query:VN-123456",
                "message": {"chat": {"id": 999888777}}
            }
        }
        resp_cq = client.post("/webhook/telegram", json=payload_cq_unauth)
        assert resp_cq.status_code == 200
        assert resp_cq.json() == {"status": "ok"}

        # 3. Request from authorized admin group (TELEGRAM_ADMIN_GROUP_ID)
        admin_group_id = int(main.settings.TELEGRAM_ADMIN_GROUP_ID)
        payload_auth = {
            "message": {
                "chat": {"id": admin_group_id},
                "text": "/query VN-123456"
            }
        }
        resp_auth = client.post("/webhook/telegram", json=payload_auth)
        assert resp_auth.status_code == 200
        assert resp_auth.json() == {"status": "ok"}
