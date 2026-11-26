# -*- coding: utf-8 -*-
"""
Image object types and check engines.

Object files live in `data/image_objects/<id>.json`; engine files in
`data/check_engines/<id>.json` are the seed/fallback. After migration 011,
enabled check engines are database-backed `engine` modules in
`admin_module_registry` and replace the file registry at startup. JSON never
contains executable code: the `implementation` field maps to the built-in
registry in `services.image_check_engines.py`.
"""

import json
import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from services.image_check_engines import (
    CONDITION_OPS,
    ENGINE_IMPLEMENTATIONS,
    resolve_placeholders,
    run_engine,
)
from services.image_types import normalize_ocr_field_aliases

logger = logging.getLogger("services.image_objects")

CUSTOMIZABLE_TYPES = ("text", "number", "select", "toggle", "month", "date", "textarea", "constraints")
EXPECTED_FIELD_TYPES = ("string", "integer", "number", "boolean", "array", "object")


class ImageObjectDefinitionError(ValueError):
    """Raised when an object/engine definition is missing or unusable."""


# ==============================================================================
# Definition models
# ==============================================================================

class ExpectedField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str = "string"
    required: bool = False
    description: Optional[str] = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in EXPECTED_FIELD_TYPES:
            raise ValueError(f"expected field type must be one of {', '.join(EXPECTED_FIELD_TYPES)}")
        return value


class CustomizableField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str = "text"
    options: Optional[List[Any]] = None
    default: Any = None
    help: Optional[str] = None
    item_fields: Optional[List[Dict[str, Any]]] = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in CUSTOMIZABLE_TYPES:
            raise ValueError(f"customizable type must be one of {', '.join(CUSTOMIZABLE_TYPES)}")
        return value

    @model_validator(mode="after")
    def _options_contract(self) -> "CustomizableField":
        if self.type == "select":
            if not self.options:
                raise ValueError(f"customizable select '{self.key}' requires non-empty options")
        elif self.options is not None:
            raise ValueError(f"customizable '{self.key}' only accepts options when type is select")

        if self.type == "constraints":
            if not self.item_fields:
                raise ValueError(f"customizable constraints '{self.key}' requires item_fields")
            for item in self.item_fields:
                if not isinstance(item, dict) or not item.get("key") or not item.get("label"):
                    raise ValueError(
                        f"customizable constraints '{self.key}' item_fields need key and label"
                    )
        elif self.item_fields is not None:
            raise ValueError(f"customizable '{self.key}' only accepts item_fields when type is constraints")
        return self


class EngineBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    conditions: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("conditions")
    @classmethod
    def _valid_conditions(cls, value: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for condition in value:
            if not isinstance(condition, dict) or not condition.get("field"):
                raise ValueError("condition requires a 'field'")
            op = condition.get("op", "not_empty")
            if op not in CONDITION_OPS:
                raise ValueError(f"condition op must be one of {', '.join(CONDITION_OPS)}")
        return value


class ImageObjectDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    label: str
    description: str = ""
    prompt: str = ""
    objects: List[str] = Field(default_factory=list)
    expected_fields: List[ExpectedField] = Field(default_factory=list)
    customizable: List[CustomizableField] = Field(default_factory=list)
    engines: List[EngineBinding] = Field(default_factory=list)
    # Built-in extraction profile (income_statement/profile/vehicle/generic/none).
    # This is a registry key, never executable code.
    extractor: str = "generic"
    # Disabled definitions remain visible to administrators but cannot resolve in surveys.
    enabled: bool = True
    json_output_instruction: bool = False
    classify_role: Optional[str] = None

    @model_validator(mode="after")
    def _unique_keys(self) -> "ImageObjectDefinition":
        keys = [f.key for f in self.expected_fields]
        if len(keys) != len(set(keys)):
            raise ValueError("expected_fields keys must be unique")
        custom_keys = [f.key for f in self.customizable]
        if len(custom_keys) != len(set(custom_keys)):
            raise ValueError("customizable keys must be unique")
        engine_ids = [e.id for e in self.engines]
        if len(engine_ids) != len(set(engine_ids)):
            raise ValueError("engine bindings must be unique")
        return self


class EngineSchemaField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    type: str = "text"
    options: Optional[List[Any]] = None
    default: Any = None
    required: bool = False


class EngineDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    label: str
    description: str = ""
    implementation: str
    params_schema: List[EngineSchemaField] = Field(default_factory=list)
    conditions_schema: List[EngineSchemaField] = Field(default_factory=list)

    @field_validator("implementation")
    @classmethod
    def _known_implementation(cls, value: str) -> str:
        if value not in ENGINE_IMPLEMENTATIONS:
            raise ValueError(
                f"unknown implementation '{value}'; allowed: {', '.join(sorted(ENGINE_IMPLEMENTATIONS))}"
            )
        return value


# ==============================================================================
# Paths & cache
# ==============================================================================

def _data_root() -> str:
    return os.path.join(os.getcwd(), "data")


def objects_dir() -> str:
    return os.path.join(_data_root(), "image_objects")


def engines_dir() -> str:
    return os.path.join(_data_root(), "check_engines")


_CACHE: Dict[str, Any] = {"signature": None, "registry": None}
_DATABASE_OBJECTS: Optional[Dict[str, ImageObjectDefinition]] = None
_DATABASE_ENGINES: Optional[Dict[str, EngineDefinition]] = None


def set_database_object_registry(objects: Dict[str, ImageObjectDefinition]) -> None:
    """Replace file objects with the latest database-backed catalog snapshot."""
    global _DATABASE_OBJECTS
    _DATABASE_OBJECTS = dict(objects)
    cached = _CACHE.get("registry")
    if cached is not None:
        updated = dict(cached)
        updated["objects"] = dict(_DATABASE_OBJECTS)
        updated["objects_raw"] = {key: value.model_dump(mode="json") for key, value in _DATABASE_OBJECTS.items()}
        _CACHE["registry"] = updated


def set_database_engine_registry(engines: Dict[str, EngineDefinition]) -> None:
    """Replace file engines with the latest database-backed module snapshot."""
    global _DATABASE_ENGINES
    _DATABASE_ENGINES = dict(engines)
    cached = _CACHE.get("registry")
    if cached is not None:
        updated = dict(cached)
        updated["engines"] = dict(_DATABASE_ENGINES)
        updated["engines_raw"] = {key: value.model_dump(mode="json") for key, value in _DATABASE_ENGINES.items()}
        _CACHE["registry"] = updated


def _scan_dir(directory: str) -> List[Tuple[str, int, int]]:
    if not os.path.isdir(directory):
        return []
    entries: List[Tuple[str, int, int]] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append((path, stat.st_mtime_ns, stat.st_size))
    return entries


def _signature() -> Tuple[Any, ...]:
    return tuple(_scan_dir(objects_dir())) + tuple(_scan_dir(engines_dir()))


def _load_file(path: str, model_cls):
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    model = model_cls.model_validate(raw)
    expected_id = os.path.splitext(os.path.basename(path))[0]
    if model.id != expected_id:
        raise ValueError(f"id '{model.id}' does not match filename '{expected_id}.json'")
    return model, raw


def load_registry(force: bool = False) -> Dict[str, Any]:
    """Load and validate all object/engine files; cached by file mtimes."""
    signature = _signature()
    if not force and _CACHE.get("registry") is not None and _CACHE.get("signature") == signature:
        return _CACHE["registry"]

    objects: Dict[str, ImageObjectDefinition] = {}
    engines: Dict[str, EngineDefinition] = {}
    object_raw: Dict[str, Dict[str, Any]] = {}
    engine_raw: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, str]] = []

    for path, _mtime, _size in _scan_dir(engines_dir()):
        try:
            model, raw = _load_file(path, EngineDefinition)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.error("[IMAGE ENGINE INVALID] %s: %s", path, exc)
            errors.append({"scope": "engine", "path": path, "file": os.path.basename(path), "error": str(exc)})
            continue
        engines[model.id] = model
        engine_raw[model.id] = raw

    if _DATABASE_ENGINES is not None:
        engines = dict(_DATABASE_ENGINES)
        engine_raw = {key: value.model_dump(mode="json") for key, value in engines.items()}

    for path, _mtime, _size in _scan_dir(objects_dir()):
        try:
            model, raw = _load_file(path, ImageObjectDefinition)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            logger.error("[IMAGE OBJECT INVALID] %s: %s", path, exc)
            errors.append({"scope": "object", "path": path, "file": os.path.basename(path), "error": str(exc)})
            continue
        missing_engines = sorted({binding.id for binding in model.engines if binding.id not in engines})
        if missing_engines:
            message = "references missing engine(s): " + ", ".join(missing_engines)
            logger.error("[IMAGE OBJECT INVALID] %s: %s", path, message)
            errors.append({"scope": "object", "path": path, "file": os.path.basename(path), "error": message})
        objects[model.id] = model
        object_raw[model.id] = raw

    if _DATABASE_OBJECTS is not None:
        objects = dict(_DATABASE_OBJECTS)
        object_raw = {key: value.model_dump(mode="json") for key, value in objects.items()}
    registry = {
        "objects": objects,
        "engines": engines,
        "objects_raw": object_raw,
        "engines_raw": engine_raw,
        "errors": errors,
        "paths": {"objects": objects_dir(), "engines": engines_dir()},
    }
    _CACHE["signature"] = signature
    _CACHE["registry"] = registry
    if errors:
        logger.error("[IMAGE OBJECTS] %d invalid definition file(s)", len(errors))
    return registry


