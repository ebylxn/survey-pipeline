# AI Agent & Customer Guide: Creating a Working Image Object from a Sample Image

> **Purpose**: Give this file (or its GitHub URL) together with a **sample image** to any AI agent (ChatGPT, Claude, Gemini, etc.). The agent will inspect the image and either:
> 1. **Create a valid Image Object directly** via the Trust Bridge admin API, **or**
> 2. **Output exact copy-pasteable fields + step-by-step UI instructions** so a `manager` or `superadmin` can create and wire it into a survey form in under 2 minutes with zero validation errors.

---

## 1. Ready-to-Use Prompt for Customers

Attach your sample image to your AI assistant and paste:

```text
Read docs/IMAGE_OBJECT_AI_AUTHORING_GUIDE.md from the Trust Bridge survey-pipeline repository.
Inspect the attached sample image and design a working Trust Bridge Image Object for it.
Follow every schema constraint in the guide strictly (no extra keys, unique engine IDs, extractor="generic", json_output_instruction=true).
Output:
1. A short summary of what fields will be extracted and what checks will pass/reject a photo.
2. The complete validated JSON definition (for one-shot paste in /admin/modules or POST /admin/image-objects/<id>).
3. The exact field-by-field copy-paste values for /admin/image-objects (if I prefer the form UI).
4. Step-by-step instructions to attach it to a camera_webrtc slot in /admin/forms.
```

---

## 2. AI Agent Authoring Protocol (Strict Rules)

When a customer gives you a sample image, follow this exact checklist before outputting anything:

1. **Inspect the image carefully**:
   - Identify what the photo actually is (e.g., fuel receipt, vehicle registration card, e-hailing wallet screen, odometer photo, electricity bill).
   - List 2-5 concrete visual landmarks visible in authentic photos -> `objects` (string array).
   - List only fields that are **actually readable** in the image -> `expected_fields` (snake_case keys, strict types). Always include `"is_authentic"` (`boolean`) if you want to reject mockups/screenshots/unrelated photos, and `"is_legible"` (`boolean`) if legibility matters.
2. **Choose `extractor` and `json_output_instruction`**:
   - **For ANY new custom image type**: ALWAYS set `"extractor": "generic"` and `"json_output_instruction": true`.
   - Do **NOT** use `"vehicle"` (returns `{}` in the generic extractor pipeline) or `"income_statement"` / `"profile"` (reserved for built-in GSM driver income/profile extractors).
   - Use `"none"` only when the slot should skip AI OCR extraction entirely.
3. **Choose `id` and `classify_role`**:
   - **`id`**: Use lowercase ASCII letters, digits, and underscores only: `^[a-z0-9_]{1,64}$` (e.g. `fuel_receipt`, `grab_wallet_screen`, `odometer_reading`).
     - *Why*: `/admin/modules` and the API accept `[A-Za-z0-9_-]{1,64}`, but the `/admin/image-objects` HTML creation form enforces `pattern="[a-z0-9_]+"`. Using lowercase `snake_case` guarantees compatibility everywhere.
   - **`classify_role`**: Set to the same `snake_case` slug as `id` (or `null` if unused). Do **NOT** reuse built-in aliases (`app_profile`, `profile`, `app_income`, `income`, `revenue_screen`, `weekly_income`, `vehicle_exterior`, `vehicle_interior`, `gsm_profile_screen`) unless intentionally targeting those built-in objects.
4. **Write a deterministic OCR `prompt`**:
   - Include the three supported template tokens: `{{objects}}`, `{{fields}}`, and `{{config}}`.
   - Explicitly instruct the vision model: *"Only transcribe what is actually visible. Never guess or invent values; use null for any field not visible. Set is_authentic=false for unrelated, edited, or mockup images."*
   - Remember: when `json_output_instruction: true` and `expected_fields` is non-empty, the backend automatically appends strict JSON-only instructions listing every `expected_fields[].key`.
5. **Design `customizable` (per-form slot overrides)**:
   - Expose campaign-specific thresholds or toggles here (e.g. `require_authentic`, `min_amount`, `allowed_brands`) so admins can tune the same Image Object per survey without editing the object itself.
   - Reference customizable keys inside engine `params` using `"{{key}}"` and inside engine `conditions` using `{"field": "$config.<key>", "op": "equals", "value": true}`.
