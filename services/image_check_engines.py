# -*- coding: utf-8 -*-
"""
Built-in declarative image check engines.

Object JSON files reference these implementations by name only; no Python code
is ever imported from JSON. Conditions gate engine execution and `{{param}}`
placeholders resolve against the object's customizable defaults plus the slot's
`object_config`.
"""

import ast
import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.image_types import TimeGranularity, parse_date_range_from_text

logger = logging.getLogger("services.image_check_engines")

CONDITION_OPS = (
    "not_empty",
    "empty",
    "equals",
    "not_equals",
    "contains",
    "regex",
    "gte",
    "lte",
)

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


class EngineDefinitionError(ValueError):
    """Raised for an unknown implementation or malformed engine parameters."""


# ==============================================================================
# Value helpers
# ==============================================================================

def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _to_number(value: Any) -> Optional[float]:
    """Coerce extracted currency/count values to a number, or None."""
    if value is None or isinstance(value, bool):
        return None if value is None else float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"-?\d+(?:\.\d+)?[eE][+-]?\d+", text):
            try:
                return float(text)
            except ValueError:
                return None
        match = re.search(r"-?\d[\d.,]*", text)
        if not match:
            return None
        raw = match.group(0)
        # Vietnamese formatting: 13.461.500 -> 13461500; 1,234,567.5 stays decimal.
        if "," in raw and "." in raw:
            raw = raw.replace(".", "").replace(",", ".")
        elif raw.count(".") > 1 or re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", raw):
            raw = raw.replace(".", "")
        elif raw.count(",") > 1 or re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", raw):
            raw = raw.replace(",", "")
        else:
            raw = raw.replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _field_value(extraction: Dict[str, Any], field: str) -> Any:
    current: Any = extraction
    for part in str(field or "").split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def resolve_placeholders(value: Any, config: Dict[str, Any]) -> Any:
    """Resolve `{{key}}` placeholders; exact placeholders keep the raw type."""
    if isinstance(value, str):
        exact = PLACEHOLDER_RE.fullmatch(value.strip())
        if exact:
            return config.get(exact.group(1), value)
        return PLACEHOLDER_RE.sub(
            lambda m: str(config.get(m.group(1), m.group(0))),
            value,
        )
    if isinstance(value, list):
        return [resolve_placeholders(item, config) for item in value]
    if isinstance(value, dict):
        return {k: resolve_placeholders(v, config) for k, v in value.items()}
    return value


# ==============================================================================
# Conditions
# ==============================================================================

def evaluate_condition(
    condition: Dict[str, Any], extraction: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> bool:
    field = str(condition.get("field") or "")
    op = str(condition.get("op") or "not_empty")
    expected = condition.get("value")
    if field.startswith("$config."):
        actual = _field_value(config or {}, field[len("$config."):])
    else:
        actual = _field_value(extraction, field)

    if op == "not_empty":
        return not _is_empty(actual)
    if op == "empty":
        return _is_empty(actual)
    if op == "equals":
        if isinstance(actual, str) and isinstance(expected, str):
            return actual.strip().lower() == expected.strip().lower()
        return actual == expected
    if op == "not_equals":
        return not evaluate_condition({**condition, "op": "equals"}, extraction, config)
    if op == "contains":
        if isinstance(actual, (list, tuple, set)):
            return expected in actual or str(expected) in {str(item) for item in actual}
        return str(expected or "").lower() in str(actual or "").lower()
    if op == "regex":
        return bool(re.search(str(expected or ""), str(actual or "")))
    if op in ("gte", "lte"):
        left, right = _to_number(actual), _to_number(expected)
        if left is None or right is None:
            return False
        return left >= right if op == "gte" else left <= right
    raise EngineDefinitionError(f"Unknown condition op: {op}")


def conditions_pass(
    conditions: Iterable[Dict[str, Any]],
    extraction: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    return all(evaluate_condition(c, extraction, config) for c in (conditions or []))


# ==============================================================================
# Safe arithmetic (AST, no eval)
# ==============================================================================

_ALLOWED_FUNCS = {"abs": abs, "min": min, "max": max, "round": round, "int": int, "float": float}
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Compare,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
    ast.BoolOp,
    ast.And,
    ast.Or,
)


def _eval_node(node: ast.AST, context: Dict[str, float]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        raise EngineDefinitionError("Arithmetic expression contains a non-numeric constant")
    if isinstance(node, ast.Name):
        if node.id in context:
            return context[node.id]
        raise EngineDefinitionError(f"Unknown arithmetic field: {node.id}")
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, context)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise EngineDefinitionError("Unsupported unary operator")
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right if right else 0.0
        if isinstance(node.op, ast.Mod):
            return left % right if right else 0.0
        raise EngineDefinitionError("Unsupported arithmetic operator")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise EngineDefinitionError("Unsupported arithmetic function")
        args = [_eval_node(arg, context) for arg in node.args]
        return _ALLOWED_FUNCS[node.func.id](*args)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, context)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, context)
            if isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            else:
                raise EngineDefinitionError("Unsupported comparison operator")
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(value, context) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    raise EngineDefinitionError("Unsupported arithmetic expression node")


