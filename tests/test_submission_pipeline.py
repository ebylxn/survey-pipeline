import asyncio
import io
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from psycopg import errors as pg_errors

from core.embedding_executor import InferenceBusyError, embedding_executor_lifespan
from services import submission_pipeline as pipeline


class MemoryDB:
    def __init__(self):
        self.rows = {}
        self.attempts = {}
        self.events = []
        self.sql = []
        self.fail_extended = False
        self.lose_commit_ack = False

    @asynccontextmanager
    async def connection(self):
        yield Transaction(self)


class Transaction:
    def __init__(self, db):
        self.db = db
        self.rows = {}
        self.attempts = {}
        self.events = []
        self.rowcount = -1
        self.result = None

    @asynccontextmanager
    async def cursor(self):
        yield self

    async def execute(self, sql, params=()):
        self.db.sql.append(sql)
        self.result = None
        if sql.startswith("INSERT INTO survey_submissions"):
            columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
            values = dict(zip(columns, params, strict=True))
            if self.db.fail_extended and "image_dhash" in values:
                raise pg_errors.UndefinedColumn("legacy test schema")
            sid = values["submission_id"]
            self.rowcount = int(sid not in self.db.rows)
            if self.rowcount:
                self.rows[sid] = values
        elif sql.startswith("INSERT INTO outbox_events"):
            assert "'ALL'" in sql
            event, sid, _ = params
            self.events.append((sid, event))
            self.result = ("legacy-id",)

    async def fetchone(self):
        return self.result

    async def rollback(self):
        self.rows.clear()
        self.attempts.clear()
        self.events.clear()

    async def commit(self):
        self.db.rows.update(self.rows)
        self.db.attempts.update(self.attempts)
        self.db.events.extend(self.events)
        if self.db.lose_commit_ack:
            self.db.lose_commit_ack = False
            raise ConnectionError("lost commit acknowledgement")