6. **Bind ONLY real, enabled `engines` (and never repeat an engine `id`)**:
   - Every `engines[].id` MUST already exist in the system's enabled `engine` registry (Section 4 below). Referencing an unregistered engine ID fails with HTTP `400` (`references missing engine(s)`).
   - **`engines[].id` MUST be unique within one Image Object** (`engine bindings must be unique`).
   - If you need two checks that use the same underlying implementation (e.g. two separate `boolean` checks), either:
     - Use distinct pre-registered engine IDs (for example `boolean` + `legible` + `not_photoshopped`), **or**
     - Instruct the admin (or call `/admin/modules`) to create a second `engine` module first with a distinct ID pointing to the same `implementation` (Section 5).
7. **Self-validate against `extra="forbid"`**:
   - Every Pydantic model (`ImageObjectDefinition`, `ExpectedField`, `CustomizableField`, `EngineBinding`) uses `ConfigDict(extra="forbid")`. Any typo or undocumented key causes a hard validation error!

---

## 3. Exact Schema Specification (`services/image_objects.py`)

### 3.1 Top-Level `ImageObjectDefinition` (`extra="forbid"`)

| Key | Type | Required | Allowed Values / Constraints |
|---|---|---|---|
| `version` | integer | No (default `1`) | Integer `1` |
| `id` | string | Yes | Pattern `^[A-Za-z0-9_-]{1,64}$` (**recommend `^[a-z0-9_]+$`** so it works in both `/admin/image-objects` and `/admin/modules`) |
| `label` | string | Yes | Human-readable name (Vietnamese recommended for admin UI, max 100 chars when mirrored to module registry) |
| `description` | string | No (default `""`) | Short description of what photo this object validates |
| `prompt` | string | No (default `""`) | Vision AI instruction template. Supports `{{objects}}`, `{{fields}}`, `{{config}}` |
| `objects` | `string[]` | No (default `[]`) | Visual landmarks to detect in the image |
| `expected_fields` | `ExpectedField[]` | No (default `[]`) | Fields extracted by OCR. `key` values must be unique |
| `customizable` | `CustomizableField[]` | No (default `[]`) | Per-slot config options editable in `/admin/forms`. `key` values must be unique |
| `engines` | `EngineBinding[]` | No (default `[]`) | Validation rules executed on extracted fields. `id` values must be unique and registered |
| `extractor` | string | No (default `"generic"`) | One of: `"generic"` (**use for custom objects**), `"income_statement"`, `"profile"`, `"vehicle"`, `"none"` |
| `enabled` | boolean | No (default `true`) | Must be `true` to be usable in surveys |
| `json_output_instruction` | boolean | No (default `false`) | **Set `true`** for custom `"generic"` objects so the backend appends strict JSON-key instructions |
| `classify_role` | `string \| null` | No (default `null`) | Optional classification role tag (use same slug as `id` or `null`) |

### 3.2 `ExpectedField` (`extra="forbid"`)

| Key | Type | Required | Constraints |
|---|---|---|---|
| `key` | string | Yes | Unique `snake_case` key |
| `label` | string | Yes | Human-readable label |
| `type` | string | No (`"string"`) | One of: `"string"`, `"integer"`, `"number"`, `"boolean"`, `"array"`, `"object"` |
| `required` | boolean | No (`false`) | Enforced: a blank required value rejects the submission, with or without an explicit `required_fields` engine |
| `description` | `string \| null` | No (`null`) | Optional note |

> **Note**: `"required": true` is enforced by `run_definition_checks`, which runs the shared `required_fields` check when the object has no explicit `required_fields` binding. Mark only fields that a genuine photo must always show; a field the model may legitimately leave blank must stay `false`.

### 3.3 `CustomizableField` (`extra="forbid"`)

| Key | Type | Required | Constraints |
|---|---|---|---|
| `key` | string | Yes | Unique `snake_case` key |
| `label` | string | Yes | Human-readable label shown in Form Editor |
| `type` | string | No (`"text"`) | One of: `"text"`, `"number"`, `"select"`, `"toggle"`, `"month"`, `"date"`, `"textarea"`, `"constraints"` |
| `options` | `any[] \| null` | Only for `select` | **Required** non-empty list when `type == "select"`; **FORBIDDEN** (must omit or `null`) for all other types |
| `default` | any | No (`null`) | Default config value |
| `help` | `string \| null` | No (`null`) | Optional helper text |
| `item_fields` | `object[] \| null` | Only for `constraints` | **Required** list of `{"key": ..., "label": ...}` when `type == "constraints"`; **FORBIDDEN** for all other types |

### 3.4 `EngineBinding` (`extra="forbid"`)