def safe_arithmetic(expression: str, values: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Evaluate a numeric expression with ast; returns (value, error)."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return None, f"Công thức không hợp lệ: {exc.msg}"
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return None, "Công thức chứa thành phần không được phép"
    try:
        result = _eval_node(tree, values)
    except EngineDefinitionError as exc:
        return None, str(exc)
    except ZeroDivisionError:
        return None, "Công thức chia cho 0"
    if isinstance(result, bool):
        return None, "Công thức phải trả về số"
    return float(result), None


# ==============================================================================
# Engines: signature (params, extraction, context) -> dict
# ==============================================================================

def _result(passed: bool, reason: str = "", details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"passed": passed, "reason": reason if not passed else "", "details": details or {}}


def engine_required_fields(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    fields = params.get("fields")
    if not fields:
        object_def = context.get("object") or {}
        fields = [
            f["key"] for f in object_def.get("expected_fields", [])
            if isinstance(f, dict) and f.get("required")
        ]
    missing = [str(f) for f in (fields or []) if _is_empty(_field_value(extraction, str(f)))]
    if missing:
        return _result(False, "Thiếu trường dữ liệu bắt buộc: " + ", ".join(missing), {"missing": missing})
    return _result(True)


def _month_key(value: Any) -> Optional[Tuple[int, int]]:
    text = str(value or "").strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{1,2})[/-](\d{4})", text)
    if m:
        return int(m.group(2)), int(m.group(1))
    return None


def _months_spanned(start: date, end: date) -> set:
    """Every (year, month) touched by an inclusive date range."""
    months = {(start.year, start.month)}
    cursor = start
    while True:
        year, month = cursor.year, cursor.month + 1
        if month > 12:
            year, month = year + 1, 1
        cursor = date(year, month, 1)
        if cursor > end:
            break
        months.add((cursor.year, cursor.month))
    return months


_DATE_LIKE_RE = re.compile(
    r"\d{1,2}\s*[/-]\s*\d{1,2}\s*[/-]\s*\d{2,4}"
    r"|\d{1,2}\s*[/-]\s*\d{1,2}\s*[-–]\s*\d{1,2}"
)

# Explicit duration windows per period type (inclusive days).
PERIOD_TYPE_WINDOWS = {
    "DAILY": (1, 1),
    "WEEKLY": (2, 14),
    "MONTHLY": (25, 31),
    "QUARTERLY": (80, 100),
    "YEARLY": (350, 380),
}


def _period_type_from_params(params: Dict[str, Any]) -> str:
    value = str(params.get("expected") or params.get("period_type") or "WEEKLY").upper()
    return value if value in PERIOD_TYPE_WINDOWS else "WEEKLY"


def _detected_period_type(extraction: Dict[str, Any], text: str) -> Optional[TimeGranularity]:
    raw = str(
        extraction.get("period_type")
        or extraction.get("active_tab")
        or extraction.get("period_granularity")
        or ""
    ).upper()
    if raw in TimeGranularity.__members__:
        return TimeGranularity(raw)
    lowered = (text or "").lower()
    if "tháng" in lowered or "toàn bộ tháng" in lowered:
        return TimeGranularity.MONTHLY
    if "quý" in lowered:
        return TimeGranularity.QUARTERLY
    if "năm" in lowered:
        return TimeGranularity.YEARLY
    return None


def _period_dates(text: str) -> Tuple[Optional[date], Optional[date], int]:
    return parse_date_range_from_text(text)


_DETAIL_DATE_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{2,4}))?")
_DAY_RANGE_RE = re.compile(r"(\d{1,2})\s*[-–~]\s*(\d{1,2})")