@pytest.fixture
def dependencies(tmp_path, monkeypatch):
    async def reserve(conn, scope, key, fingerprint):
        identity = (scope, key)
        previous = conn.db.attempts.get(identity)
        if previous:
            return ("replay", previous[1]) if previous[0] == fingerprint else ("conflict", None)
        conn.attempts[identity] = (fingerprint, None)
        return "new", None

    async def commit_response(conn, scope, key, fingerprint, sid, response):
        assert conn.attempts[(scope, key)] == (fingerprint, None)
        assert sid in conn.rows
        assert any(event_sid == sid for event_sid, _ in conn.events)
        conn.attempts[(scope, key)] = (fingerprint, response)

    monkeypatch.setattr(pipeline, "reserve", reserve)
    monkeypatch.setattr(pipeline, "commit_response", commit_response)
    monkeypatch.setattr(pipeline, "writer_schema_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(pipeline, "require_delivery_schema", AsyncMock())
    return pipeline.SubmissionDependencies(
        vision=None, media_root=str(tmp_path), dev_storage_file=str(tmp_path / "dev.json"),
        append_dev_record=lambda _: pytest.fail("unexpected DEV write"),
        image_max_dimension=600, blur_threshold=40, db_pool=MemoryDB(),
        schedule_legacy=lambda _: pytest.fail("unexpected legacy dispatch"),
    )


def _durable_enqueue(monkeypatch):
    targets = ("admin", "party", "r2", "lark", "drive", "billing")

    async def enqueue(conn, sid, *, schema, context):
        assert context == {"new_submission": True}
        assert conn.rows[sid]["work_owner"] == "outbox_v2"
        assert json.loads(conn.rows[sid]["form_data"])["_delivery_policy"]["source"] == "ctv"
        assert sid not in conn.db.rows  # same transaction as insert and attempt
        conn.events.extend((sid, target) for target in targets)
        return list(targets)

    adapter = AsyncMock(side_effect=enqueue)
    monkeypatch.setattr(pipeline, "enqueue_submission_work", adapter)
    return adapter, targets


@pytest.mark.asyncio
async def test_durable_commit_and_key_replay_after_flag_rollback(dependencies, monkeypatch, tmp_path):
    enqueue, targets = _durable_enqueue(monkeypatch)
    from services.form_engine import FormSchema
    schema = FormSchema(form_id="form", title="Form", questions=[])
    request = pipeline.SubmissionInput(
        form_id="form", delivery_schema=schema, driver_phone="+84988123456", collaborator_id="CTV-0001",
        fields={"_delivery_policy": '{"source":"none"}'}, idempotency_key="same-attempt",
    )
    deps = replace(dependencies, durable_enabled=True)
    result = await pipeline.process_submission(request, deps)
    assert result.response["driver_phone"] == "0988123456"
    assert result.response["status"] == result.status == "REJECTED"
    assert result.outbox_ids == targets
    assert enqueue.await_args.kwargs["schema"] is schema
    assert deps.db_pool.rows[result.submission_id]["ctv_code"] == "CTV-0001"
    assert deps.db_pool.events == [(result.submission_id, target) for target in targets]
    assert all("'ALL'" not in sql for sql in deps.db_pool.sql)
    assert pipeline.require_delivery_schema.await_count == 2

    legacy = replace(deps, durable_enabled=False)
    replay = await pipeline.process_submission(replace(request, driver_phone="0988123456"), legacy)
    assert replay.replayed and replay.response == result.response and not replay.outbox_ids
    with pytest.raises(pipeline.SubmissionConflict):
        await pipeline.process_submission(replace(request, fields={"new": "answer"}), legacy)
    assert len(list(tmp_path.iterdir())) == len(deps.db_pool.rows) == enqueue.await_count == 1


@pytest.mark.asyncio
async def test_unkeyed_id_collision_cannot_downgrade_durable_owner(dependencies, monkeypatch, tmp_path):
    enqueue, targets = _durable_enqueue(monkeypatch)
    monkeypatch.setattr(pipeline.secrets, "token_hex", lambda size: "a" * (2 * size))
    request = pipeline.SubmissionInput(form_id="form")
    result = await pipeline.process_submission(request, replace(dependencies, durable_enabled=True))

    # Use a separate writer volume to test the DB guard independently of directory exclusivity.
    legacy = replace(dependencies, media_root=str(tmp_path / "other-writer"))
    with pytest.raises(pipeline.SubmissionUnavailable):
        await pipeline.process_submission(request, legacy)
    assert dependencies.db_pool.rows[result.submission_id]["work_owner"] == "outbox_v2"
    assert dependencies.db_pool.events == [(result.submission_id, target) for target in targets]
    assert enqueue.await_count == 1


@pytest.mark.asyncio
async def test_legacy_savepoint_fallback_and_background_plan(dependencies):
    pending = []
    deps = replace(dependencies, schedule_legacy=pending.append)
    deps.db_pool.fail_extended = True
    result = await pipeline.process_submission(pipeline.SubmissionInput(form_id="form", collaborator_id="CTV"), deps)
    db = deps.db_pool
    assert db.rows[result.submission_id]["status"] == result.status
    assert "work_owner" not in db.rows[result.submission_id]
    assert db.sql.index("SAVEPOINT survey_extended_insert") < db.sql.index("ROLLBACK TO SAVEPOINT survey_extended_insert")
    assert db.sql.index("ROLLBACK TO SAVEPOINT survey_extended_insert") < db.sql.index("RELEASE SAVEPOINT survey_extended_insert")
    assert result.outbox_ids == ("legacy-id",)
    assert db.events == [(result.submission_id, "survey.rejected")]
    assert pending[0].checklist["submission_id"] == result.submission_id
    assert pending[0].party_notification["collaborator_id"] == "CTV"


@pytest.mark.asyncio
async def test_uncertain_commit_reads_committed_attempt_without_resending(dependencies, monkeypatch):
    enqueue, targets = _durable_enqueue(monkeypatch)
    deps = replace(dependencies, durable_enabled=True)
    deps.db_pool.lose_commit_ack = True
    result = await pipeline.process_submission(pipeline.SubmissionInput(form_id="form", idempotency_key="ack"), deps)
    assert result.replayed and result.response["status"] == deps.db_pool.rows[result.submission_id]["status"]
    assert enqueue.await_count == 1 and len(deps.db_pool.events) == len(targets)


@pytest.mark.asyncio
async def test_enqueue_failure_does_not_commit_attempt_or_fall_back(dependencies, monkeypatch):
    monkeypatch.setattr(pipeline, "enqueue_submission_work", AsyncMock(side_effect=RuntimeError("failed event insert")))
    deps = replace(dependencies, durable_enabled=True)
    with pytest.raises(pipeline.SubmissionUnavailable):
        await pipeline.process_submission(pipeline.SubmissionInput(form_id="form", idempotency_key="failure"), deps)
    assert not deps.db_pool.rows and not deps.db_pool.events and not deps.db_pool.attempts


@pytest.mark.asyncio
async def test_cancelled_durable_evaluation_recovers_once_with_retained_media(dependencies, monkeypatch, tmp_path):
    enqueue, targets = _durable_enqueue(monkeypatch)
    evaluating = asyncio.Event()

    async def interrupted(*_):
        evaluating.set()
        await asyncio.Future()

    monkeypatch.setattr(pipeline, "_evaluate", interrupted)
    deps = replace(dependencies, durable_enabled=True)
    request = pipeline.SubmissionInput(
        form_id="form", idempotency_key="cancelled", uploads=(pipeline.SubmissionUpload("__legacy__", 0, "a", b"raw"),),
    )
    task = asyncio.create_task(pipeline.process_submission(request, deps))
    await evaluating.wait()
    assert not deps.db_pool.rows
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replay = await pipeline.process_submission(request, replace(deps, durable_enabled=False))
    assert replay.replayed and replay.status == "MANUAL_REVIEW"
    assert replay.response["reason"] == pipeline.INTERRUPTED_CHECK_REASON
    assert deps.db_pool.events == [(replay.submission_id, target) for target in targets]
    item, = json.loads(deps.db_pool.rows[replay.submission_id]["media_manifest"])
    assert (tmp_path / replay.submission_id / item["relative_path"]).read_bytes() == b"raw"
    assert enqueue.await_count == 1


@pytest.mark.asyncio
async def test_dev_bypass_does_not_touch_database_even_with_flag_enabled(dependencies, monkeypatch):
    enqueue, _ = _durable_enqueue(monkeypatch)
    saved, scheduled = [], []
    deps = replace(dependencies, durable_enabled=True, append_dev_record=saved.append, schedule_legacy=scheduled.append)
    result = await pipeline.process_submission(pipeline.SubmissionInput(
        form_id="dev", driver_phone="0988123456", is_test_mode=True, bypass_img_check=True,
        gross_revenue="650000", fields={"consent_choice": "Tôi đồng ý"},
    ), deps)
    assert result.submission_id.startswith("DEV-") and result.status == "APPROVED"
    assert saved[0]["driver_phone"] == "0988123456" and saved[0]["total_gross_revenue"] == 650000
    assert saved[0]["mode"] == result.response["mode"] == "TEST_DEV_MODE"
    assert scheduled[0].checklist["db_pool"] is None
    assert not deps.db_pool.sql and not enqueue.called


@pytest.mark.asyncio
async def test_inference_busy_escapes_without_binding_a_verdict(dependencies, tmp_path):
    deps = replace(
        dependencies, normalize_image=lambda *_args, **_kw: (b"normalized", Image.new("RGB", (16, 16))),
        vision=SimpleNamespace(extract_facts=AsyncMock(return_value={})),
    )
    request = pipeline.SubmissionInput(
        form_id="form", idempotency_key="can-retry", uploads=(pipeline.SubmissionUpload("__legacy__", 0, "a", b"raw"),),
    )
    with pytest.raises(InferenceBusyError):  # no running embedding executor
        await pipeline.process_submission(request, deps)
    assert not deps.db_pool.rows and not deps.db_pool.events and not deps.db_pool.attempts
    received = [path for directory in tmp_path.iterdir() for path in directory.iterdir()]
    assert len(received) == 1 and received[0].read_bytes() == b"raw"


@pytest.mark.asyncio
async def test_duplicate_and_embedding_jobs_run_off_loop_with_private_pil_copies(dependencies, monkeypatch):
    from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig

    vision_roles = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="app_income"),
        ocr_driver_app_profile=AsyncMock(return_value={}),
        ocr_driver_app_income=AsyncMock(return_value={"gross_revenue": 10, "period": "Tuần 1"}),
        extract_facts=AsyncMock(return_value={}),
    )
    camera = FormQuestion(id="camera", title="Ảnh", type="camera_webrtc", image_slots=[
        {"title": "Thu nhập", "role": "app_income", "object_type": "gsm_app_income"},
    ])
    schema = FormSchema(form_id="form", title="Form", questions=[camera], settings=FormSettings(
        decree13_required=False, image_checks=ImageCheckConfig(enable_blur_check=False, enable_vehicle_match=False),
    ))
    audit = {
        "image_object_results": [], "income_pairs": [(0, {"gross_revenue": 10})],
        "income_required": 1, "violation": None, "profile_object": None, "role_0": "app_income",
    }
    monkeypatch.setattr(pipeline, "_audit_camera_question", AsyncMock(return_value=audit))
    enqueue, _ = _durable_enqueue(monkeypatch)
    original = Image.new("RGB", (16, 16))
    buffer = io.BytesIO()
    original.save(buffer, format="JPEG")
    valid_bytes = buffer.getvalue()
    calls = []
    event_loop_thread = threading.get_ident()

    def intra(raw_bytes, question, income, *, income_indices, pil_cache, **kwargs):
        assert raw_bytes == (valid_bytes,)
        assert threading.get_ident() != event_loop_thread and pil_cache[0] is not original
        assert question is not camera
        pil_cache[0].putpixel((0, 0), (255, 255, 255))
        calls.append("intra")
        return False, None

    def embed(image):
        assert threading.get_ident() != event_loop_thread and image is not original
        assert image.getpixel((0, 0)) == (0, 0, 0)
        calls.append("embed")
        return [1.0]

    inter = AsyncMock(return_value=(False, None, None))
    monkeypatch.setattr(pipeline, "check_intra_submission_duplicates", intra)
    monkeypatch.setattr(pipeline, "compute_dinov2_embedding", embed)
    monkeypatch.setattr(pipeline, "check_inter_submission_duplicates", inter)
    deps = replace(dependencies, durable_enabled=True, vision=vision_roles, normalize_image=lambda *_a, **_kw: (b"norm", original))
    request = pipeline.SubmissionInput(
        form_id="form", schema=schema, uploads=(pipeline.SubmissionUpload("camera", 0, "a", valid_bytes),),
    )
    async with embedding_executor_lifespan():
        result = await pipeline.process_submission(request, deps)
    assert result.status == "MANUAL_REVIEW"
    assert calls == ["intra", "embed"]  # inter check and persistence share the embedding
    assert inter.await_args.args[2] == [1.0] and enqueue.await_count == 1


