"""Structured response validators, JSON repair logic, and interactive CLI validator (Phase 14: F14.1-F14.3)."""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import sys
from typing import Any, Callable, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError, field_validator


# ---------------------------------------------------------------------------
# F14.1: Pydantic Tool Call Parameter Validators & JSON Schemas
# ---------------------------------------------------------------------------


class BaseToolParams(BaseModel):
    """Base schema for Nova tool call arguments."""

    model_config = ConfigDict(extra="forbid")

    urgency: Optional[Literal["low", "normal", "high"]] = Field(
        default=None,
        description="Optional urgency tag injected by the router for high-priority commands.",
    )


def _ensure_non_empty_str(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"'{field_name}' must be a non-empty string.")
    return stripped


class OpenAppParams(BaseToolParams):
    app_name: StrictStr = Field(..., description="The name or executable path of the application to launch.")

    @field_validator("app_name")
    @classmethod
    def validate_app_name(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "app_name")


class EmptyToolParams(BaseToolParams):
    """Schema for read-only info actions that require no parameters."""


class SearchFilesParams(BaseToolParams):
    query: StrictStr = Field(..., description="The file name query string or pattern.")
    search_path: Optional[StrictStr] = Field(default=None, description="Optional root directory to search in.")
    use_regex: Optional[StrictBool] = Field(default=None, description="Whether to interpret query as regex.")
    exclude_dirs: Optional[list[StrictStr]] = Field(
        default=None, description="Optional list of directory names to exclude."
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "query")

    @field_validator("search_path")
    @classmethod
    def validate_search_path(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _ensure_non_empty_str(v, "search_path")


class ReadFileParams(BaseToolParams):
    path: StrictStr = Field(..., description="Absolute or workspace-relative path to the file.")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "path")


class WriteFileParams(BaseToolParams):
    path: StrictStr = Field(..., description="Target file path.")
    content: StrictStr = Field(..., description="Text content to write to the file.")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "path")


class DeleteFileParams(BaseToolParams):
    path: StrictStr = Field(..., description="Path to the file to delete.")

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "path")


class RunScriptParams(BaseToolParams):
    command: StrictStr = Field(..., description="Shell command string to execute.")

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "command")


class InstallPackageParams(BaseToolParams):
    package: StrictStr = Field(..., description="Package name to install.")
    manager: Literal["pip", "npm"] = Field(default="pip", description="Package manager to use ('pip' or 'npm').")

    @field_validator("package")
    @classmethod
    def validate_package(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "package")


class TuyaControlDeviceParams(BaseToolParams):
    device_id: StrictStr = Field(..., description="The target Tuya smart home device ID.")
    command_name: StrictStr = Field(..., description="Tuya command name (e.g., 'switch_led').")
    value: StrictBool = Field(..., description="Boolean command value (true to turn on, false to turn off).")

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "device_id")

    @field_validator("command_name")
    @classmethod
    def validate_command_name(cls, v: str) -> str:
        return _ensure_non_empty_str(v, "command_name")


ACTION_PARAM_SCHEMAS: dict[str, type[BaseToolParams]] = {
    "open_app": OpenAppParams,
    "get_battery": EmptyToolParams,
    "list_running_apps": EmptyToolParams,
    "get_system_info": EmptyToolParams,
    "search_files": SearchFilesParams,
    "read_file": ReadFileParams,
    "write_file": WriteFileParams,
    "delete_file": DeleteFileParams,
    "run_script": RunScriptParams,
    "install_package": InstallPackageParams,
    "tuya_control_device": TuyaControlDeviceParams,
}

ACTION_CATEGORIES: dict[str, str] = {
    "open_app": "app_control",
    "get_battery": "read_only_info",
    "list_running_apps": "read_only_info",
    "get_system_info": "read_only_info",
    "search_files": "file_system",
    "read_file": "file_system",
    "write_file": "file_system",
    "delete_file": "file_system",
    "run_script": "shell_command",
    "install_package": "shell_command",
    "tuya_control_device": "smart_home",
}


