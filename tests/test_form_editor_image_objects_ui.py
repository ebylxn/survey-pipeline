"""Focused checks for the image-object selector in the form editor.

The editor is static HTML + inline JS (no bundler / JSDOM harness in this repo),
so this test inspects the rendered HTML for the required controls/functions and
runs `node --check` on the inline script when node is available.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from services.form_engine import FormQuestion, FormSchema, FormSettings, QuestionType
from services.form_manager_html import render_form_editor_html


def _camera_schema(slots):
    return FormSchema(
        form_id="test_image_objects_editor",
        title="Test Image Objects Editor",
        settings=FormSettings(decree13_required=False),
        questions=[
            FormQuestion(
                id="q_cam",
                title="Ảnh chụp",
                type=QuestionType.CAMERA_WEBRTC,
                required=True,
                image_slots=slots,
            )
        ],
    )


def _embedded_schema_json(html_text: str) -> dict:
    marker = "const INITIAL_SCHEMA = "
    match = re.search(re.escape(marker), html_text)
    assert match, f"marker not found: {marker}"
    data, _ = json.JSONDecoder().raw_decode(html_text[match.end():])
    return data


def test_editor_slot_has_object_selector_and_custom_prompt():
    html = render_form_editor_html(_camera_schema([]), is_new=False)

    # Object mode dropdown replaces the free-text "role" input.
    assert '-- Tự nhập prompt --' in html
    assert 'onchange="setSlotObjectType(${qidx}, ${sidx}, this.value)"' in html
    assert "Loại đối tượng / Role (Mã AI nhận diện)" not in html

    # Custom prompt fallback plus the object-config renderer.
    assert "Prompt nhận diện tự nhập" in html
    assert "function renderSlotObjectConfig" in html
    assert "function renderSlotObjectControl" in html
    assert "function setSlotObjectType" in html
    assert "function setSlotObjectConfig" in html

    # Loader wires the API contract and keeps load failures non-blocking.
    assert "fetch('/api/v1/image-objects'" in html
    assert "credentials: 'include'" in html
    assert "window.IMAGE_OBJECTS" in html
    assert "window.CHECK_ENGINES" in html
    assert "loadImageObjects().then(" in html
    assert "renderImageObjectsWarning()" in html


def test_editor_scoped_modules_and_collapsible_question_options():
    html = render_form_editor_html(_camera_schema([]), is_new=False)
    assert "function renderQuestionOptionsBlock" in html
    assert "Tùy chọn câu hỏi" in html
    assert "function renderScopedModulesSection" in html
    assert "function addScopedModule" in html
    assert "renderScopedModulesSection('question'" in html
    assert "renderScopedModulesSection('slot'" in html
    assert "scope: module.scope" not in html
    assert "question_id: scope === 'form' ? null" in html
    assert "slot_index: scope === 'slot' ? Number(module.slot_index) : null" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_adds_question_and_slot_scoped_modules():
    slot = {"slot": 0, "title": "Front", "role": "custom_object", "required": True}
    html = render_form_editor_html(_camera_schema([slot]), is_new=False)
    body = (
        "const t = sandbox.__t;\n"
        "function check(cond, msg) { if (!cond) throw new Error(msg); }\n"
        "const q = t.questions[0];\n"
        "t.addScopedModule('question', q.id, null);\n"
        "check(t.formModules.length === 1, 'question module not added');\n"
        "check(t.formModules[0].scope === 'question', 'question scope missing');\n"
        "check(t.formModules[0].question_id === q.id, 'question id missing');\n"
        "check(t.formModules[0].slot_index === null, 'question module must not have slot_index');\n"
        "const questionHtml = t.renderQuestionOptionsBlock(q, 0);\n"
        "check(questionHtml.includes('Tùy chọn câu hỏi'), 'collapsible question options missing');\n"
        "check(questionHtml.includes('Module kiểm tra cho câu này'), 'question module section missing');\n"
        "t.addScopedModule('slot', q.id, 0);\n"
        "check(t.formModules.length === 2, 'slot module not added');\n"
        "check(t.formModules[1].scope === 'slot', 'slot scope missing');\n"
        "check(t.formModules[1].slot_index === 0, 'slot index missing');\n"
        "const slotHtml = t.renderScopedModulesSection('slot', q.id, 0, 'Slot modules', 'hint');\n"
        "check(slotHtml.includes('Slot modules'), 'slot module section missing');\n"
        "check(t.scopedModuleIndexes('slot', q.id, 0).length === 1, 'slot scope index wrong');\n"
    )
    _run_editor_js(html, body)


def test_editor_renders_every_customizable_control_type():
    html = render_form_editor_html(_camera_schema([]), is_new=False)
    src = html[html.index("function renderSlotObjectControl"):]
    src = src[:src.index("function renderSlotObjectConfig")]

    for control_type in ("select", "toggle", "number", "month", "date", "textarea", "constraints"):
        assert f"type === '{control_type}'" in src
    # Anything else falls back to a plain text input.
    assert '<input type="text"' in src
    assert "this.value, 'text')" in src
    # Toggle reuses the editor's switch classes instead of a bare checkbox.
    assert 'class="toggle-switch"' in src
    assert 'class="toggle-slider"' in src
    # Read-only extracted fields are surfaced for admins.
    assert "AI sẽ trích xuất các trường sau" in html


def test_editor_round_trips_existing_slot_fields():
    slot = {
        "slot": 0,
        "title": "Thu nhập tuần",
        "role": "weekly_income",
        "object_type": "weekly_income",
        "object_config": {"granularity": "WEEKLY", "max_days": 24},
        "ai_detection_prompt": "ignored in object mode",
        "required": True,
        "require_unique": True,
        "allow_upload": False,
    }
    html = render_form_editor_html(_camera_schema([slot]), is_new=False)
    embedded = _embedded_schema_json(html)
    restored = embedded["questions"][0]["image_slots"][0]
    assert restored["object_type"] == "weekly_income"
    assert restored["object_config"] == {"granularity": "WEEKLY", "max_days": 24}
    assert restored["title"] == "Thu nhập tuần"
    assert restored["require_unique"] is True
    assert restored["allow_upload"] is False


def test_editor_legacy_slot_without_object_type_stays_custom_prompt():
    slot = {
        "slot": 0,
        "title": "Hồ sơ",
        "role": "app_profile",
        "ai_detection_prompt": "Đọc mã đối tác và hạng thành viên",
        "required": True,
    }
    html = render_form_editor_html(_camera_schema([slot]), is_new=False)
    restored = _embedded_schema_json(html)["questions"][0]["image_slots"][0]
    assert "object_type" not in restored
    assert restored["role"] == "app_profile"
    assert restored["ai_detection_prompt"] == "Đọc mã đối tác và hạng thành viên"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_inline_script_passes_node_syntax_check():
    html = render_form_editor_html(_camera_schema([]), is_new=False)
    scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, "editor should embed an inline script"
    for script in scripts:
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(script)
            result = subprocess.run(
                ["node", "--check", path], capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, result.stderr
        finally:
            os.unlink(path)


CONSTRAINTS_OBJECT = {
    "id": "weekly_income",
    "label": "Báo cáo thu nhập tuần",
    "description": "Màn hình thu nhập tuần.",
    "expected_fields": [
        {"key": "period_raw_text", "label": "Khoảng thời gian", "type": "string", "required": True}
    ],
    "customizable": [
        {
            "key": "period_type",
            "label": "Loại kỳ",
            "type": "select",
            "options": ["DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"],
            "default": "WEEKLY",
        },
        {
            "key": "constraints",
            "label": "Ràng buộc kỳ",
            "type": "constraints",
            "default": [],
            "help": "Mỗi ràng buộc gồm trường, điều kiện và giá trị.",
            "item_fields": [
                {
                    "key": "field",
                    "label": "Trường",
                    "type": "select",
                    "options": [
                        "period_type", "period_months", "period_years",
                        "duration_days", "period_start", "period_end",
                    ],
                },
                {
                    "key": "op",
                    "label": "Điều kiện",
                    "type": "select",
                    "options": [
                        "equals", "not_equals", "contains", "in",
                        "gte", "lte", "between",
                    ],
                },
                {"key": "value", "label": "Giá trị", "type": "text"},
            ],
        },
    ],
}


def _run_editor_js(rendered_html: str, body_js: str) -> None:
    """Run the editor's inline JS in a stubbed DOM and execute ``body_js``."""
    node = shutil.which("node")
    assert node, "node is required"
    match = re.search(r"<script>([\s\S]*?)</script>", rendered_html)
    assert match, "inline editor script not found"
    script = match.group(1)
    harness = (
        "const vm = require('vm');\n"
        f"const script = {json.dumps(script, ensure_ascii=True)};\n"
        "function fakeEl() {\n"
        "  return {\n"
        "    innerHTML: '', style: {}, value: '', checked: false, textContent: '',\n"
        "    classList: { add() {}, remove() {}, contains() { return false; } },\n"
        "    appendChild() {}, querySelectorAll() { return []; }, focus() {},\n"
        "  };\n"
        "}\n"
        "const els = {};\n"
        "const sandbox = {\n"
        "  console, setTimeout, clearTimeout,\n"
        "  JSON, Math, Date, Object, Array, String, Number, Boolean, Set, Map, Promise, RegExp, Error,\n"
        "  localStorage: { getItem() { return null; }, setItem() {} },\n"
        "  fetch: async () => ({ ok: false, status: 500, json: async () => ({}) }),\n"
        "  document: {\n"
        "    getElementById(id) { return els[id] || (els[id] = fakeEl()); },\n"
        "    querySelectorAll() { return []; },\n"
        "    createElement() { return fakeEl(); },\n"
        "    addEventListener() {},\n"
        "    title: '',\n"
        "  },\n"
        "};\n"
        "sandbox.window = { addEventListener() {}, IMAGE_OBJECTS: [], CHECK_ENGINES: [], IMAGE_OBJECTS_ERROR: '' };\n"
        "vm.createContext(sandbox);\n"
        "vm.runInContext(script + '\\n;globalThis.__t = { questions, setSlotObjectType, renderSlotObjectControl, "
        "addSlotObjectConstraint, removeSlotObjectConstraint, setSlotObjectConstraintValue, "
        "pruneSlotObjectConfig, formModules, renderQuestionOptionsBlock, "
        "renderScopedModulesSection, addScopedModule, scopedModuleIndexes };', sandbox);\n"
        + body_js
    )
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(harness)
        proc = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_editor_renders_constraints_repeater():
    html = render_form_editor_html(_camera_schema([]), is_new=False)
    assert "type === 'constraints'" in html
    assert "function renderSlotObjectConstraints" in html
    assert "function renderSlotObjectConstraintRow" in html
    assert "function addSlotObjectConstraint" in html
    assert "function removeSlotObjectConstraint" in html
    assert "function setSlotObjectConstraintValue" in html
    assert "function pruneSlotObjectConfig" in html
    assert "+ Thêm ràng buộc" in html
    assert "Chưa có ràng buộc nào." in html
    assert "class=\"constraint-row\"" in html
    # Save path must go through the nested-safe pruner.
    assert "cleanSlot.object_config = pruneSlotObjectConfig(" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_constraints_repeater_state_and_save_pruning():
    slot = {"slot": 0, "title": "Thu nhập", "role": "custom_object", "required": True}
    html = render_form_editor_html(_camera_schema([slot]), is_new=False)
    body = (
        "const t = sandbox.__t;\n"
        f"const objects = {json.dumps([CONSTRAINTS_OBJECT], ensure_ascii=False)};\n"
        + r"""
sandbox.window.IMAGE_OBJECTS = objects;
const item = objects[0].customizable.find(i => i.type === 'constraints');
function check(cond, msg) { if (!cond) throw new Error(msg); }

t.setSlotObjectType(0, 0, 'weekly_income');
const state = t.questions[0].image_slots[0];
check(Array.isArray(state.object_config.constraints), 'constraints should default to []');
check(state.object_config.constraints.length === 0, 'constraints default should be empty');
check(state.object_config.period_type === 'WEEKLY', 'period_type default was not applied');

let rendered = t.renderSlotObjectControl(0, 0, state.object_config, item);
check(rendered.includes('+ Thêm ràng buộc'), 'add button missing');
check(rendered.includes('Chưa có ràng buộc nào.'), 'empty state missing');
check(rendered.includes('Ràng buộc kỳ'), 'constraints label missing');
check(rendered.includes('Mỗi ràng buộc gồm'), 'help text missing');

t.addSlotObjectConstraint(0, 0, 'constraints');
check(state.object_config.constraints.length === 1, 'row not added');
const row = state.object_config.constraints[0];
check(row.field === '' && row.op === '' && row.value === '', 'new row shape is wrong');

t.setSlotObjectConstraintValue(0, 0, 'constraints', 0, 'field', 'period_type', 'select');
t.setSlotObjectConstraintValue(0, 0, 'constraints', 0, 'op', 'in', 'select');
t.setSlotObjectConstraintValue(0, 0, 'constraints', 0, 'value', '9, 10', 'text');
check(
  JSON.stringify(state.object_config.constraints[0]) === JSON.stringify({ field: 'period_type', op: 'in', value: '9, 10' }),
  'row values were not updated: ' + JSON.stringify(state.object_config.constraints[0])
);

rendered = t.renderSlotObjectControl(0, 0, state.object_config, item);
check(rendered.includes('removeSlotObjectConstraint(0, 0, &quot;constraints&quot;, 0)'), 'remove button missing');
check(rendered.includes('placeholder="9, 10  (dùng &quot;a,b&quot;'), 'in placeholder missing');
check(rendered.includes('dấu phẩy'), 'comma quoting hint missing');
check(rendered.includes('<option value="period_type" selected>'), 'field select lost its state');
check(rendered.includes('<option value="in" selected>'), 'op select lost its state');

t.addSlotObjectConstraint(0, 0, 'constraints');
check(state.object_config.constraints.length === 2, 'second row not added');
t.setSlotObjectConstraintValue(0, 0, 'constraints', 1, 'value', 'second', 'text');
t.removeSlotObjectConstraint(0, 0, 'constraints', 0);
check(state.object_config.constraints.length === 1, 'row not removed');
check(state.object_config.constraints[0].value === 'second', 'removed the wrong row');

const pruned = t.pruneSlotObjectConfig({
  period_type: 'WEEKLY',
  constraints: [{ field: 'period_type', op: 'between', value: '2026-09, 2026-10' }],
  max_days: undefined,
}, objects[0]);
check(!('max_days' in pruned), 'undefined config key was not pruned');
check(
  JSON.stringify(pruned.constraints) === JSON.stringify([{ field: 'period_type', op: 'between', value: '2026-09, 2026-10' }]),
  'nested constraints were altered or coerced: ' + JSON.stringify(pruned.constraints)
);
check(Array.isArray(pruned.constraints) && typeof pruned.constraints[0] === 'object', 'constraints must stay an array of objects');
"""
    )
    _run_editor_js(html, body)