def reload_registry() -> Dict[str, Any]:
    return load_registry(force=True)


def list_objects() -> List[ImageObjectDefinition]:
    return list(load_registry()["objects"].values())


def list_engines() -> List[EngineDefinition]:
    return list(load_registry()["engines"].values())


def get_object(object_id: str, *, require_enabled: bool = False) -> ImageObjectDefinition:
    registry = load_registry()
    obj = registry["objects"].get(str(object_id))
    if obj is None:
        raise ImageObjectDefinitionError(f"image object '{object_id}' is not defined in {objects_dir()}")
    missing = [binding.id for binding in obj.engines if binding.id not in registry["engines"]]
    if missing:
        raise ImageObjectDefinitionError(
            f"image object '{object_id}' references missing engine(s): {', '.join(missing)}"
        )
    if require_enabled and not obj.enabled:
        raise ImageObjectDefinitionError(f"image object '{object_id}' is disabled")
    return obj


def get_engine(engine_id: str) -> EngineDefinition:
    engine = load_registry()["engines"].get(str(engine_id))
    if engine is None:
        raise ImageObjectDefinitionError(f"check engine '{engine_id}' is not defined in {engines_dir()}")
    return engine


def get_definition_errors() -> List[Dict[str, str]]:
    return list(load_registry()["errors"])


# ==============================================================================
# Config & prompt
# ==============================================================================

def merged_config(obj: ImageObjectDefinition, slot_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = {field.key: field.default for field in obj.customizable}
    for key, value in (slot_config or {}).items():
        if value is not None:
            config[key] = value
    return config


def build_detection_prompt(obj: ImageObjectDefinition, slot_config: Optional[Dict[str, Any]] = None) -> str:
    """Build the AI detection prompt from the object definition + slot config."""
    config = merged_config(obj, slot_config)
    fields = "; ".join(
        f"{field.label} ({field.key}, {field.type}{', required' if field.required else ''})"
        for field in obj.expected_fields
    ) or "none"
    objects = ", ".join(obj.objects) or obj.label
    template = obj.prompt or "Analyze the image. Detect: {{objects}}. Expected fields: {{fields}}. Configuration: {{config}}."
    prompt = (
        template.replace("{{objects}}", objects)
        .replace("{{fields}}", fields)
        .replace("{{config}}", json.dumps(config, ensure_ascii=False, sort_keys=True))
    )
    if obj.expected_fields and obj.json_output_instruction:
        keys = ", ".join(field.key for field in obj.expected_fields)
        prompt = f'{prompt}\nReturn only one valid JSON object with exactly these keys: {keys}. Include every key; use null when a value is not visible. Do not return prose or Markdown.'
    elif obj.expected_fields:
        keys = ", ".join(field.key for field in obj.expected_fields)
        prompt = f"{prompt}\nUse exactly these snake_case JSON keys, untranslated: {keys}."
    return prompt.strip()


# ==============================================================================
# Extraction validation & engine execution
# ==============================================================================

def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d[\d.,]*", value)
        if not match:
            return None
        digits = match.group(0).replace(".", "").replace(",", "")
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:[.,]\d+)?", value)
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", "."))
        except ValueError:
            return None
    return None