def get_action_json_schema(action_type: str) -> dict[str, Any]:
    """Returns the JSON Schema definition for a registered Nova action."""
    if action_type not in ACTION_PARAM_SCHEMAS:
        raise KeyError(f"Unknown action_type '{action_type}'.")
    return ACTION_PARAM_SCHEMAS[action_type].model_json_schema()


def list_action_schemas() -> dict[str, dict[str, Any]]:
    """Returns JSON Schemas for all registered Nova tool actions."""
    return {
        action_type: {
            "category": ACTION_CATEGORIES.get(action_type, "unknown"),
            "parameters": model_cls.model_json_schema(),
        }
        for action_type, model_cls in ACTION_PARAM_SCHEMAS.items()
    }


# ---------------------------------------------------------------------------
# F14.2: Structured Output JSON Parsing & Error Repair Logic
# ---------------------------------------------------------------------------


class StructuredOutputParseError(ValueError):
    """Raised when structured LLM output cannot be repaired into a valid JSON object."""


class ToolValidationError(ValueError):
    """Raised when parsed tool arguments fail schema validation."""

    def __init__(self, message: str, report: CommandValidationReport | None = None):
        super().__init__(message)
        self.report = report


class JSONRepairResult(BaseModel):
    parsed: Optional[dict[str, Any]] = None
    repaired: bool = False
    repairs_applied: list[str] = Field(default_factory=list)
    error: Optional[str] = None


