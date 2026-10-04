"""Tool argument validation and output safety checks.

Untrusted model output becomes arguments here. Everything is validated *before* any tool runs:
types, ranges, lengths, enumerations, path shape, and command shape. A tool that receives
validated arguments can rely on basic invariants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..core import ValidationError

_MAX_STRING = 100_000
_MAX_LIST = 1_000
_MAX_DEPTH = 12


@dataclass
class Schema:
    """Small, explicit JSON-schema subset - enough for tool contracts, no dependency."""

    type: str = "object"
    properties: dict[str, "Schema"] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    items: "Schema | None" = None
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    pattern: str | None = None
    description: str = ""
    default: Any = None
    additional: bool = False

    # -- constructors ---------------------------------------------------------- #
    @classmethod
    def string(cls, *, max_length: int | None = None, pattern: str | None = None, enum: list[str] | None = None, description: str = "", default: Any = None) -> "Schema":
        return cls("string", enum=enum, max_length=max_length, pattern=pattern, description=description, default=default)

    @classmethod
    def integer(cls, *, minimum: float | None = None, maximum: float | None = None, description: str = "", default: Any = None) -> "Schema":
        return cls("integer", minimum=minimum, maximum=maximum, description=description, default=default)

    @classmethod
    def number(cls, *, minimum: float | None = None, maximum: float | None = None, description: str = "", default: Any = None) -> "Schema":
        return cls("number", minimum=minimum, maximum=maximum, description=description, default=default)

    @classmethod
    def boolean(cls, *, description: str = "", default: Any = None) -> "Schema":
        return cls("boolean", description=description, default=default)

    @classmethod
    def array(cls, items: "Schema", *, max_length: int | None = None, description: str = "",
              default: Any = None) -> "Schema":
        return cls("array", items=items, max_length=max_length, description=description, default=default)

    @classmethod
    def object(cls, properties: dict[str, "Schema"], *, required: list[str] | None = None, additional: bool = False, description: str = "") -> "Schema":
        return cls("object", properties=properties, required=required or [], additional=additional, description=description)

    @classmethod
    def any(cls, *, description: str = "", default: Any = None) -> "Schema":
        """Accepts any JSON value (used for free-form payloads such as headers or bodies)."""
        return cls("any", description=description, default=default)

    @classmethod
    def enum_of(cls, values: list[Any], *, description: str = "", default: Any = None) -> "Schema":
        kind = "integer" if all(isinstance(v, int) and not isinstance(v, bool) for v in values) else "string"
        return cls(kind, enum=list(values), description=description, default=default)

    # -- serialisation --------------------------------------------------------- #
    @classmethod
    def from_dict(cls, data: Any) -> "Schema":
        """Build a Schema from the JSON-schema subset produced by :meth:`to_json_schema`.

        Verification plans and tool contracts arrive as data (often written by a model), so the
        relaxed subset is accepted here and validated properly - never trusted as-is.
        """
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ValidationError("schema must be an object")
        unknown = set(data) - {"type", "properties", "required", "items", "enum", "minimum", "maximum",
                               "maxLength", "max_length", "pattern", "description", "default",
                               "additionalProperties", "additional"}
        if unknown:
            raise ValidationError(f"unknown schema keys: {', '.join(sorted(str(key) for key in unknown))}")
        properties = {str(name): cls.from_dict(value)
                      for name, value in (data.get("properties") or {}).items()}
        items = data.get("items")
        required = data.get("required")
        if isinstance(required, bool):            # tolerate {"required": true} inside a property
            required = None
        return cls(
            type=str(data.get("type", "object")),
            properties=properties,
            required=[str(name) for name in (required or [])],
            items=cls.from_dict(items) if items else None,
            enum=list(data["enum"]) if data.get("enum") else None,
            minimum=data.get("minimum"), maximum=data.get("maximum"),
            max_length=data.get("maxLength", data.get("max_length")), pattern=data.get("pattern"),
            description=str(data.get("description", "")), default=data.get("default"),
            additional=bool(data.get("additionalProperties", data.get("additional", False))),
        )

    def to_json_schema(self) -> dict[str, Any]:
        """Export for model tool-calling APIs."""
        out: dict[str, Any] = {"type": self.type}
        if self.description:
            out["description"] = self.description
        if self.enum:
            out["enum"] = self.enum
        if self.properties:
            out["properties"] = {name: schema.to_json_schema() for name, schema in self.properties.items()}
            out["additionalProperties"] = self.additional
        if self.required:
            out["required"] = self.required
        if self.items:
            out["items"] = self.items.to_json_schema()
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.max_length is not None:
            out["maxLength"] = self.max_length
        if self.pattern:
            out["pattern"] = self.pattern
        if self.default is not None:
            out["default"] = self.default
        return out


@dataclass
class ValidationIssue:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def validate_arguments(schema: Schema, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate *arguments* against *schema*.

    Returns a new dict containing only validated values (defaults applied).
    Raises :class:`ValidationError` listing every issue at once.
    """
    issues: list[ValidationIssue] = []
    clean = _validate(schema, dict(arguments or {}), "$", issues, 0)
    if issues:
        raise ValidationError(
            "; ".join(str(issue) for issue in issues),
            issues=[{"path": issue.path, "message": issue.message} for issue in issues],
        )
    return clean if isinstance(clean, dict) else {}