def normalize_extraction(obj: ImageObjectDefinition, extraction: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Alias legacy keys and coerce declared field types; returns (values, issues)."""
    if hasattr(extraction, "model_dump"):
        extraction = extraction.model_dump()
    data: Dict[str, Any] = normalize_ocr_field_aliases(dict(extraction or {}))
    if data.get("period_raw_text") in (None, "") and data.get("period"):
        data["period_raw_text"] = data["period"]
    if data.get("active_tab") in (None, "") and data.get("period_type"):
        data["active_tab"] = data["period_type"]

    issues: List[str] = []
    for field in obj.expected_fields:
        value = data.get(field.key)
        if value in (None, ""):
            continue
        if field.type == "integer":
            coerced = _coerce_int(value)
            if coerced is None:
                issues.append(f"{field.key} phải là số nguyên")
            else:
                data[field.key] = coerced
        elif field.type == "number":
            coerced_f = _coerce_float(value)
            if coerced_f is None:
                issues.append(f"{field.key} phải là số")
            else:
                data[field.key] = coerced_f
        elif field.type == "boolean":
            if isinstance(value, str):
                data[field.key] = value.strip().lower() in ("true", "1", "yes", "có", "co")
            else:
                data[field.key] = bool(value)
        elif field.type == "string" and not isinstance(value, str):
            data[field.key] = str(value)
    return data, issues


def run_definition_checks(
    obj: ImageObjectDefinition,
    extraction: Any,
    slot_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate extraction fields and execute a definition's bound engines.

    Works for both on-disk objects and transient compatibility objects built
    from a custom slot prompt.
    """
    registry = load_registry()
    config = merged_config(obj, slot_config)
    values, issues = normalize_extraction(obj, extraction)

    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = [
        {"engine_id": "field_types", "reason": issue, "details": {}} for issue in issues
    ]
    # A field marked required is mandatory even when the definition forgot to
    # bind an engine: otherwise a partly hallucinated OCR response passes.
    if "required_fields" not in {binding.id for binding in obj.engines}:
        engine_def = registry["engines"].get("required_fields")
        if engine_def is not None and any(field.required for field in obj.expected_fields):
            implicit = run_engine(
                engine_def.model_dump(),
                {"id": "required_fields", "params": {}},
                values,
                config,
                context={"object": obj.model_dump(), "slot_config": slot_config or {}},
            )
            results.append(implicit)
            if not implicit.get("passed"):
                failures.append({
                    "engine_id": implicit["engine_id"],
                    "reason": implicit.get("reason") or "Thiếu trường dữ liệu bắt buộc.",
                    "details": implicit.get("details") or {},
                })
    for binding in obj.engines:
        engine_def = registry["engines"].get(binding.id)
        if engine_def is None:
            raise ImageObjectDefinitionError(f"image object '{obj.id}' references missing engine '{binding.id}'")
        result = run_engine(
            engine_def.model_dump(),
            binding.model_dump(),
            values,
            config,
            context={"object": obj.model_dump(), "slot_config": slot_config or {}},
        )
        results.append(result)
        if not result.get("passed"):
            failures.append(
                {
                    "engine_id": result["engine_id"],
                    "reason": result.get("reason") or "Kiểm tra không đạt",
                    "details": result.get("details") or {},
                }
            )
    return {
        "object_type": obj.id,
        "passed": not failures,
        "failures": failures,
        "reasons": [failure["reason"] for failure in failures],
        "engines": results,
        "extraction": values,
        "config": config,
    }


def run_object_checks(
    object_id: str,
    extraction: Any,
    slot_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate extraction fields and execute the object's bound engines."""
    return run_definition_checks(get_object(object_id, require_enabled=True), extraction, slot_config)


# ==============================================================================
# Slot compatibility resolver
# ==============================================================================

_ROLE_ALIASES = {
    "gsm_profile_screen": "gsm-profile-screen",
    "app_profile": "app_profile",
    "profile": "app_profile",
    "app_income": "weekly_income",
    "income": "weekly_income",
    "revenue_screen": "weekly_income",
    "weekly_income": "weekly_income",
    "vehicle_exterior": "vehicle_exterior_or_interior",
    "vehicle_exterior_or_interior": "vehicle_exterior_or_interior",
    "vehicle_interior": "vehicle_exterior_or_interior",
}


def object_type_for_classified_role(role: Any) -> Optional[str]:
    value = str(role or "").strip().lower()
    if value in _ROLE_ALIASES:
        return _ROLE_ALIASES[value]
    if value.startswith("app_income_week_") or value.startswith("income_week_"):
        return "weekly_income"
    return None


def _legacy_object_for_slot(slot: Dict[str, Any]) -> ImageObjectDefinition:
    role = str(slot.get("role") or "custom").strip()
    title = str(slot.get("title") or role or "Ảnh xác thực")
    return ImageObjectDefinition(
        version=1,
        id=f"legacy_{role or 'photo'}",
        label=title,
        description="Legacy slot without a defined image object type.",
        prompt="Analyze the image for: {{objects}}. Expected fields: {{fields}}. Configuration: {{config}}.",
        objects=[title],
        expected_fields=[],
        customizable=[],
        engines=[],
        extractor="generic",
    )


def _custom_prompt_object(slot: Dict[str, Any]) -> ImageObjectDefinition:
    """Explicit custom-prompt mode: generic object, no hardcoded semantics."""
    role = str(slot.get("role") or "custom").strip()
    title = str(slot.get("title") or role or "Ảnh xác thực")
    return ImageObjectDefinition(
        version=1,
        id=f"custom_{role or 'photo'}",
        label=title,
        description="Slot-defined custom AI prompt; no hardcoded object checks.",
        prompt=str(slot.get("ai_detection_prompt") or "").strip(),
        objects=[title],
        expected_fields=[],
        customizable=[],
        engines=[],
        extractor="generic",
        classify_role=None,
    )


def resolve_slot_object(slot: Any) -> ImageObjectDefinition:
    """Resolve a slot to an object definition.

    Precedence:
    1. Explicit `object_type` is authoritative.
    2. A non-empty `ai_detection_prompt` without `object_type` is custom-prompt
       mode: a generic object with no hardcoded weekly/profile/vehicle checks.
    3. Legacy role/title compatibility maps to a known object.
    4. Fallback generic object.
    """
    if not isinstance(slot, dict):
        return _legacy_object_for_slot({})
    object_type = str(slot.get("object_type") or "").strip()
    if object_type:
        return get_object(object_type, require_enabled=True)

    if str(slot.get("ai_detection_prompt") or "").strip():
        return _custom_prompt_object(slot)

    role = str(slot.get("role") or "").strip().lower()
    resolved = object_type_for_classified_role(role)
    if resolved:
        return get_object(resolved, require_enabled=True)

    title = str(slot.get("title") or "").lower()
    if "hồ sơ" in title or "profile" in title or "màn hình chính" in title:
        return get_object("app_profile", require_enabled=True)
    if "thu nhập" in title or "income" in title or "tuần" in title:
        return get_object("weekly_income", require_enabled=True)
    if "thân xe" in title or "đầu xe" in title or "vô lăng" in title or "phương tiện" in title:
        return get_object("vehicle_exterior_or_interior", require_enabled=True)
    return _legacy_object_for_slot(slot)


# ==============================================================================
# Atomic persistence
# ==============================================================================

def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def save_object(object_id: str, data: Dict[str, Any]) -> ImageObjectDefinition:
    payload = dict(data or {})
    payload["id"] = object_id
    model = ImageObjectDefinition.model_validate(payload)
    missing = [
        binding.id for binding in model.engines
        if binding.id not in load_registry()["engines"]
    ]
    if missing:
        raise ImageObjectDefinitionError("references missing engine(s): " + ", ".join(missing))
    _atomic_write_json(os.path.join(objects_dir(), f"{object_id}.json"), model.model_dump(mode="json"))
    reload_registry()
    logger.info("[IMAGE OBJECTS] saved %s", object_id)
    return get_object(object_id)


def delete_object(object_id: str) -> bool:
    path = os.path.join(objects_dir(), f"{object_id}.json")
    if not os.path.exists(path):
        return False
    os.unlink(path)
    reload_registry()
    logger.info("[IMAGE OBJECTS] deleted %s", object_id)
    return True


def save_engine(engine_id: str, data: Dict[str, Any]) -> EngineDefinition:
    payload = dict(data or {})
    payload["id"] = engine_id
    model = EngineDefinition.model_validate(payload)
    _atomic_write_json(os.path.join(engines_dir(), f"{engine_id}.json"), model.model_dump(mode="json"))
    reload_registry()
    logger.info("[IMAGE ENGINES] saved %s", engine_id)
    return get_engine(engine_id)