_JSON_FENCE_RE = re.compile(r"```(?:json|JSON)\b[^\n]*\s*\n?(.*?)\n?\s*```", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_UNQUOTED_KEY_RE = re.compile(r'([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)')


def _strip_trailing_commas(text: str) -> Tuple[str, bool]:
    """Removes trailing commas before '}' or ']' outside string literals."""
    out: list[str] = []
    in_string = False
    quote_char = ""
    escaped = False
    changed = False
    length = len(text)
    i = 0

    while i < length:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                in_string = False
                quote_char = ""
            i += 1
            continue

        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
            out.append(ch)
            i += 1
            continue

        if ch == ",":
            # Look ahead skipping whitespace
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            if j < length and text[j] in ("}", "]"):
                changed = True
                i += 1
                continue

        out.append(ch)
        i += 1

    return "".join(out), changed


def _extract_json_object_wrapper(text: str) -> Tuple[str, bool]:
    """Extracts the primary JSON object from surrounding prose/partial wrappers."""
    start = text.find("{")
    if start == -1:
        return text, False

    in_string = False
    quote_char = ""
    escaped = False
    depth = 0
    end = -1

    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                in_string = False
                quote_char = ""
            continue

        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break

    if end != -1:
        extracted = text[start : end + 1]
        if extracted != text:
            return extracted, True
        return text, False

    # Partial wrapper (missing closing brace(s)): take from first '{' onwards
    if start > 0:
        return text[start:], True
    return text, False


def _balance_braces(text: str) -> Tuple[str, bool]:
    """Appends missing closing '}' or ']' if an LLM output was truncated."""
    stack: list[str] = []
    in_string = False
    quote_char = ""
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                in_string = False
                quote_char = ""
            continue

        if ch in ('"', "'"):
            in_string = True
            quote_char = ch
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()

    if stack and not in_string:
        return text + "".join(reversed(stack)), True
    return text, False


def _quote_unquoted_keys(text: str) -> Tuple[str, bool]:
    """Quotes bare identifier keys outside string literals (e.g., {app_name: 'x'})."""
    # Split into string segments and non-string segments so string contents stay untouched
    segments: list[str] = []
    in_string = False
    quote_char = ""
    escaped = False
    buf: list[str] = []
    changed = False

    def flush_non_string() -> None:
        nonlocal changed
        if not buf:
            return
        chunk = "".join(buf)
        buf.clear()
        replaced = _UNQUOTED_KEY_RE.sub(r'\1"\2"\3', chunk)
        if replaced != chunk:
            changed = True
        segments.append(replaced)

    for ch in text:
        if in_string:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                in_string = False
                quote_char = ""
                segments.append("".join(buf))
                buf.clear()
            continue

        if ch in ('"', "'"):
            flush_non_string()
            in_string = True
            quote_char = ch
            buf.append(ch)
        else:
            buf.append(ch)

    if in_string:
        segments.append("".join(buf))
    else:
        flush_non_string()

    return "".join(segments), changed


def _replace_json_literals_for_python(text: str) -> str:
    """Replaces bare JSON literals true/false/null with Python True/False/None outside strings."""
    out: list[str] = []
    in_string = False
    quote_char = ""
    escaped = False
    buf: list[str] = []

    def flush_non_string() -> None:
        if not buf:
            return
        chunk = "".join(buf)
        buf.clear()
        chunk = re.sub(r"\btrue\b", "True", chunk)
        chunk = re.sub(r"\bfalse\b", "False", chunk)
        chunk = re.sub(r"\bnull\b", "None", chunk)
        out.append(chunk)

    for ch in text:
        if in_string:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote_char:
                in_string = False
                quote_char = ""
                out.append("".join(buf))
                buf.clear()
            continue

        if ch in ('"', "'"):
            flush_non_string()
            in_string = True
            quote_char = ch
            buf.append(ch)
        else:
            buf.append(ch)

    if in_string:
        out.append("".join(buf))
    else:
        flush_non_string()

    return "".join(out)


def _try_parse_dict(candidate: str) -> Tuple[Optional[dict[str, Any]], Optional[str]]:
    """Attempts standard json.loads and returns (dict, unwrap_note)."""
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None, None

    if isinstance(parsed, dict):
        return parsed, None
    if isinstance(parsed, str):
        # Handle double-encoded JSON strings such as '"{\"app_name\": \"vscode\"}"'
        try:
            nested = json.loads(parsed)
            if isinstance(nested, dict):
                return nested, "unwrapped_double_encoded_json"
        except json.JSONDecodeError:
            pass
    return None, None


def repair_and_parse_json(raw_output: str | dict[str, Any]) -> JSONRepairResult:
    """
    Parses and repairs malformed structured LLM JSON output before falling back to error.

    Handles:
    - Markdown code fences (```json ... ```)
    - Leading/trailing prose and partial wrappers
    - Unclosed trailing braces/brackets
    - Trailing commas before '}' or ']'
    - Single-quoted dicts and Python literals (True/False/None)
    - Unquoted object keys
    - Double-encoded JSON strings
    """
    if isinstance(raw_output, dict):
        return JSONRepairResult(parsed=raw_output, repaired=False, repairs_applied=[])

    if not isinstance(raw_output, str):
        return JSONRepairResult(
            parsed=None,
            repaired=False,
            error=f"Expected a JSON string or dict, got {type(raw_output).__name__}.",
        )

    stripped = raw_output.strip()
    if not stripped:
        return JSONRepairResult(parsed=None, repaired=False, error="Empty structured output.")

    # Fast path: already valid JSON
    try:
        direct = json.loads(stripped)
        if isinstance(direct, dict):
            return JSONRepairResult(parsed=direct, repaired=False, repairs_applied=[])
        if isinstance(direct, str):
            nested, note = _try_parse_dict(stripped)
            if nested is not None:
                return JSONRepairResult(parsed=nested, repaired=True, repairs_applied=[note or "unwrapped_json"])
        return JSONRepairResult(
            parsed=None,
            repaired=False,
            error=f"Expected a JSON object for tool arguments, got {type(direct).__name__}.",
        )
    except json.JSONDecodeError:
        pass

    candidate = stripped
    repairs: list[str] = []

    # 1. Strip markdown code fences (```json ... ``` or nested ```markdown ... ```)
    json_fence_match = _JSON_FENCE_RE.search(candidate)
    if json_fence_match:
        fenced_content = json_fence_match.group(1).strip()
        repairs.append("stripped_markdown_fences")
        parsed, note = _try_parse_dict(fenced_content)
        if parsed is not None:
            if note:
                repairs.append(note)
            return JSONRepairResult(parsed=parsed, repaired=True, repairs_applied=repairs)
        candidate = fenced_content
    else:
        stripped_any_fence = False
        while True:
            outer_fence = re.search(r"^\s*```(?:[a-zA-Z0-9_-]+)?\s*\n?(.*?)\n?\s*```\s*$", candidate, re.DOTALL)
            if outer_fence:
                candidate = outer_fence.group(1).strip()
                stripped_any_fence = True
            else:
                break

        if not stripped_any_fence:
            fence_match = _FENCE_RE.search(candidate)
            if fence_match and "{" in fence_match.group(1):
                candidate = fence_match.group(1).strip()
                stripped_any_fence = True

        if stripped_any_fence:
            repairs.append("stripped_markdown_fences")
            parsed, note = _try_parse_dict(candidate)
            if parsed is not None:
                if note:
                    repairs.append(note)
                return JSONRepairResult(parsed=parsed, repaired=True, repairs_applied=repairs)

    # 2. Extract JSON object from surrounding prose / partial wrapper
    extracted, extracted_changed = _extract_json_object_wrapper(candidate)
    if extracted_changed:
        candidate = extracted.strip()
        repairs.append("extracted_from_wrapper")
        parsed, note = _try_parse_dict(candidate)
        if parsed is not None:
            if note:
                repairs.append(note)
            return JSONRepairResult(parsed=parsed, repaired=True, repairs_applied=repairs)

    # 3. Remove trailing commas
    no_trailing, comma_changed = _strip_trailing_commas(candidate)
    if comma_changed:
        candidate = no_trailing
        repairs.append("removed_trailing_commas")
        parsed, note = _try_parse_dict(candidate)
        if parsed is not None:
            if note:
                repairs.append(note)
            return JSONRepairResult(parsed=parsed, repaired=True, repairs_applied=repairs)

    # 4. Balance truncated braces/brackets
    balanced, balance_changed = _balance_braces(candidate)
    if balance_changed:
        candidate = balanced
        repairs.append("balanced_closing_braces")
        # Also re-check trailing commas right before newly added braces
        no_trailing_after, comma_after = _strip_trailing_commas(candidate)
        if comma_after:
            candidate = no_trailing_after
            if "removed_trailing_commas" not in repairs:
                repairs.append("removed_trailing_commas")
        parsed, note = _try_parse_dict(candidate)
        if parsed is not None:
            if note:
                repairs.append(note)
            return JSONRepairResult(parsed=parsed, repaired=True, repairs_applied=repairs)

    # 5. Quote unquoted object keys
    quoted_keys, keys_changed = _quote_unquoted_keys(candidate)
    if keys_changed:
        candidate = quoted_keys
        repairs.append("quoted_unquoted_keys")
        parsed, note = _try_parse_dict(candidate)
        if parsed is not None:
            if note:
                repairs.append(note)
            return JSONRepairResult(parsed=parsed, repaired=True, repairs_applied=repairs)

    # 6. Safe Python literal parsing for single-quoted dicts & True/False/None
    py_candidate = _replace_json_literals_for_python(candidate)
    try:
        literal_val = ast.literal_eval(py_candidate)
        if isinstance(literal_val, dict):
            # Ensure JSON serializability
            json.dumps(literal_val)
            repairs.append("repaired_single_quotes_or_literals")
            return JSONRepairResult(parsed=literal_val, repaired=True, repairs_applied=repairs)
        return JSONRepairResult(
            parsed=None,
            repaired=False,
            repairs_applied=repairs,
            error=f"Expected a JSON object for tool arguments, got {type(literal_val).__name__}.",
        )
    except (SyntaxError, ValueError, TypeError):
        pass

    return JSONRepairResult(
        parsed=None,
        repaired=False,
        repairs_applied=repairs,
        error="Unable to parse or repair structured JSON output.",
    )


# ---------------------------------------------------------------------------
# F14.3: Structured Validation Report, Actionable Suggestions & CLI Validator
# ---------------------------------------------------------------------------


class ValidationIssue(BaseModel):
    field: str
    code: str
    message: str
    suggestion: str


class CommandValidationReport(BaseModel):
    valid: bool
    action_type: Optional[str] = None
    category: Optional[str] = None
    normalized_params: Optional[dict[str, Any]] = None
    repaired: bool = False
    repairs_applied: list[str] = Field(default_factory=list)
    errors: list[ValidationIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    schema_definition: Optional[dict[str, Any]] = None


def _build_field_suggestion(
    action_type: str,
    model_cls: type[BaseToolParams],
    err: dict[str, Any],
) -> ValidationIssue:
    loc_parts = [str(p) for p in err.get("loc", ())]
    field_name = ".".join(loc_parts) if loc_parts else "params"
    err_type = err.get("type", "validation_error")
    err_msg = err.get("msg", "Invalid value.")
    schema = model_cls.model_json_schema()
    props = schema.get("properties", {})
    allowed_fields = [k for k in props.keys() if k != "urgency"]

    if err_type == "missing":
        prop_info = props.get(field_name, {})
        expected_type = prop_info.get("type", "string")
        desc = prop_info.get("description", "")
        suggestion = f"Provide required field '{field_name}' (type: {expected_type})" + (
            f" — {desc}" if desc else "."
        )
        code = "missing_required_field"
    elif err_type == "extra_forbidden":
        close = difflib.get_close_matches(field_name, allowed_fields, n=1, cutoff=0.4)
        if close:
            suggestion = f"Remove unknown field '{field_name}'. Did you mean '{close[0]}'?"
        elif allowed_fields:
            suggestion = f"Remove unknown field '{field_name}'. Allowed fields for '{action_type}': {', '.join(allowed_fields)}."
        else:
            suggestion = f"Action '{action_type}' does not take parameters. Pass an empty object {{}}."
        code = "unexpected_field"
    elif "bool" in err_type:
        suggestion = f"Set '{field_name}' to a JSON boolean: true or false."
        code = "invalid_type"
    elif "string" in err_type:
        suggestion = f"Set '{field_name}' to a non-empty string."
        code = "invalid_type"
    elif "literal" in err_type:
        suggestion = f"Use one of the allowed literal values for '{field_name}'. ({err_msg})"
        code = "invalid_value"
    else:
        suggestion = f"Fix '{field_name}': {err_msg}"
        code = "invalid_value"

    return ValidationIssue(
        field=f"params.{field_name}" if field_name != "params" else "params",
        code=code,
        message=err_msg,
        suggestion=suggestion,
    )


def validate_tool_arguments(
    action_type: str,
    raw_arguments: str | dict[str, Any] | None = None,
) -> CommandValidationReport:
    """
    Validates tool call arguments for a specific Nova action_type.
    Automatically attempts JSON repair when raw_arguments is a string.
    """
    if not action_type or not isinstance(action_type, str) or not action_type.strip():
        issue = ValidationIssue(
            field="action_type",
            code="missing_action_type",
            message="Missing or invalid 'action_type'.",
            suggestion=f"Specify one of the supported actions: {', '.join(sorted(ACTION_PARAM_SCHEMAS.keys()))}.",
        )
        return CommandValidationReport(
            valid=False,
            errors=[issue],
            suggestions=[issue.suggestion],
        )

    action_key = action_type.strip()
    if action_key not in ACTION_PARAM_SCHEMAS:
        valid_actions = sorted(ACTION_PARAM_SCHEMAS.keys())
        close = difflib.get_close_matches(action_key, valid_actions, n=1, cutoff=0.5)
        if close:
            sugg = f"Unknown action '{action_key}'. Did you mean '{close[0]}'? Supported actions: {', '.join(valid_actions)}."
        else:
            sugg = f"Unknown action '{action_key}'. Supported actions: {', '.join(valid_actions)}."
        issue = ValidationIssue(
            field="action_type",
            code="unknown_action",
            message=f"Unsupported Nova action_type '{action_key}'.",
            suggestion=sugg,
        )
        return CommandValidationReport(
            valid=False,
            action_type=action_key,
            errors=[issue],
            suggestions=[sugg],
        )

    model_cls = ACTION_PARAM_SCHEMAS[action_key]
    category = ACTION_CATEGORIES.get(action_key, "unknown")
    schema_def = model_cls.model_json_schema()

    if raw_arguments is None:
        parsed_params: dict[str, Any] = {}
        repaired = False
        repairs_applied: list[str] = []
    elif isinstance(raw_arguments, dict):
        parsed_params = raw_arguments
        repaired = False
        repairs_applied = []
    elif isinstance(raw_arguments, str):
        repair_res = repair_and_parse_json(raw_arguments)
        if repair_res.parsed is None:
            required = schema_def.get("required", [])
            example_obj = {k: f"<{k}>" for k in required}
            sugg = (
                f"Provide a valid JSON object for '{action_key}' arguments, e.g. {json.dumps(example_obj)}."
            )
            issue = ValidationIssue(
                field="params",
                code="json_parse_error",
                message=repair_res.error or "Invalid JSON arguments.",
                suggestion=sugg,
            )
            return CommandValidationReport(
                valid=False,
                action_type=action_key,
                category=category,
                repaired=False,
                repairs_applied=repair_res.repairs_applied,
                errors=[issue],
                suggestions=[sugg],
                schema_definition=schema_def,
            )
        parsed_params = repair_res.parsed
        repaired = repair_res.repaired
        repairs_applied = repair_res.repairs_applied
    else:
        sugg = "Provide 'params' as a JSON object (dict) or JSON-encoded string."
        issue = ValidationIssue(
            field="params",
            code="invalid_type",
            message=f"Expected dict or JSON string for params, got {type(raw_arguments).__name__}.",
            suggestion=sugg,
        )
        return CommandValidationReport(
            valid=False,
            action_type=action_key,
            category=category,
            errors=[issue],
            suggestions=[sugg],
            schema_definition=schema_def,
        )

    try:
        validated = model_cls.model_validate(parsed_params)
        normalized = validated.model_dump(exclude_none=True)
        return CommandValidationReport(
            valid=True,
            action_type=action_key,
            category=category,
            normalized_params=normalized,
            repaired=repaired,
            repairs_applied=repairs_applied,
            errors=[],
            suggestions=[],
            schema_definition=schema_def,
        )
    except ValidationError as exc:
        issues = [_build_field_suggestion(action_key, model_cls, err) for err in exc.errors()]
        suggestions = [issue.suggestion for issue in issues]
        return CommandValidationReport(
            valid=False,
            action_type=action_key,
            category=category,
            repaired=repaired,
            repairs_applied=repairs_applied,
            errors=issues,
            suggestions=suggestions,
            schema_definition=schema_def,
        )


def parse_and_validate_tool_call(
    action_type: str,
    raw_arguments: str | dict[str, Any] | None,
) -> Tuple[dict[str, Any], bool, list[str]]:
    """
    Convenience helper for LLMClient: repairs JSON if needed, validates against the
    action schema, and returns (normalized_params, repaired, repairs_applied).
    Raises ToolValidationError on failure.
    """
    report = validate_tool_arguments(action_type, raw_arguments)
    if not report.valid or report.normalized_params is None:
        detail = "; ".join(f"{e.field}: {e.message}" for e in report.errors) or "Validation failed"
        raise ToolValidationError(f"Tool validation failed for '{action_type}': {detail}", report=report)
    return report.normalized_params, report.repaired, report.repairs_applied


def validate_command_payload(
    payload: str | dict[str, Any],
    *,
    action_type: Optional[str] = None,
) -> CommandValidationReport:
    """
    Interactive helper callable that validates a complete command payload (dict or raw JSON string)
    and returns structured error messages plus actionable repair suggestions.

    Accepted forms:
    - validate_command_payload({"action_type": "open_app", "params": {"app_name": "vscode"}})
    - validate_command_payload({"tool_name": "open_app", "arguments": '{"app_name": "vscode"}'})
    - validate_command_payload('```json\\n{"action_type": "open_app", "params": {"app_name": "vscode",}}\\n```')
    - validate_command_payload('{"app_name": "vscode"}', action_type="open_app")
    """
    if action_type is not None:
        return validate_tool_arguments(action_type, payload)

    repairs_applied: list[str] = []
    repaired = False

    if isinstance(payload, str):
        repair_res = repair_and_parse_json(payload)
        if repair_res.parsed is None:
            sugg = (
                'Pass a JSON object with "action_type" and "params", e.g. '
                '{"action_type": "open_app", "params": {"app_name": "vscode"}}.'
            )
            issue = ValidationIssue(
                field="payload",
                code="json_parse_error",
                message=repair_res.error or "Failed to parse command payload JSON.",
                suggestion=sugg,
            )
            return CommandValidationReport(
                valid=False,
                repaired=False,
                repairs_applied=repair_res.repairs_applied,
                errors=[issue],
                suggestions=[sugg],
            )
        data = repair_res.parsed
        repaired = repair_res.repaired
        repairs_applied = list(repair_res.repairs_applied)
    elif isinstance(payload, dict):
        data = payload
    else:
        sugg = 'Provide a dict or JSON string with "action_type" and "params".'
        issue = ValidationIssue(
            field="payload",
            code="invalid_type",
            message=f"Unsupported payload type '{type(payload).__name__}'.",
            suggestion=sugg,
        )
        return CommandValidationReport(valid=False, errors=[issue], suggestions=[sugg])

    resolved_action = data.get("action_type") or data.get("tool_name") or data.get("name")
    if "params" in data:
        raw_params = data["params"]
    elif "arguments" in data:
        raw_params = data["arguments"]
    elif "input" in data:
        raw_params = data["input"]
    else:
        raw_params = {}

    report = validate_tool_arguments(resolved_action, raw_params)
    if repaired:
        report.repaired = True
        merged_repairs = repairs_applied + [r for r in report.repairs_applied if r not in repairs_applied]
        report.repairs_applied = merged_repairs
    return report


def format_validation_report(report: CommandValidationReport) -> str:
    """Formats a CommandValidationReport as human-readable CLI output."""
    lines: list[str] = []
    status_label = "VALID" if report.valid else "INVALID"
    lines.append(f"[{status_label}] action={report.action_type or '<unknown>'} category={report.category or 'unknown'}")
    if report.repaired:
        lines.append(f"  Repairs applied: {', '.join(report.repairs_applied)}")
    if report.valid:
        lines.append(f"  Normalized params: {json.dumps(report.normalized_params or {})}")
    else:
        lines.append("  Errors:")
        for err in report.errors:
            lines.append(f"    - {err.field} ({err.code}): {err.message}")
            lines.append(f"      Suggestion: {err.suggestion}")
    return "\n".join(lines)


def run_interactive_validator(
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], Any] = print,
) -> int:
    """Interactive command query CLI validator loop."""
    output_fn("Nova Interactive Command Payload Validator (type 'schemas' to list actions, 'exit' to quit)")
    while True:
        try:
            raw_line = input_fn("nova-validate> ")
        except EOFError:
            break

        text = raw_line.strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit", "q"):
            break
        if text.lower() == "schemas":
            output_fn("Supported actions: " + ", ".join(sorted(ACTION_PARAM_SCHEMAS.keys())))
            continue

        report = validate_command_payload(text)
        output_fn(format_validation_report(report))

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint: `python -m nova_core.router.validators`."""
    parser = argparse.ArgumentParser(description="Nova LLM structured command validator CLI")
    parser.add_argument("--action", type=str, default=None, help="Target Nova action_type (e.g., open_app)")
    parser.add_argument("--params", type=str, default=None, help="JSON string of tool parameters")
    parser.add_argument("--payload", type=str, default=None, help="Full JSON payload string")
    parser.add_argument("--list-schemas", action="store_true", help="Print all registered action JSON schemas")
    parser.add_argument("--interactive", action="store_true", help="Start interactive validation prompt")
    args = parser.parse_args(argv)

    if args.list_schemas:
        print(json.dumps(list_action_schemas(), indent=2))
        return 0

    if args.payload is not None:
        report = validate_command_payload(args.payload)
        print(format_validation_report(report))
        return 0 if report.valid else 1

    if args.action is not None:
        report = validate_tool_arguments(args.action, args.params if args.params is not None else {})
        print(format_validation_report(report))
        return 0 if report.valid else 1

    return run_interactive_validator()


if __name__ == "__main__":
    sys.exit(main())