| Key | Type | Required | Constraints |
|---|---|---|---|
| `id` | string | Yes | Must match an enabled engine ID; must be unique within `engines[]` |
| `label` | `string \| null` | No (`null`) | Display label for this check |
| `params` | object | No (`{}`) | Engine parameters; string values `"{{customizable_key}}"` resolve from slot config keeping raw type |
| `conditions` | `object[]` | No (`[]`) | All conditions must pass for the engine to run (if any fails, engine is skipped cleanly) |

Each condition object has:
- `field` (required string): extracted field key (e.g. `"amount"`), or `"$config.<customizable_key>"` to gate on a slot config toggle!
- `op` (optional string, default `"not_empty"`): one of `"not_empty"`, `"empty"`, `"equals"`, `"not_equals"`, `"contains"`, `"regex"`, `"gte"`, `"lte"`.
- `value` (optional): comparison target for ops other than `"not_empty"` / `"empty"`.

### 3.5 How the object is enforced at submission time

Knowing this prevents a false sense of safety when authoring an object:

- Every uploaded image on a `camera_webrtc` question is routed to the object declared by its slot (`object_type`). Custom `generic` objects and vehicle objects are extracted per slot and checked with their bound engines.
- `extractor: "generic"` objects call the configured vision model with the object prompt; `extractor: "vehicle"` objects also use that generic extraction path and then run their own engines.
- **Fail-closed evidence rule:** if an object declares `expected_fields` or `engines` (or uses a `profile` / `income_statement` / `vehicle` extractor) and the extraction returns no readable value at all, the submission is rejected with "Không trích xuất được thông tin từ ảnh để kiểm tra đối tượng yêu cầu." A missing model response never counts as a pass.
- A required slot with no upload, or an image that never reached a check, is also rejected.
- `"required": true` on `expected_fields` now rejects a blank value by itself; a `boolean` / `enum` / `number_range` engine adds the value-level rule (e.g. allowed view, authenticity must be true).
- Objects with no fields and no engines (legacy slots and custom-prompt slots) keep their previous tolerant behavior: there is nothing to validate.

---

## 4. Pre-Registered Check Engines Catalog

These engines are seeded out of the box (verified against `data/check_engines/*.json` and `services/image_check_engines.py`). If you have API access, call `GET /api/v1/image-objects` first to confirm the exact enabled engines on the target server.

### 4.1 General-Purpose Engines (Best for Custom Image Objects)

| Engine `id` | `implementation` | Key `params` | What it checks |
|---|---|---|---|
| `required_fields` | `required_fields` | `{"fields": ["key1", "key2"]}` | Rejects if any listed extracted field is null/empty |
| `boolean` | `boolean` | `{"field": "is_authentic", "expected": true, "required": false, "message": "..."}` | Checks a boolean field equals `expected` |
| `legible` | `boolean` | `{"field": "is_legible", "expected": true, "message": "..."}` | Separate boolean engine ID for legibility check |
| `not_photoshopped` | `boolean` | `{"field": "is_manipulated", "expected": false, "message": "..."}` | Separate boolean engine ID for manipulation check |
| `enum` | `enum` | `{"field": "brand", "allowed": "{{allowed_brands}}", "required": false, "message": "..."}` | Checks extracted value is in comma-separated string or array `allowed` |
| `regex` | `regex` | `{"field": "plate", "pattern": "^[0-9]{2}[A-Z]-?[0-9]{4,5}$", "message": "..."}` | Checks extracted string matches regex |
| `number_range` | `number_range` | `{"field": "total_vnd", "min": 10000, "max": 50000000, "message": "..."}` | Checks numeric value is within `[min, max]` |
| `confidence` | `number_range` | `{"field": "confidence_score", "min": 0.7, "max": 1.0, "message": "..."}` | Separate `number_range` engine ID for confidence score |
| `arithmetic` | `arithmetic` | `{"expression": "subtotal + tax - total", "tolerance": 1000, "direction": "abs", "message": "..."}` | Safe AST math check (`abs`/`min`/`max`/`round`/`int`/`float`, no `eval`) |
| `date_range` | `date_range` | `{"field": "receipt_date", "granularity": "DAILY", "target_month": "{{target_month}}"}` | Validates parsed date against target month/granularity |
| `period_granularity` | `period_granularity` | `{"expected": "WEEKLY"}` | Validates extracted period granularity |
| `constraints` | `constraints` | `{"constraints": "{{constraints}}"}` | Runs dynamic period/range constraints from a `constraints` customizable field |