@pytest.mark.asyncio
async def test_undecodable_upload_is_stored_as_bin_without_duplicate_hooks(
    dependencies, monkeypatch, tmp_path,
):
    from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig

    vision_roles = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="app_income"),
        ocr_driver_app_profile=AsyncMock(return_value={}),
        ocr_driver_app_income=AsyncMock(return_value={"gross_revenue": 10, "period": "Tuần 1"}),
        extract_facts=AsyncMock(return_value={}),
    )
    camera = FormQuestion(id="camera", title="Ảnh", type="camera_webrtc", image_slots=[
        {"title": "Thu nhập", "role": "app_income", "object_type": "gsm_app_income"},
    ])
    schema = FormSchema(form_id="form", title="Form", questions=[camera], settings=FormSettings(
        decree13_required=False, image_checks=ImageCheckConfig(enable_blur_check=False, enable_vehicle_match=False),
    ))
    audit = {
        "image_object_results": [], "income_pairs": [(0, {"gross_revenue": 10})],
        "income_required": 1, "violation": None, "profile_object": None, "role_0": "app_income",
    }
    monkeypatch.setattr(pipeline, "_audit_camera_question", AsyncMock(return_value=audit))
    calls = []

    def intra(*_args, **_kwargs):
        calls.append("intra")
        return False, None

    def embed(_image):
        calls.append("embed")
        return [1.0]

    monkeypatch.setattr(pipeline, "check_intra_submission_duplicates", intra)
    monkeypatch.setattr(pipeline, "compute_dinov2_embedding", embed)
    _durable_enqueue(monkeypatch)
    deps = replace(
        dependencies, durable_enabled=True, vision=vision_roles,
        normalize_image=lambda *_args, **_kwargs: (None, None),
    )
    request = pipeline.SubmissionInput(
        form_id="form", schema=schema, uploads=(pipeline.SubmissionUpload("camera", 0, "a", b"raw"),),
    )
    async with embedding_executor_lifespan():
        result = await pipeline.process_submission(request, deps)

    assert result.status == "REJECTED"
    assert calls == []
    assert any(path.suffix == ".bin" for path in tmp_path.rglob("*"))