def _validate(schema: Schema, value: Any, path: str, issues: list[ValidationIssue], depth: int) -> Any:
    if depth > _MAX_DEPTH:
        issues.append(ValidationIssue(path, "nesting too deep"))
        return None

    kind = schema.type
    if kind == "any":
        return value
    if kind == "object":
        if not isinstance(value, Mapping):
            issues.append(ValidationIssue(path, f"expected object, got {type(value).__name__}"))
            return {}
        out: dict[str, Any] = {}
        for name in schema.required:
            if name not in value:
                if schema.properties.get(name) and schema.properties[name].default is not None:
                    out[name] = schema.properties[name].default
                else:
                    issues.append(ValidationIssue(f"{path}.{name}", "required field missing"))
        for name, item in value.items():
            child = schema.properties.get(name)
            if child is None:
                if schema.additional:
                    out[name] = item
                else:
                    issues.append(ValidationIssue(f"{path}.{name}", "unexpected field"))
                continue
            out[name] = _validate(child, item, f"{path}.{name}", issues, depth + 1)
        for name, child in schema.properties.items():
            if name not in out and child.default is not None and name not in schema.required:
                out[name] = child.default
        return out

    if kind == "array":
        if not isinstance(value, (list, tuple)):
            issues.append(ValidationIssue(path, f"expected array, got {type(value).__name__}"))
            return []
        if schema.max_length and len(value) > schema.max_length:
            issues.append(ValidationIssue(path, f"array exceeds max length {schema.max_length}"))
            return list(value[: schema.max_length])
        if len(value) > _MAX_LIST:
            issues.append(ValidationIssue(path, f"array exceeds hard limit {_MAX_LIST}"))
            return list(value[:_MAX_LIST])
        return [_validate(schema.items, item, f"{path}[{i}]", issues, depth + 1) for i, item in enumerate(value)]

    # scalars ---------------------------------------------------------------- #
    if kind == "string":
        if not isinstance(value, str):
            if value is None and schema.default is not None:
                return schema.default
            issues.append(ValidationIssue(path, f"expected string, got {type(value).__name__}"))
            return ""
        if "\x00" in value:
            issues.append(ValidationIssue(path, "NUL byte not allowed"))
            value = value.replace("\x00", "")
        limit = min(schema.max_length or _MAX_STRING, _MAX_STRING)
        if len(value) > limit:
            issues.append(ValidationIssue(path, f"string exceeds max length {limit}"))
            value = value[:limit]
        if schema.enum and value not in schema.enum:
            issues.append(ValidationIssue(path, f"value must be one of {schema.enum}"))
        if schema.pattern and not re.fullmatch(schema.pattern, value):
            issues.append(ValidationIssue(path, f"value does not match pattern {schema.pattern!r}"))
        return value

    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
                value = int(value.strip())
            else:
                issues.append(ValidationIssue(path, f"expected integer, got {type(value).__name__}"))
                return 0
        if schema.minimum is not None and value < schema.minimum:
            issues.append(ValidationIssue(path, f"value {value} below minimum {schema.minimum}"))
        if schema.maximum is not None and value > schema.maximum:
            issues.append(ValidationIssue(path, f"value {value} above maximum {schema.maximum}"))
        if schema.enum and value not in schema.enum:
            issues.append(ValidationIssue(path, f"value must be one of {schema.enum}"))
        return value

    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            try:
                value = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                issues.append(ValidationIssue(path, f"expected number, got {type(value).__name__}"))
                return 0.0
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            issues.append(ValidationIssue(path, "non-finite number"))
            return 0.0
        if schema.minimum is not None and value < schema.minimum:
            issues.append(ValidationIssue(path, f"value {value} below minimum {schema.minimum}"))
        if schema.maximum is not None and value > schema.maximum:
            issues.append(ValidationIssue(path, f"value {value} above maximum {schema.maximum}"))
        return value

    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        issues.append(ValidationIssue(path, f"expected boolean, got {type(value).__name__}"))
        return False

    if kind == "null":
        return None

    issues.append(ValidationIssue(path, f"unsupported schema type {kind!r}"))
    return value


def validate_tool_output(payload: Any, *, max_bytes: int = 262_144) -> Any:
    """Bound and sanitise a tool result before it re-enters the model context."""
    import json

    text = json.dumps(payload, default=str, ensure_ascii=False) if not isinstance(payload, str) else payload
    if len(text.encode("utf-8")) > max_bytes:
        raise ValidationError(f"tool output exceeds {max_bytes} bytes")
    return payload