### 4.2 Domain-Specific Pre-Registered Engines (Also Available)
- `authentic_app` (`enum`), `authentic_profile` (`boolean`), `authentic_ui_flag` (`boolean`), `profile_app_allowed` (`enum`), `profile_member_tier_required` (`required_fields`), `vehicle_authentic` (`boolean`), `vehicle_plate_required` (`required_fields`), `vehicle_view_allowed` (`enum`), `arithmetic_income_minus_costs` (`arithmetic`), `arithmetic_net_within_gross` (`arithmetic`), `net_when_no_gross` (`number_range`).

---

## 5. Creating the Object: Mode A (Direct API) vs Mode B (Guided Copy-Paste)

### Mode A: AI Agent Creates the Object Directly via API

Requires a `manager` or `superadmin` account (signed session cookie or HTTP Basic auth).

1. **Optional: Check live enabled engines first**
   ```http
   GET /api/v1/image-objects
   Accept: application/json
   ```
2. **Optional: Create a new engine module** (only if you need a second engine with the same `implementation`, e.g. a second `regex` check):
   - Submit `POST /admin/modules` (`application/x-www-form-urlencoded`) with:
     - `module_id`: e.g. `receipt_code_regex`
     - `module_type`: `engine`
     - `display_name`: `Kiểm tra mã hóa đơn`
     - `module_version`: `1`
     - `definition`:
       ```json
       {
         "version": 1,
         "id": "receipt_code_regex",
         "label": "Kiểm tra mã hóa đơn",
         "description": "Kiểm tra định dạng mã hóa đơn",
         "implementation": "regex",
         "params_schema": [
           {"key": "field", "label": "Trường", "type": "text", "required": true},
           {"key": "pattern", "label": "Regex", "type": "text", "required": true},
           {"key": "message", "label": "Thông báo lỗi", "type": "text", "required": false}
         ],
         "conditions_schema": []
       }
       ```
3. **Create & save the Image Object in one shot**
   ```http
   POST /admin/image-objects/<object_id>
   Content-Type: application/json
   Accept: application/json
   ```
   Body: the complete `ImageObjectDefinition` JSON (see examples in Section 6).
   - Success response: `200 OK` -> `{"status": "ok", "object_id": "<object_id>"}`.
   - Validation error: `400 Bad Request` -> `{"detail": "..."}` (fix the reported field and retry).
   - This saves both `data/image_objects/<object_id>.json` and mirrors the object into PostgreSQL `admin_module_registry`.

---

### Mode B: AI Agent Guides Customer to Create via Admin UI (Copy-Paste)

#### Path 1 (Fastest — One-Shot JSON Paste on `/admin/modules`)
Tell the customer:
1. Log in as `manager` or `superadmin` and open **Module** (`/admin/modules`).
2. Expand **Tạo module** at the top.
3. Fill the 4 header inputs:
   - **ID module**: `<object_id>` (e.g. `fuel_receipt`)
   - **Loại**: `Object ảnh` (`object`)
   - **Tên**: `<label>` (e.g. `Hóa đơn đổ xăng / sạc điện`)
   - **Phiên bản module**: `1`
4. Clear **JSON định nghĩa** and paste the complete JSON block provided by the AI agent.
5. Click **Tạo module**. Verify it appears in the list with badge `đang bật`.

#### Path 2 (Field-by-Field Form on `/admin/image-objects`)
Tell the customer:
1. Log in as `manager` or `superadmin` and open **Ảnh/Object** (`/admin/image-objects`).
2. Expand **Tạo object mới**:
   - **ID**: `<object_id>` (lowercase `a-z0-9_`)
   - **Nhãn**: `<label>`
   - **Bộ trích xuất**: `generic`
   - **Vai trò phân loại**: `<classify_role>`
   - Click **Tạo object**.
3. Scroll down to **Object ảnh**, expand the newly created `<object_id>` card, and paste each field provided by the AI agent:
   - **Mô tả** (`description`)
   - **Prompt** (`prompt`)
   - **Đối tượng (mỗi dòng một mục)** (`objects` — plain text, one item per line, NOT JSON)
   - **Trường bắt buộc (JSON)** (`expected_fields` — JSON array)
   - **Tùy biến (JSON)** (`customizable` — JSON array)
   - **Engine (JSON)** (`engines` — JSON array)
4. Click **Lưu object**.
   - *Tip*: If you need `"json_output_instruction": true` toggled on explicitly, open the same object in `/admin/modules` and set `"json_output_instruction": true` in its JSON (Path 1 sets it immediately in one step).