@pytest.mark.asyncio
async def test_required_income_ocr_failure_cannot_count_as_audited_evidence():
    from services.form_engine import FormQuestion

    question = FormQuestion(id="proof", title="Income", type="camera_webrtc", image_slots=[
        {"title": "Income", "role": "app_income", "object_type": "weekly_income", "required": True},
    ])
    vision = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="app_income"),
        ocr_driver_app_income=AsyncMock(side_effect=TimeoutError("disposable OCR failure")),
    )
    with pytest.raises(TimeoutError, match="disposable OCR failure"):
        await pipeline._audit_camera_question(vision, question, [
            {"flat_index": 0, "slot_index": 0, "filename": "income.webp"},
        ], {0: b"normalized"})


PERFORMANCE_FIELDS = {
    "acceptance_rate_pct": 100,
    "cancellation_rate_pct": 20,
    "completion_rate_pct": 80,
    "period_raw_text": "16/11/2026",
    "capture_source": "SOCIAL_MEDIA_CAPTURE",
    "is_performance_screen": True,
    "is_legible": True,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("object_id,extraction,config,status,failure_engine", [
    ("gsm_performance", PERFORMANCE_FIELDS, {}, "APPROVED", None),
    ("gsm_performance", {**PERFORMANCE_FIELDS, "acceptance_rate_pct": 0, "cancellation_rate_pct": 0, "completion_rate_pct": 0}, {}, "APPROVED", None),
    ("gsm_performance", {"is_performance_screen": False, "is_legible": True}, {}, "REJECTED", "required_fields"),
    ("gsm_performance", {**PERFORMANCE_FIELDS, "is_performance_screen": False}, {}, "REJECTED", "boolean"),
    ("gsm_performance", {**PERFORMANCE_FIELDS, "acceptance_rate_pct": None}, {}, "REJECTED", "required_fields"),
    ("gsm_performance", {**PERFORMANCE_FIELDS, "cancellation_rate_pct": None}, {}, "REJECTED", "required_fields"),
    ("gsm_performance", {**PERFORMANCE_FIELDS, "completion_rate_pct": None}, {}, "REJECTED", "required_fields"),
    ("gsm_performance", {**PERFORMANCE_FIELDS, "period_raw_text": "16/10/2026"}, {}, "REJECTED", "constraints"),
    ("gsm_performance", {**PERFORMANCE_FIELDS, "period_raw_text": "16/11/2025"}, {}, "REJECTED", "constraints"),
    ("gsm_performance", {}, {}, "REJECTED", "object_extraction"),
    ("vehicle_exterior_or_interior", {}, {}, "REJECTED", "object_extraction"),
    ("vehicle_exterior_or_interior", {"view": None, "is_authentic": None}, {}, "REJECTED", "object_extraction"),
    ("vehicle_exterior_or_interior", {"vehicle_type": "CAR"}, {}, "REJECTED", "required_fields"),
    ("vehicle_exterior_or_interior", {"view": "EXTERIOR"}, {}, "REJECTED", "required_fields"),
    ("vehicle_exterior_or_interior", {"is_authentic": True}, {}, "REJECTED", "required_fields"),
    ("vehicle_exterior_or_interior", {"view": "EXTERIOR", "is_authentic": True}, {}, "APPROVED", None),
    ("vehicle_exterior_or_interior", {"view": "EXTERIOR", "is_authentic": False}, {}, "REJECTED", "vehicle_authentic"),
    ("vehicle_exterior_or_interior", {"view": "TRUNK", "is_authentic": True}, {}, "REJECTED", "vehicle_view_allowed"),
    ("vehicle_exterior_or_interior", {"view": "EXTERIOR", "is_authentic": True}, {"require_plate": True}, "REJECTED", "vehicle_plate_required"),
])
async def test_object_checks_determine_committed_submission_status(
    dependencies, monkeypatch, object_id, extraction, config, status, failure_engine,
):
    from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig

    camera = FormQuestion(id="proof", title="Evidence", type="camera_webrtc", required=True, image_slots=[
        {"title": "Evidence", "object_type": object_id, "object_config": config, "required": True},
    ])
    schema = FormSchema(form_id="object-audit", title="Object audit", questions=[camera], settings=FormSettings(
        decree13_required=False, require_manual_review=False,
        image_checks=ImageCheckConfig(enable_blur_check=False, enable_vehicle_match=False, enable_anti_fraud=False),
    ))
    vision = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="unknown"),
        extract_object_fields=AsyncMock(return_value=extraction),
    )
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    pending = []
    deps = replace(dependencies, vision=vision, schedule_legacy=pending.append)
    monkeypatch.setattr(pipeline, "compute_dinov2_embedding", lambda _image: [1.0])
    request = pipeline.SubmissionInput(
        form_id=schema.form_id, schema=schema,
        uploads=(pipeline.SubmissionUpload("proof", 0, "admin-editor.png", buffer.getvalue()),),
    )

    async with embedding_executor_lifespan():
        result = await pipeline.process_submission(request, deps)

    assert result.status == status, result.response
    row = deps.db_pool.rows[result.submission_id]
    assert row["status"] == status
    audit, = json.loads(row["raw_extracted_facts"])["image_object_results"]
    assert audit["question_id"] == "proof" and audit["slot_index"] == 0
    assert audit["passed"] is (status == "APPROVED")
    if failure_engine:
        assert failure_engine in {item["engine_id"] for item in audit["failures"]}