def _period_range_from_extraction(extraction: Dict[str, Any]) -> Tuple[Optional[date], Optional[date], int]:
    """Parse period text, using first_detail_date as month context for day-only tabs."""
    text = str(extraction.get("period_raw_text") or extraction.get("period") or "")
    start, end, days = _period_dates(text)
    if start and end:
        return start, end, days
    detail = _DETAIL_DATE_RE.search(str(extraction.get("first_detail_date") or ""))
    is_week_label = bool(_DAY_RANGE_RE.search(text)) or "tuần" in text.lower()
    if detail and is_week_label:
        day = int(detail.group(1))
        month = int(detail.group(2))
        year = int(detail.group(3) or 2026)
        if year < 100:
            year += 2000
        try:
            from services.income_layout import parse_tab_period
            info = parse_tab_period(text, f"{month:02d}/{year}", year)
            if info.start and info.end:
                return info.start, info.end, info.days
        except Exception:
            logger.warning("[PERIOD] detail-date context parse failed for %r", text)
        try:
            return date(year, month, day), date(year, month, day) + timedelta(days=6), 7
        except ValueError:
            pass
    return None, None, 0


def _month_year_from_text(text: str) -> Optional[Tuple[int, int]]:
    match = re.search(r"(\d{1,2})\s*/\s*(\d{4})", text or "")
    if not match:
        return None
    month, year = int(match.group(1)), int(match.group(2))
    return (year, month) if 1 <= month <= 12 else None