---

### Final Step (Required for Both Modes): Attach the Image Object to a Survey Form

Tell the customer:
1. Open **Biểu mẫu** (`/admin/forms`) and click the pencil icon to edit the target survey.
2. Open tab **Tùy chọn & Cài đặt** -> ensure **Bật AI Vision** (`enable_ai_vision`) and **Bật nhận diện đối tượng** (`enable_object_detection`) are enabled as needed.
3. Open tab **Danh sách câu hỏi** -> find or add a **Camera (WebRTC)** (`camera_webrtc`) question -> edit its **Image Slots** (`image_slots`):
   - Select **`object_type`**: `<object_id>` (this overrides legacy role/title matching).
   - Adjust any **customizable** options exposed by the object (e.g. toggles/allowed values).
4. Click **Lưu & Kích hoạt biểu mẫu**.
5. Test with a sample submission (enable `is_test_mode` or use a `private` draft form first) and verify the extracted fields and pass/reject reasons in `/admin/submissions`.

---

## 6. Worked Copy-Paste Examples (Schema-Verified)

### Example 1: Fuel / Charging Receipt (`fuel_receipt`)

Complete JSON (for `/admin/modules` or `POST /admin/image-objects/fuel_receipt`):

```json
{
  "version": 1,
  "id": "fuel_receipt",
  "label": "Hóa đơn đổ xăng / sạc điện",
  "description": "Ảnh chụp hóa đơn thanh toán nhiên liệu hoặc phiên sạc xe điện của tài xế.",
  "prompt": "You are an OCR extraction engine for Vietnamese fuel and EV charging receipts. Detect: {{objects}}. Expected fields: {{fields}}. Configuration: {{config}}. Transcribe only visible text and numbers. Extract station_name, receipt_date (YYYY-MM-DD if visible), total_amount_vnd (integer VND without separators), payment_method (CASH/CARD/E_WALLET/OTHER), is_legible (boolean), and is_authentic (false if not a real receipt, mockup, or digitally edited). Never guess; use null for absent fields.",
  "objects": [
    "printed or digital fuel/charging receipt",
    "station or provider header",
    "total payment amount line"
  ],
  "expected_fields": [
    {"key": "station_name", "label": "Tên trạm / nhà cung cấp", "type": "string", "required": true},
    {"key": "receipt_date", "label": "Ngày hóa đơn", "type": "string", "required": false},
    {"key": "total_amount_vnd", "label": "Tổng tiền (VND)", "type": "integer", "required": true},
    {"key": "payment_method", "label": "Phương thức thanh toán", "type": "string", "required": false},
    {"key": "is_legible", "label": "Ảnh rõ nét", "type": "boolean", "required": true},
    {"key": "is_authentic", "label": "Hóa đơn thật", "type": "boolean", "required": true}
  ],
  "customizable": [
    {"key": "require_authentic", "label": "Bắt buộc hóa đơn thật", "type": "toggle", "default": true},
    {"key": "min_amount_vnd", "label": "Số tiền tối thiểu (VND)", "type": "number", "default": 10000}
  ],
  "engines": [
    {
      "id": "required_fields",
      "label": "Bắt buộc tên trạm và tổng tiền",
      "params": {"fields": ["station_name", "total_amount_vnd"]},
      "conditions": []
    },
    {
      "id": "boolean",
      "label": "Kiểm tra hóa đơn thật",
      "params": {
        "field": "is_authentic",
        "expected": true,
        "required": true,
        "message": "Không phát hiện hóa đơn đổ xăng/sạc điện hợp lệ."
      },
      "conditions": [
        {"field": "$config.require_authentic", "op": "equals", "value": true}
      ]
    },
    {
      "id": "legible",
      "label": "Ảnh đọc được",
      "params": {
        "field": "is_legible",
        "expected": true,
        "message": "Ảnh hóa đơn quá mờ, không đọc được thông tin."
      },
      "conditions": [{"field": "is_legible", "op": "not_empty"}]
    },
    {
      "id": "number_range",
      "label": "Kiểm tra số tiền hợp lệ",
      "params": {
        "field": "total_amount_vnd",
        "min": "{{min_amount_vnd}}",
        "max": 50000000,
        "message": "Tổng tiền trên hóa đơn nằm ngoài khoảng hợp lệ."
      },
      "conditions": [{"field": "total_amount_vnd", "op": "not_empty"}]
    }
  ],
  "extractor": "generic",
  "enabled": true,
  "json_output_instruction": true,
  "classify_role": "fuel_receipt"
}
```

