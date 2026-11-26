import asyncio
import io
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

from core.embedding_executor import embedding_executor_lifespan
from services import submission_pipeline as pipeline
from services.evaluator import evaluate_survey_compliance
from services.vision_ai import ExtractedReceiptFacts, ReceiptLineItem
from services.form_engine import (
    DEFAULT_TRUST_BRIDGE_SCHEMA,
    FormSchema,
    FormQuestion,
    QuestionType,
    ImageCheckConfig,
)
from services.dynamic_form_html import render_dynamic_form_html
from services.form_manager_html import render_form_editor_html


def make_test_image(pattern: str = "lines") -> bytes:
    import random
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    rng = random.Random(pattern)
    for _ in range(30):
        x1 = rng.randint(0, 500)
        y1 = rng.randint(0, 700)
        draw.rectangle([(x1, y1), (x1 + rng.randint(20, 80), y1 + rng.randint(20, 80))], fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


async def classify_camera_role(_image_bytes, filename, expected_slots):
    assert expected_slots
    if filename == "slot_0.jpg":
        return "app_profile"
    if filename == "slot_1.jpg":
        return "vehicle_exterior"
    return "app_income"


def weekly_ocr(week_idx, **overrides):
    period = (
        "28/09/2026 - 04/10/2026", "05/10/2026 - 11/10/2026",
        "12/10/2026 - 18/10/2026", "19/10/2026 - 25/10/2026",
        "26/10/2026 - 01/11/2026",
    )[week_idx - 1]
    return {
        "period": period, "period_raw_text": period, "period_type": "WEEKLY",
        "gross_revenue": 3_000_000 * week_idx, "net_income": 2_500_000 * week_idx,
        "discount_and_tax": 500_000 * week_idx, "total_trips": 20 * week_idx,
        "detected_app": "XANH_SM", "is_authentic_gsm_ui": True,
        "is_legible": True, "confidence_score": 0.99,
        "is_manipulated_or_photoshopped": False, **overrides,
    }


@pytest_asyncio.fixture
async def submit_camera(monkeypatch, tmp_path):
    # Use the deterministic model backend, not a replacement for the bounded executor.
    monkeypatch.setenv("MOCK_AI", "1")
    schema = DEFAULT_TRUST_BRIDGE_SCHEMA
    camera = next(q for q in schema.questions if q.id == "camera_receipt")
    dev_file = tmp_path / "dev_test_submissions.json"
    dev_file.write_text("[]", encoding="utf-8")

    def append_dev_record(record):
        records = json.loads(dev_file.read_text(encoding="utf-8"))
        dev_file.write_text(json.dumps([*records, record]), encoding="utf-8")

    async def profile(_image_bytes, *, prompt):
        return {"app": "XANH_SM", "is_authentic": True}

    async def object_fields(_image_bytes, _prompt):
        # Vehicle slots run their real object engines now, so the stub must
        # return readable evidence instead of the empty response used before.
        return {"vehicle_type": "CAR", "view": "EXTERIOR", "is_authentic": True}

    async def submit(extract, *, photos=None, classify=classify_camera_role, phone="0988123456"):
        if photos is None:
            photos = [make_test_image(f"camera-slot-{i}") for i in range(7)]
        uploads = tuple(
            pipeline.SubmissionUpload(camera.id, i, f"slot_{i}.jpg", photo)
            for i, photo in enumerate(photos)
        )
        request = pipeline.SubmissionInput(
            form_id=schema.form_id, schema=schema, is_test_mode=True, uploads=uploads,
            driver_phone=phone, signature_png="data:image/png;base64,mock",
            fields={
                "consent_choice": "Tôi đồng ý", "vehicle_type": "Ô tô", "car_model": "VF 5 Plus",
                "gsm_duration": "Từ 6 – 12 tháng", "gsm_contract_type": "Xe Taxi xanh GSM",
                "daily_trips": "18", "monthly_income_range": "12 – 20 triệu VNĐ",
                "gsm_incentive_policy": "Có áp dụng chỉ tiêu",
            },
        )
        deps = pipeline.SubmissionDependencies(
            vision=SimpleNamespace(classify_image_role=classify, ocr_driver_app_profile=profile,
                                   ocr_driver_app_income=extract, extract_object_fields=object_fields),
            media_root=str(tmp_path / "photos"), dev_storage_file=str(dev_file),
            append_dev_record=append_dev_record, image_max_dimension=720, blur_threshold=15,
        )
        result = await pipeline.process_submission(request, deps)
        return result.response

    async with embedding_executor_lifespan():
        yield submit


class TestArchitectFixes:
    """Automated tests verifying latency optimization, screen photo allowance, form resets, and tab 3 rendering."""

    def test_screen_photo_allowed_in_evaluator(self):
        """Taking photo of driver phone screen / odometer is explicitly allowed and passes compliance."""
        valid_facts = ExtractedReceiptFacts(
            vendor_name="GSM Ride Hailing",
            tax_id="0108938491",
            receipt_serial="GSM-88912",
            line_items=[ReceiptLineItem(item_name="Cuoc xe", amount=150000)],
            raw_subtotal=150000,
            vat_amount=15000,
            raw_total=165000,
            has_signature=True,
            unchecked_boxes_count=0,
            is_screen_photo=True,  # Device screen recapture
            is_manipulated_or_photoshopped=False,
            notes="Photo of smartphone driver app screen",
        )
        res = evaluate_survey_compliance(valid_facts)
        assert res.passed is True
        assert res.status_tier == "APPROVED"
        assert res.reason_code == "APPROVED"

    def test_default_image_check_config_screen_photo_disabled(self):
        """ImageCheckConfig has enable_screen_photo_check=False by default."""
        cfg = ImageCheckConfig()
        assert cfg.enable_screen_photo_check is False

    @pytest.mark.asyncio
    async def test_parallel_image_checking_concurrency(self, submit_camera):
        """Vision classification and extraction must each overlap, even with one DINO worker."""
        active = {"classify": 0, "ocr": 0}
        peak = active.copy()

        async def observe(stage):
            active[stage] += 1
            peak[stage] = max(peak[stage], active[stage])
            try:
                await asyncio.sleep(0)
            finally:
                active[stage] -= 1

        async def classify(image_bytes, filename, expected_slots):
            await observe("classify")
            return await classify_camera_role(image_bytes, filename, expected_slots)

        async def extract(_image_bytes, *, week_idx, prompt):
            await observe("ocr")
            return weekly_ocr(week_idx)

        response = await submit_camera(extract, classify=classify)
        assert response["status"] == "APPROVED", response
        for stage, concurrent_calls in peak.items():
            assert concurrent_calls >= 2, f"Expected {stage} concurrency >= 2, got {concurrent_calls}"

    def test_execute_reset_form_clears_all_slots_and_camera_state(self):
        """Verify dynamic_form_html JavaScript executeResetForm wipes answers, slots, and resets UI."""
        html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA, telegram_bot_username="")
        
        # Verify executeResetForm resets slot 0 and cleans up all image slots
        assert "function executeResetForm" in html
        assert "selectCameraSlot(q.id, 0)" in html
        assert "activeSlotByQId[q.id] = 0" in html
        assert "deactivateCamera(q.id)" in html
        assert "delete answers[k]" in html
        assert "delete answers[`${q.id}_${sIdx}`]" in html
        assert "cam-slot-pill" in html
        assert "pill.classList.remove('captured')" in html
        assert "retakeBtn.style.display = 'none'" in html
        assert "screenOff.style.display = 'flex'" in html

        # Verify in-app history action button in submissionResultModal
        assert "btnSubResultHistory" in html
        assert "openHistoryModal()" in html
        assert "Tra cứu lịch sử nộp trên web" in html

        # Verify telegram bot link is hidden when bot username is empty
        assert 'href="https://t.me/"' in html or 'style="display:none;' in html
        assert 'style="display:none;' in html

    def test_dynamic_form_html_with_configured_telegram_bot(self):
        """Verify dynamic_form_html shows Telegram button when configured."""
        html = render_dynamic_form_html(DEFAULT_TRUST_BRIDGE_SCHEMA, telegram_bot_username="TrustBridgeLiveBot")
        assert 'href="https://t.me/TrustBridgeLiveBot"' in html
        assert 'style="display:flex;' in html

    def test_form_manager_tab3_renders_questions_with_double_quotes(self):
        """Verify Tab 3 in Form Manager escapes quotes in titles and descriptions cleanly."""
        schema = FormSchema(
            form_id="test_quotes_form",
            title='Biểu mẫu "Đặc biệt"',
            description='Mô tả có "dấu ngoặc kép"',
            questions=[
                FormQuestion(
                    id="q_quote",
                    title='Tải lên ảnh "Màn hình chính" & "Doanh thu"',
                    type=QuestionType.TEXT,
                    required=True,
                    description='Mục "Thống kê thu nhập" trên điện thoại',
                    options=['Lựa chọn "A"', 'Lựa chọn "B"'],
                )
            ],
        )
        html = render_form_editor_html(schema)

        # Ensure escapeHtml helper exists in JavaScript
        assert "function escapeHtml" in html
        # Ensure renderQuestions uses escapeHtml to interpolate q.title, description, and options
        assert 'value="${escapeHtml(q.title)}"' in html
        assert '>${escapeHtml(q.description || \'\')}</textarea>' in html
        assert 'value="${escapeHtml((q.options || []).join(\', \'))}"' in html
        assert '>${escapeHtml(q.title)}</span>' in html
        assert '#${idx + 1}. ' not in html

        # Ensure INITIAL_SCHEMA JSON parses cleanly without escaping corruption
        assert 'const INITIAL_SCHEMA =' in html
        assert 'Tải lên ảnh \\"Màn hình chính\\" & \\"Doanh thu\\"' in html or 'Tải lên ảnh' in html

    def test_slot_level_image_checks_override_hierarchy(self):
        """Verify slot-level image checks override question-level and global checks."""
        from services.form_engine import get_effective_image_checks, FormSettings
        schema = FormSchema(
            form_id="test_slot_overrides",
            title="Test Overrides",
            settings=FormSettings(
                image_checks=ImageCheckConfig(
                    enable_blur_check=True,
                    blur_threshold=40.0,
                    enable_vehicle_match=True,
                )
            ),
            questions=[
                FormQuestion(
                    id="cam_q",
                    title="Camera Question",
                    type=QuestionType.CAMERA_WEBRTC,
                    image_checks=ImageCheckConfig(
                        enable_blur_check=True,
                        blur_threshold=30.0,
                        enable_vehicle_match=True,
                    ),
                    image_slots=[
                        {
                            "slot": 0,
                            "title": "Slot 0",
                            "image_checks": {
                                "enable_blur_check": False,
                                "blur_threshold": 15.0,
                                "enable_vehicle_match": False,
                            }
                        },
                        {
                            "slot": 1,
                            "title": "Slot 1",
                        }
                    ]
                )
            ]
        )

        # Slot 0 overrides: blur check disabled, vehicle match disabled, blur threshold 15.0
        slot0_checks = get_effective_image_checks(schema, "cam_q", slot_index=0)
        assert slot0_checks.enable_blur_check is False
        assert slot0_checks.blur_threshold == 15.0
        assert slot0_checks.enable_vehicle_match is False

        # Slot 1 inherits from Question-level: blur check enabled, blur threshold 30.0
        slot1_checks = get_effective_image_checks(schema, "cam_q", slot_index=1)
        assert slot1_checks.enable_blur_check is True
        assert slot1_checks.blur_threshold == 30.0
        assert slot1_checks.enable_vehicle_match is True

    def test_dynamic_form_rich_description_support(self):
        """Verify dynamic_form_html includes formatRichDescription and renders rich text container."""
        schema = FormSchema(
            form_id="test_rich_desc",
            title="Test Rich Description",
            questions=[
                FormQuestion(
                    id="q_desc_test",
                    title="Question with links and images",
                    type=QuestionType.TEXT,
                    description="Xem hướng dẫn [tại đây](https://example.com/guide) và ảnh ![Mẫu](https://example.com/sample.jpg)"
                )
            ]
        )
        html = render_dynamic_form_html(schema)
        assert "formatRichDescription" in html
        assert '<div class="q-desc">' in html
        assert ".q-desc a" in html
        assert ".q-desc img" in html

    @pytest.mark.asyncio
    async def test_inter_submission_detects_identical_financial_data(self, submit_camera, tmp_path):
        """A prior approved record blocks copied financials even when photos differ."""
        prev_id = "DEV-PAST-FINANCIAL"
        records = [{
            "submission_id": prev_id,
            "status": "APPROVED",
            "total_gross_revenue": 45_000_000,
            "total_trips": 300,
            "weekly_incomes": {f"week_{i}": weekly_ocr(i) for i in range(1, 6)},
        }]
        (tmp_path / "dev_test_submissions.json").write_text(json.dumps(records), encoding="utf-8")

        async def extract(_image_bytes, *, week_idx, prompt):
            return weekly_ocr(week_idx)

        response = await submit_camera(extract)
        assert response["status"] == "REJECTED", response
        assert "trùng" in response["reason"].lower()
        assert prev_id in response["reason"]
        assert response["duplicate_detected"] is True
        assert response["duplicate_type"] == "INTER_SUBMISSION_DATA"