def engine_date_range(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    field = str(params.get("field") or "period_raw_text")
    text = str(_field_value(extraction, field) or "")
    expected = _period_type_from_params(params)
    target_month = _month_key(params.get("target_month"))
    granularity = _detected_period_type(extraction, text)
    lowered = text.strip().lower()

    if granularity == TimeGranularity.MONTHLY or "tháng" in lowered:
        return _result(False, "Phát hiện ảnh chụp báo cáo doanh thu theo THÁNG. Biểu mẫu yêu cầu báo cáo thu nhập theo TUẦN (7 ngày)!")
    if granularity == TimeGranularity.DAILY or (("hôm nay" in lowered or "ngày" in lowered) and not any(sep in lowered for sep in ("-", "–", "đến"))):
        return _result(False, "Phát hiện tab 'Ngày' (báo cáo thu nhập theo NGÀY). Vui lòng chuyển sang tab 'Tuần' trên ứng dụng!")

    start, end, days = _period_range_from_extraction(extraction)
    if start and end:
        low, high = PERIOD_TYPE_WINDOWS[expected]
        if not (low <= days <= high):
            return _result(False, f"Khoảng thời gian {days} ngày không khớp loại kỳ {expected} ({low}-{high} ngày).")
        if target_month and target_month not in _months_spanned(start, end):
            return _result(
                False,
                f"Kỳ báo cáo {start.strftime('%m/%Y')} không khớp tháng mục tiêu {target_month[1]:02d}/{target_month[0]}.",
            )
        return _result(True, details={"days": days, "start": start.isoformat(), "end": end.isoformat()})

    if not text.strip():
        return _result(True, details={"missing_text": True})
    if _DATE_LIKE_RE.search(text):
        return _result(False, "Không đọc được khoảng ngày (từ ngày - đến ngày) trên màn hình báo cáo.")
    return _result(True, details={"unparsed": True})


def engine_period_granularity(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    text = str(extraction.get("period_raw_text") or extraction.get("period") or "")
    expected = _period_type_from_params(params)
    detected = _detected_period_type(extraction, text)
    lowered = text.strip().lower()

    if detected == TimeGranularity.MONTHLY or "tháng" in lowered:
        if expected != "MONTHLY":
            return _result(
                False,
                f"Phát hiện ảnh chụp báo cáo doanh thu theo THÁNG. Biểu mẫu yêu cầu báo cáo thu nhập theo {expected}!",
            )
    elif detected == TimeGranularity.DAILY:
        if expected != "DAILY":
            return _result(
                False,
                f"Phát hiện tab 'Ngày' (báo cáo thu nhập theo NGÀY). Biểu mẫu yêu cầu báo cáo thu nhập theo {expected}!",
            )
    elif detected is not None and detected != TimeGranularity.UNKNOWN:
        detected_value = detected.value
        if detected_value != expected:
            return _result(
                False,
                f"Phát hiện kỳ báo cáo {detected_value} nhưng biểu mẫu yêu cầu {expected}.",
            )

    start, end, days = _period_range_from_extraction(extraction)
    if start and end:
        low, high = PERIOD_TYPE_WINDOWS[expected]
        if not (low <= days <= high):
            return _result(
                False,
                f"Khoảng thời gian {days} ngày không khớp loại kỳ {expected} ({low}-{high} ngày).",
            )
        return _result(True, details={"days": days, "period_type": expected})

    if not text.strip():
        return _result(True, details={"missing_text": True, "period_type": expected})
    if _DATE_LIKE_RE.search(text):
        return _result(False, "Không đọc được khoảng ngày (từ ngày - đến ngày) trên màn hình báo cáo.")
    return _result(True, details={"unparsed": True, "period_type": expected})


_CONSTRAINT_FIELDS = {
    "period_type",
    "period_months",
    "period_years",
    "duration_days",
    "period_start",
    "period_end",
}
_CONSTRAINT_OPS = ("equals", "not_equals", "contains", "in", "gte", "lte", "between")


def _period_facts(extraction: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Derived period facts used by the constraints engine."""
    text = str(extraction.get("period_raw_text") or extraction.get("period") or "")
    start, end, days = _period_range_from_extraction(extraction)
    facts = dict(extraction)
    facts["duration_days"] = days if start and end else 0
    facts["period_start"] = start.isoformat() if start else ""
    facts["period_end"] = end.isoformat() if end else ""
    months = sorted(_months_spanned(start, end)) if start and end else []
    if not months:
        month_year = _month_year_from_text(text)
        if month_year:
            months = [month_year]
    facts["period_months"] = [month for _year, month in months]
    facts["period_years"] = sorted({year for year, _month in months})
    facts["period_type"] = str(
        extraction.get("period_type")
        or extraction.get("active_tab")
        or config.get("period_type")
        or ""
    ).upper()
    return facts


def split_list_values(value: Any) -> List[str]:
    """Split a comma list, honoring `"quoted, values"` and `\\"` / `\\\\` escapes.

    Plain `9,10` yields `['9', '10']`; `"a,b",c` yields `['a,b', 'c']`. A
    backslash only escapes a quote or another backslash, so paths survive.
    """
    text = str(value or "")
    parts: List[str] = []
    current: List[str] = []
    quoted = False
    escaped = False
    for char in text:
        if escaped:
            if char in ('"', "\\"):
                current.append(char)
            else:
                current.extend(("\\", char))
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if char == "," and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if escaped:
        current.append("\\")
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _split_values(value: Any) -> List[str]:
    return split_list_values(value)


def _constraint_actual(facts: Dict[str, Any], field: str) -> Any:
    if field in _CONSTRAINT_FIELDS:
        return facts.get(field)
    return _field_value(facts, field)


def _compare_scalar(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) or isinstance(expected, (int, float)):
        left, right = _to_number(actual), _to_number(expected)
        if left is not None and right is not None:
            return left == right
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) == bool(expected)
    return str(actual).strip().lower() == str(expected).strip().lower()


def _expected_parts(raw_value: Any) -> List[str]:
    """One expected value, or every comma-separated alternative ('9,10')."""
    parts = _split_values(raw_value)
    return parts or [str(raw_value)]


def _matches_any(actual: Any, raw_value: Any) -> bool:
    """True when the fact (scalar or list) matches any listed expected value."""
    parts = _expected_parts(raw_value)
    if isinstance(actual, (list, tuple, set)):
        return any(_compare_scalar(item, part) for item in actual for part in parts)
    return any(_compare_scalar(actual, part) for part in parts)


def _numeric_values(actual: Any) -> Optional[List[float]]:
    """Every numeric fact value, so list fields (months, years) compare per item."""
    items = actual if isinstance(actual, (list, tuple, set)) else [actual]
    values = [_to_number(item) for item in items]
    if not values or any(value is None for value in values):
        return None
    return values


def _range_bounds(constraint: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """`between` bounds from explicit min/max keys, or the legacy 'a,b' value."""
    low_raw = constraint.get("value_min", constraint.get("min_value"))
    high_raw = constraint.get("value_max", constraint.get("max_value"))
    if not _is_empty(low_raw) or not _is_empty(high_raw):
        return _to_number(low_raw), _to_number(high_raw)
    parts = _split_values(constraint.get("value"))
    if len(parts) != 2:
        return None, None
    return _to_number(parts[0]), _to_number(parts[1])


def _evaluate_constraint(constraint: Dict[str, Any], facts: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(constraint, dict):
        return False, "Ràng buộc không hợp lệ: mục ràng buộc phải là object."
    field = str(constraint.get("field") or "").strip()
    op = str(constraint.get("op") or "").strip()
    raw_value = constraint.get("value")
    if field not in _CONSTRAINT_FIELDS:
        return False, f"Ràng buộc không hợp lệ: trường '{field}' không được hỗ trợ."
    if op not in _CONSTRAINT_OPS:
        return False, f"Ràng buộc không hợp lệ: phép '{op}' không được hỗ trợ."
    low, high = _range_bounds(constraint) if op == "between" else (None, None)
    if op == "between" and (low is None or high is None):
        return False, f"Ràng buộc không hợp lệ: {field} between cần giá trị nhỏ nhất và lớn nhất."
    if op != "between" and _is_empty(raw_value):
        return False, f"Ràng buộc không hợp lệ: {field} {op} thiếu giá trị."

    actual = _constraint_actual(facts, field)
    label = f"{field} between {low:g}-{high:g}" if op == "between" else f"{field} {op} {raw_value}"

    if op == "equals":
        passed = _matches_any(actual, raw_value)
    elif op == "not_equals":
        passed = not _matches_any(actual, raw_value)
    elif op == "contains":
        if isinstance(actual, (list, tuple, set)):
            passed = _matches_any(actual, raw_value)
        else:
            text = str(actual or "").lower()
            passed = any(str(part).strip().lower() in text for part in _expected_parts(raw_value))
    elif op == "in":
        passed = _matches_any(actual, raw_value)
    elif op == "between":
        values = _numeric_values(actual)
        passed = bool(values) and any(low <= value <= high for value in values)
    else:
        values = _numeric_values(actual)
        right = _to_number(raw_value)
        if not values or right is None:
            passed = False
        else:
            passed = (
                any(value >= right for value in values)
                if op == "gte"
                else any(value <= right for value in values)
            )

    if passed:
        return True, ""
    custom_msg = str(constraint.get("reject_message") or "").strip()
    if custom_msg:
        return False, custom_msg
    return False, f"Ràng buộc không đạt: {label} (giá trị thực: {actual!r})."


def engine_constraints(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    constraints = params.get("constraints")
    if constraints in (None, "", []):
        return _result(True)
    if isinstance(constraints, str):
        try:
            constraints = json.loads(constraints)
        except json.JSONDecodeError:
            return _result(False, "Ràng buộc không hợp lệ: constraints phải là JSON array.")
    if not isinstance(constraints, list):
        return _result(False, "Ràng buộc không hợp lệ: constraints phải là array.")
    facts = _period_facts(extraction, context.get("config") or {})
    for constraint in constraints:
        passed, message = _evaluate_constraint(constraint, facts)
        if not passed:
            return _result(False, message)
    return _result(True, details={"constraints": len(constraints)})


def engine_number_range(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    field = str(params.get("field") or "")
    value = _to_number(_field_value(extraction, field))
    if value is None:
        return _result(True, details={"missing": field})
    minimum = _to_number(params.get("min"))
    maximum = _to_number(params.get("max"))
    if minimum is not None and value < minimum:
        return _result(False, str(params.get("message") or f"{field} nhỏ hơn mức tối thiểu {minimum:g}."))
    if maximum is not None and value > maximum:
        return _result(False, str(params.get("message") or f"{field} vượt quá mức tối đa {maximum:g}."))
    return _result(True, details={"value": value})


def engine_arithmetic(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    expression = str(params.get("expression") or "")
    if not expression:
        raise EngineDefinitionError("Arithmetic engine requires an 'expression' parameter")
    names = {
        node.id for node in ast.walk(ast.parse(expression, mode="eval"))
        if isinstance(node, ast.Name) and node.id not in _ALLOWED_FUNCS
    }
    values: Dict[str, float] = {}
    for name in names:
        raw = _field_value(extraction, name)
        number = _to_number(raw)
        if number is None:
            # Missing extracted data is not proof of tampering; a required-fields
            # engine owns that failure instead.
            return _result(True, details={"skipped_missing": name})
        values[name] = number
    outcome, error = safe_arithmetic(expression, values)
    if error:
        return _result(False, f"Không kiểm tra được số học: {error}")
    tolerance = abs(float(params.get("tolerance") or 0))
    direction = str(params.get("direction") or "abs")
    if direction == "lte":
        failed = outcome > tolerance
    elif direction == "gte":
        failed = outcome < -tolerance
    else:
        failed = abs(outcome) > tolerance
    if failed:
        message = str(params.get("message") or "Số học không khớp giữa các số liệu trích xuất.")
        return _result(False, f"{message} (sai lệch {abs(outcome):,.2f})")
    return _result(True, details={"value": outcome})


def engine_regex(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    field = str(params.get("field") or "")
    pattern = str(params.get("pattern") or "")
    value = _field_value(extraction, field)
    if _is_empty(value):
        return _result(True, details={"missing": field})
    if not re.search(pattern, str(value)):
        return _result(False, str(params.get("message") or f"Giá trị {field} không đúng định dạng yêu cầu."))
    return _result(True)


def engine_enum(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    field = str(params.get("field") or "")
    allowed = params.get("allowed") or params.get("options") or []
    if isinstance(allowed, str):
        allowed = split_list_values(allowed)
    allowed_norm = {str(item).strip().lower() for item in allowed}
    value = _field_value(extraction, field)
    if _is_empty(value):
        if params.get("required"):
            return _result(False, f"Thiếu giá trị bắt buộc cho {field}.")
        return _result(True, details={"missing": field})
    if str(value).strip().lower() not in allowed_norm:
        return _result(
            False,
            str(params.get("message") or f"Giá trị '{value}' không nằm trong danh sách cho phép của {field}."),
            {"value": value, "allowed": list(allowed)},
        )
    return _result(True)


def engine_boolean(params: Dict[str, Any], extraction: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    field = str(params.get("field") or "")
    expected = params.get("expected", True)
    value = _field_value(extraction, field)
    if value is None:
        if params.get("required"):
            return _result(False, str(params.get("message") or f"Thiếu trường {field}."))
        return _result(True, details={"missing": field})
    if isinstance(value, str):
        value = value.strip().lower() in ("true", "1", "yes", "có", "co")
    if bool(value) != bool(expected):
        return _result(False, str(params.get("message") or f"Trường {field} không đạt yêu cầu."))
    return _result(True)


ENGINE_IMPLEMENTATIONS = {
    "required_fields": engine_required_fields,
    "date_range": engine_date_range,
    "number_range": engine_number_range,
    "arithmetic": engine_arithmetic,
    "regex": engine_regex,
    "enum": engine_enum,
    "boolean": engine_boolean,
    "period_granularity": engine_period_granularity,
    "constraints": engine_constraints,
}


def run_engine(
    engine_def: Dict[str, Any],
    binding: Dict[str, Any],
    extraction: Dict[str, Any],
    config: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one object-bound engine; conditions gate execution."""
    engine_id = str(binding.get("id") or engine_def.get("id") or "")
    implementation = str(engine_def.get("implementation") or "")
    fn = ENGINE_IMPLEMENTATIONS.get(implementation)
    if fn is None:
        raise EngineDefinitionError(f"Engine '{engine_id}' uses unknown implementation '{implementation}'")

    params = resolve_placeholders(binding.get("params") or {}, config)
    conditions = resolve_placeholders(binding.get("conditions") or [], config)
    base = {
        "engine_id": engine_id,
        "implementation": implementation,
        "label": binding.get("label") or engine_def.get("label") or engine_id,
        "params": params,
    }
    if not conditions_pass(conditions, extraction, config):
        return {**base, "passed": True, "skipped": True, "reason": "conditions_not_met", "details": {}}

    outcome = fn(params, extraction, {**(context or {}), "engine": engine_def, "config": config})
    return {**base, **outcome, "skipped": False}