### Example 2: Driver App Wallet / Rating Screen (`driver_rating_screen`)

Complete JSON (for `/admin/modules` or `POST /admin/image-objects/driver_rating_screen`):

```json
{
  "version": 1,
  "id": "driver_rating_screen",
  "label": "Màn hình điểm đánh giá tài xế",
  "description": "Ảnh chụp màn hình điểm sao và tỷ lệ hoàn thành trên ứng dụng tài xế.",
  "prompt": "You are an OCR extraction engine for Vietnamese ride-hailing driver rating screens. Detect: {{objects}}. Expected fields: {{fields}}. Configuration: {{config}}. Transcribe only visible values: app_name (XANH_SM/GRAB/BE/OTHER), star_rating (number 0-5), acceptance_rate_pct (number 0-100), cancellation_rate_pct (number 0-100), is_authentic (false for mockups, edited images, or unrelated screens). Never guess; use null for absent fields.",
  "objects": [
    "driver app rating summary screen",
    "star rating score",
    "acceptance or completion rate metric"
  ],
  "expected_fields": [
    {"key": "app_name", "label": "Ứng dụng", "type": "string", "required": true},
    {"key": "star_rating", "label": "Điểm sao", "type": "number", "required": true},
    {"key": "acceptance_rate_pct", "label": "Tỷ lệ nhận chuyến (%)", "type": "number", "required": false},
    {"key": "cancellation_rate_pct", "label": "Tỷ lệ hủy chuyến (%)", "type": "number", "required": false},
    {"key": "is_authentic", "label": "Màn hình chính thức", "type": "boolean", "required": true}
  ],
  "customizable": [
    {"key": "allowed_apps", "label": "Ứng dụng cho phép (dấu phẩy)", "type": "text", "default": "XANH_SM,GRAB,BE"},
    {"key": "min_star_rating", "label": "Điểm sao tối thiểu", "type": "number", "default": 4.0}
  ],
  "engines": [
    {
      "id": "required_fields",
      "label": "Bắt buộc ứng dụng và điểm sao",
      "params": {"fields": ["app_name", "star_rating"]},
      "conditions": []
    },
    {
      "id": "enum",
      "label": "Ứng dụng hợp lệ",
      "params": {
        "field": "app_name",
        "allowed": "{{allowed_apps}}",
        "required": true,
        "message": "Ứng dụng trên màn hình không thuộc danh sách cho phép."
      },
      "conditions": []
    },
    {
      "id": "number_range",
      "label": "Điểm sao hợp lệ",
      "params": {
        "field": "star_rating",
        "min": "{{min_star_rating}}",
        "max": 5.0,
        "message": "Điểm sao tài xế không đạt mức tối thiểu."
      },
      "conditions": [{"field": "star_rating", "op": "not_empty"}]
    },
    {
      "id": "boolean",
      "label": "Ảnh màn hình thật",
      "params": {
        "field": "is_authentic",
        "expected": true,
        "required": true,
        "message": "Không phát hiện màn hình đánh giá tài xế hợp lệ."
      },
      "conditions": []
    }
  ],
  "extractor": "generic",
  "enabled": true,
  "json_output_instruction": true,
  "classify_role": "driver_rating_screen"
}
```

---

## 7. Common Validation Errors & How an AI Agent Fixes Them

| Error Returned on Save | Cause | Fix |
|---|---|---|
| `Extra inputs are not permitted` | An undocumented key was included in the object, `expected_fields`, `customizable`, or `engines` | Remove any key not listed in Section 3 |
| `references missing engine(s): <id>` | `engines[].id` is not registered/enabled in the engine registry | Replace with an ID from Section 4 or create the engine module at `/admin/modules` first |
| `engine bindings must be unique` | Same engine `id` used twice in `engines[]` | Use another pre-registered engine ID with the same `implementation` (e.g. `boolean` + `legible`), or create a second engine module |
| `customizable select '<k>' requires non-empty options` / `only accepts options when type is select` | `options` missing on a `select`, or present on a non-`select` field | Only include non-empty `options` when `type == "select"` |
| `expected_fields keys must be unique` | Duplicate `key` in `expected_fields` | Deduplicate field keys |
| `module id is already registered with another type` | Chosen `id` collides with an existing `logic` or `engine` module ID | Pick a distinct `snake_case` `id` |