@pytest.mark.asyncio
async def test_same_custom_object_uses_each_slots_month_and_prompt():
    from services.form_engine import FormQuestion

    question = FormQuestion(id="months", title="Months", type="camera_webrtc", image_slots=[
        {"title": "November", "object_type": "gsm_performance", "required": True},
        {"title": "December", "object_type": "gsm_performance", "required": True,
         "object_config": {"constraints": [
             {"field": "period_months", "op": "contains", "value": 12},
             {"field": "period_years", "op": "contains", "value": 2026},
         ]}, "ai_detection_prompt": "Read the December evidence."},
    ])
    vision = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="unknown"),
        extract_object_fields=AsyncMock(side_effect=[
            PERFORMANCE_FIELDS,
            {**PERFORMANCE_FIELDS, "period_raw_text": "16/12/2026"},
        ]),
    )
    items = [{"flat_index": idx, "slot_index": idx, "filename": "photo.png"} for idx in range(2)]
    audit = await pipeline._audit_camera_question(vision, question, items, {0: b"november", 1: b"december"})
    assert audit["violation"] is None
    assert [item["passed"] for item in audit["image_object_results"]] == [True, True]
    assert "Read the December evidence." in vision.extract_object_fields.await_args_list[1].args[1]


@pytest.mark.asyncio
async def test_income_slots_validate_against_their_own_month():
    """Reported Nov-test bug: slot 1 is September while slot 0 is October."""
    from services.form_engine import FormQuestion

    def month_config(month):
        return {"period_type": "WEEKLY", "constraints": [
            {"field": "period_months", "op": "contains", "value": month},
            {"field": "period_years", "op": "contains", "value": 2026},
        ]}

    def income(period):
        return {
            "period": period, "period_raw_text": period, "period_type": "WEEKLY",
            "gross_revenue": 3_000_000, "net_income": 2_500_000, "discount_and_tax": 500_000,
            "total_trips": 20, "detected_app": "XANH_SM", "is_authentic_gsm_ui": True,
            "is_legible": True, "confidence_score": 0.99, "is_manipulated_or_photoshopped": False,
        }

    question = FormQuestion(id="cau_19", title="5 tuần", type="camera_webrtc", image_slots=[
        {"title": "Tuần 1", "object_type": "weekly_income", "required": True, "object_config": month_config("10")},
        {"title": "Tuần 2", "object_type": "weekly_income", "required": True, "object_config": month_config("9")},
    ])
    items = [{"flat_index": idx, "slot_index": idx, "filename": f"week{idx}.png"} for idx in range(2)]
    october, september = income("04/10/2026 - 10/10/2026"), income("06/09/2026 - 12/09/2026")

    vision = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="app_income"),
        ocr_driver_app_income=AsyncMock(side_effect=[october, september]),
    )
    audit = await pipeline._audit_camera_question(vision, question, items, {0: b"a", 1: b"b"})

    assert audit["violation"] is None, audit["violation"]
    assert [result["passed"] for result in audit["image_object_results"]] == [True, True]
    second_prompt = vision.ocr_driver_app_income.await_args_list[1].kwargs["prompt"]
    assert '"9"' in second_prompt

    # The same September photo in the October slot must fail on that slot only.
    vision.ocr_driver_app_income = AsyncMock(side_effect=[october, october])
    mismatch = await pipeline._audit_camera_question(vision, question, items, {0: b"a", 1: b"b"})
    assert mismatch["violation"] is not None
    assert mismatch["violation"]["failed_slot"] == 1
    assert "period_months" in mismatch["violation"]["reason"]


