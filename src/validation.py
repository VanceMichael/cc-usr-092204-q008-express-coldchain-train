"""极简 JSON Schema 校验器（仅覆盖本仓库契约用到的子集，无第三方依赖）。

支持：type(object/array/string/integer/number/boolean/null)、required、
properties、items、enum、minimum、minLength、additionalProperties=false。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TYPE_MAP = {
    "object": dict, "array": list, "string": str, "integer": int,
    "number": (int, float), "boolean": bool, "null": type(None),
}


def _check_type(instance: Any, types: list[str]) -> bool:
    for ty in types:
        if ty == "null" and instance is None:
            return True
        if ty == "boolean" and isinstance(instance, bool):
            return True
        if ty == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if ty == "number" and isinstance(instance, (int, float)) \
                and not isinstance(instance, bool):
            return True
        if ty in ("object", "array", "string") and isinstance(
                instance, _TYPE_MAP[ty]):
            return True
    return False


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    types = schema.get("type")
    if isinstance(types, str):
        types = [types]
    if types and not _check_type(instance, types):
        errors.append(f"{path}: 期望 {types}，实际 {type(instance).__name__}")
        return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: 值 {instance!r} 不在枚举 {schema['enum']}")
    if isinstance(instance, str) and "minLength" in schema \
            and len(instance) < schema["minLength"]:
        errors.append(f"{path}: 短于最小长度 {schema['minLength']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool) \
            and "minimum" in schema and instance < schema["minimum"]:
        errors.append(f"{path}: 小于最小值 {schema['minimum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必填字段 {key}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(instance) - set(props)
            if extra:
                errors.append(f"{path}: 出现未声明字段 {sorted(extra)}")
        for key, value in instance.items():
            if key in props:
                errors.extend(validate(value, props[key], f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors


def load_schema(name: str) -> dict[str, Any]:
    base = Path(__file__).resolve().parents[1] / "contracts"
    return json.loads((base / name).read_text(encoding="utf-8"))


def assert_valid(instance: Any, schema_name: str) -> None:
    errs = validate(instance, load_schema(schema_name))
    if errs:
        raise AssertionError("契约校验失败:\n" + "\n".join(errs))