@pytest.mark.asyncio
async def test_test_mode_still_requires_the_configured_manual_review(dependencies, monkeypatch):
    """A test form must not auto-approve work its settings say admins must review."""
    from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig

    camera = FormQuestion(id="proof", title="Evidence", type="camera_webrtc", required=True, image_slots=[
        {"title": "Performance", "object_type": "gsm_performance", "required": True},
    ])
    schema = FormSchema(form_id="test-review", title="Test review", questions=[camera], settings=FormSettings(
        decree13_required=False, is_test_mode=True, require_manual_review=True,
        image_checks=ImageCheckConfig(enable_blur_check=False, enable_vehicle_match=False, enable_anti_fraud=False),
    ))
    vision = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="unknown"),
        extract_object_fields=AsyncMock(return_value=PERFORMANCE_FIELDS),
    )
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    saved = []
    deps = replace(dependencies, vision=vision, append_dev_record=saved.append, schedule_legacy=lambda _: None)
    monkeypatch.setattr(pipeline, "compute_dinov2_embedding", lambda _image: [1.0])
    request = pipeline.SubmissionInput(
        form_id=schema.form_id, schema=schema, is_test_mode=True,
        uploads=(pipeline.SubmissionUpload("proof", 0, "perf.png", buffer.getvalue()),),
    )

    async with embedding_executor_lifespan():
        result = await pipeline.process_submission(request, deps)

    assert result.status == "MANUAL_REVIEW", result.response
    assert saved[0]["status"] == "MANUAL_REVIEW"


@pytest.mark.asyncio
async def test_configured_manual_review_can_be_switched_off_for_testing(dependencies):
    """`require_manual_review=False` still auto-approves, so testers can iterate fast."""
    from services.form_engine import FormSchema, FormSettings, ImageCheckConfig

    schema = FormSchema(form_id="fast", title="Fast", questions=[], settings=FormSettings(
        decree13_required=False, is_test_mode=True, require_manual_review=False,
        image_checks=ImageCheckConfig(enable_blur_check=False, enable_vehicle_match=False, enable_anti_fraud=False),
    ))
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    deps = replace(dependencies, vision=SimpleNamespace(), append_dev_record=lambda _: None, schedule_legacy=lambda _: None)
    result = await pipeline.process_submission(
        pipeline.SubmissionInput(
            form_id=schema.form_id, schema=schema, is_test_mode=True,
            uploads=(pipeline.SubmissionUpload("q1", 0, "photo.png", buffer.getvalue()),),
        ),
        deps,
    )
    assert result.status == "APPROVED", result.response


@pytest.mark.asyncio
async def test_admin_notice_uses_form_answers_not_hardcoded_income_or_vehicle(dependencies, monkeypatch):
    """The admin Telegram text lists the form's own fields, including CTV attribution."""
    from services.form_engine import FormQuestion, FormSchema, FormSettings, ImageCheckConfig, QuestionType

    schema = FormSchema(form_id="camp", title="Camp", questions=[
        FormQuestion(id="phone_q", title="Số điện thoại", type=QuestionType.TEXT, required=True),
        FormQuestion(id="brand_q", title="Hãng xe", type=QuestionType.TEXT, required=False),
    ], settings=FormSettings(
        decree13_required=False, is_test_mode=True, require_manual_review=True,
        image_checks=ImageCheckConfig(enable_blur_check=False, enable_vehicle_match=False, enable_anti_fraud=False),
    ))
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    captured = []
    deps = replace(
        dependencies, vision=SimpleNamespace(),
        append_dev_record=lambda _: None, schedule_legacy=captured.append,
    )
    monkeypatch.setattr(pipeline, "compute_dinov2_embedding", lambda _image: [1.0])
    request = pipeline.SubmissionInput(
        form_id=schema.form_id, schema=schema, is_test_mode=True, collaborator_id="CTV01",
        fields={"phone_q": "0912345678", "brand_q": "VinFast VF5"}, driver_phone="0912345678",
        uploads=(pipeline.SubmissionUpload("phone_q", 0, "photo.png", buffer.getvalue()),),
    )

    async with embedding_executor_lifespan():
        result = await pipeline.process_submission(request, deps)

    text = captured[0].admin_text
    assert result.status == "MANUAL_REVIEW"
    assert "Số điện thoại" in text and "0912345678" in text
    assert "Hãng xe" in text and "VinFast VF5" in text
    assert "CTV:" in text and "CTV01" in text
    assert "Loại xe" not in text and "Phương tiện" not in text
    assert "Doanh thu" not in text
    assert "Biểu mẫu:" in text


@pytest.mark.asyncio
async def test_required_custom_slot_cannot_disappear_from_audit():
    from services.form_engine import FormQuestion

    question = FormQuestion(id="proof", title="Proof", type="camera_webrtc", image_slots=[
        {"title": "Performance", "object_type": "gsm_performance", "required": True},
    ])
    audit = await pipeline._audit_camera_question(SimpleNamespace(), question, [], {})
    assert audit["violation"] is not None


@pytest.mark.asyncio
async def test_required_custom_ocr_failure_is_not_silently_approved():
    from services.form_engine import FormQuestion

    question = FormQuestion(id="proof", title="Proof", type="camera_webrtc", image_slots=[
        {"title": "Performance", "object_type": "gsm_performance", "required": True},
    ])
    vision = SimpleNamespace(
        classify_image_role=AsyncMock(return_value="gsm_performance"),
        extract_object_fields=AsyncMock(side_effect=TimeoutError("OCR unavailable")),
    )
    with pytest.raises(TimeoutError, match="OCR unavailable"):
        await pipeline._audit_camera_question(vision, question, [
            {"flat_index": 0, "slot_index": 0, "filename": "photo.png"},
        ], {0: b"normalized"})
