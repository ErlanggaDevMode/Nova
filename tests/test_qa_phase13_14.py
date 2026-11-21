"""
QA Audit & Edge-Case Regression Test Suite — Phase 13 & Phase 14.
Author: Dina (QA Engineer & Code Reviewer)

Covers:
- Phase 13: Path traversal, symlink security, non-existent files, regex validation,
            cache invalidation on add/delete, and TTL expiration.
- Phase 14: Malformed input repairs, empty inputs, nested markdown fences, oversized payloads,
            strict type enforcement, unknown action fuzzy matching, JWT auth enforcement (401),
            and execution history logging.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from desktop_agent.action_executor import ActionExecutor
from desktop_agent.actions.file_ops import FileIndexCache, FileOpsAction, detect_file_mime_type
from desktop_agent.models import ActionRequest
from desktop_agent.permission_registry import PermissionRegistry
from nova_core.db.store import DatabaseStore
from nova_core.main import app
from nova_core.router.validators import (
    ACTION_PARAM_SCHEMAS,
    CommandValidationReport,
    JSONRepairResult,
    repair_and_parse_json,
    validate_command_payload,
    validate_tool_arguments,
)


# ===========================================================================
# Fixtures for Phase 13 (File Operations & Permission Registry)
# ===========================================================================


@pytest.fixture
def qa_workspace(tmp_path: Path):
    """Creates a controlled sandbox directory structure for QA edge-case testing."""
    allowed_dir = tmp_path / "allowed_workspace"
    allowed_dir.mkdir(parents=True, exist_ok=True)

    outside_dir = tmp_path / "outside_workspace"
    outside_dir.mkdir(parents=True, exist_ok=True)

    # Legitimate files inside allowed_dir
    doc_file = allowed_dir / "valid_doc.txt"
    doc_file.write_text("Legitimate text content for QA testing.", encoding="utf-8")

    png_file = allowed_dir / "image.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRfake_png_header")

    sub_dir = allowed_dir / "subdir"
    sub_dir.mkdir()
    (sub_dir / "sub_doc.txt").write_text("Subdirectory content.", encoding="utf-8")

    # Sensitive/external file outside allowed_dir
    secret_file = outside_dir / "secret_credentials.txt"
    secret_file.write_text("CRITICAL_API_SECRET=sk-1234567890", encoding="utf-8")

    # Symlinks inside allowed_dir
    symlink_to_outside = allowed_dir / "symlink_leak.txt"
    symlink_to_outside.symlink_to(secret_file)

    symlink_to_inside = allowed_dir / "symlink_safe.txt"
    symlink_to_inside.symlink_to(doc_file)

    broken_symlink = allowed_dir / "symlink_broken.txt"
    broken_symlink.symlink_to(allowed_dir / "non_existent_target.txt")

    # Policy yaml allowing only allowed_dir
    policy_dict = {
        "categories": {
            "file_system": {
                "read": {
                    "confirmation": "none",
                    "actions": [
                        "search_files",
                        "read_file",
                        "detect_mime_type",
                        "get_file_mime",
                        "get_file_metadata",
                    ],
                    "allowed_paths": [str(allowed_dir)],
                    "exclude_dirs": [".git", "node_modules", "__pycache__", ".venv"],
                },
                "write": {
                    "confirmation": "confirm",
                    "allowed_paths": [str(allowed_dir)],
                },
                "delete": {
                    "confirmation": "confirm",
                    "allowed_paths": [str(allowed_dir)],
                },
            }
        }
    }
    policy_file = tmp_path / "qa_policy.yaml"
    with open(policy_file, "w", encoding="utf-8") as f:
        yaml.dump(policy_dict, f)

    registry = PermissionRegistry(policy_path=policy_file)
    mock_logger = MagicMock()
    executor = ActionExecutor(registry, mock_logger)

    return {
        "tmp_path": tmp_path,
        "allowed_dir": allowed_dir,
        "outside_dir": outside_dir,
        "doc_file": doc_file,
        "png_file": png_file,
        "sub_dir": sub_dir,
        "secret_file": secret_file,
        "symlink_to_outside": symlink_to_outside,
        "symlink_to_inside": symlink_to_inside,
        "broken_symlink": broken_symlink,
        "registry": registry,
        "executor": executor,
        "policy_dict": policy_dict,
    }


# ===========================================================================
# 1. Phase 13 QA: Security & Path Traversal Edge Cases
# ===========================================================================


def test_qa_path_traversal_direct_outside_path_blocked(qa_workspace):
    """Ensure detect_mime_type and get_file_metadata reject paths outside allowed_paths."""
    executor: ActionExecutor = qa_workspace["executor"]
    secret_path = str(qa_workspace["secret_file"])

    for action_type in ("detect_mime_type", "get_file_mime", "get_file_metadata", "read_file"):
        req = ActionRequest(
            category="file_system",
            action_type=action_type,
            params={"path": secret_path},
        )
        res = executor.execute(req)
        assert res["success"] is False, f"Expected {action_type} to be blocked for {secret_path}"
        assert "Permission denied" in res["error"]
        assert "outside allowed 'read' paths" in res["error"]


def test_qa_path_traversal_relative_parent_dots_blocked(qa_workspace):
    """Ensure path traversal using '../' escaping allowed_paths is strictly resolved and blocked."""
    executor: ActionExecutor = qa_workspace["executor"]
    allowed_dir = qa_workspace["allowed_dir"]
    traversal_path = str(allowed_dir / ".." / "outside_workspace" / "secret_credentials.txt")

    for action_type in ("detect_mime_type", "get_file_metadata", "read_file"):
        req = ActionRequest(
            category="file_system",
            action_type=action_type,
            params={"path": traversal_path},
        )
        res = executor.execute(req)
        assert res["success"] is False
        assert "Permission denied" in res["error"]


def test_qa_symlink_pointing_outside_blocked(qa_workspace):
    """
    Ensure symlinks inside allowed_paths that point to files outside allowed_paths
    are dereferenced by resolve() and rejected with Permission Denied.
    """
    executor: ActionExecutor = qa_workspace["executor"]
    symlink_path = str(qa_workspace["symlink_to_outside"])

    for action_type in ("detect_mime_type", "get_file_metadata", "read_file"):
        req = ActionRequest(
            category="file_system",
            action_type=action_type,
            params={"path": symlink_path},
        )
        res = executor.execute(req)
        assert res["success"] is False, f"Symlink to outside should be blocked for {action_type}"
        assert "Permission denied" in res["error"]


def test_qa_symlink_pointing_inside_allowed_permitted(qa_workspace):
    """Ensure symlinks pointing to targets within allowed_paths are permitted and report is_symlink=True."""
    executor: ActionExecutor = qa_workspace["executor"]
    symlink_path = str(qa_workspace["symlink_to_inside"])

    # 1. detect_mime_type
    req_mime = ActionRequest(
        category="file_system",
        action_type="detect_mime_type",
        params={"path": symlink_path},
    )
    res_mime = executor.execute(req_mime)
    assert res_mime["success"] is True
    assert res_mime["mime_type"] == "text/plain"

    # 2. get_file_metadata
    req_meta = ActionRequest(
        category="file_system",
        action_type="get_file_metadata",
        params={"path": symlink_path},
    )
    res_meta = executor.execute(req_meta)
    assert res_meta["success"] is True
    meta = res_meta["metadata"]
    assert meta["is_file"] is True
    assert meta["is_symlink"] is True
    assert meta["mime_type"] == "text/plain"


def test_qa_broken_symlink_handling(qa_workspace):
    """Ensure broken symlinks are handled gracefully without unhandled exceptions."""
    executor: ActionExecutor = qa_workspace["executor"]
    broken_path = str(qa_workspace["broken_symlink"])

    # Permission check passes since path is inside allowed_paths
    # FileOpsAction reports "Path does not exist"
    req_mime = ActionRequest(
        category="file_system",
        action_type="detect_mime_type",
        params={"path": broken_path},
    )
    res_mime = executor.execute(req_mime)
    assert res_mime["success"] is False
    assert "Path does not exist" in res_mime["error"]

    req_meta = ActionRequest(
        category="file_system",
        action_type="get_file_metadata",
        params={"path": broken_path},
    )
    res_meta = executor.execute(req_meta)
    assert res_meta["success"] is False
    assert "Path does not exist" in res_meta["error"]


def test_qa_nonexistent_file_handling(qa_workspace):
    """Ensure non-existent files inside allowed_paths return clear error messages."""
    executor: ActionExecutor = qa_workspace["executor"]
    ghost_path = str(qa_workspace["allowed_dir"] / "ghost_file.txt")

    res_mime = executor.execute(
        ActionRequest(
            category="file_system",
            action_type="detect_mime_type",
            params={"path": ghost_path},
        )
    )
    assert res_mime["success"] is False
    assert f"Path does not exist: {ghost_path}" in res_mime["error"]

    res_meta = executor.execute(
        ActionRequest(
            category="file_system",
            action_type="get_file_metadata",
            params={"path": ghost_path},
        )
    )
    assert res_meta["success"] is False
    assert f"Path does not exist: {ghost_path}" in res_meta["error"]


def test_qa_search_files_outside_allowed_paths_blocked(qa_workspace):
    """Ensure search_files cannot search directories outside allowed_paths."""
    executor: ActionExecutor = qa_workspace["executor"]
    outside_dir = str(qa_workspace["outside_dir"])

    req = ActionRequest(
        category="file_system",
        action_type="search_files",
        params={"query": "secret", "search_path": outside_dir},
    )
    res = executor.execute(req)
    assert res["success"] is False
    assert "Permission denied" in res["error"]


def test_qa_search_files_invalid_regex_handling(qa_workspace):
    """Ensure malformed/invalid regex patterns return structured errors instead of crashing."""
    executor: ActionExecutor = qa_workspace["executor"]
    allowed_dir = str(qa_workspace["allowed_dir"])

    invalid_patterns = [
        "[unclosed_bracket",
        "*invalid_starting_quantifier",
        "(?P<incomplete_group",
        "\\",
    ]

    for pattern in invalid_patterns:
        # 1. Via regex string parameter
        req1 = ActionRequest(
            category="file_system",
            action_type="search_files",
            params={"regex": pattern, "search_path": allowed_dir},
        )
        res1 = executor.execute(req1)
        assert res1["success"] is False
        assert "Invalid regular expression" in res1["error"]

        # 2. Via query + regex=True
        req2 = ActionRequest(
            category="file_system",
            action_type="search_files",
            params={"query": pattern, "regex": True, "search_path": allowed_dir},
        )
        res2 = executor.execute(req2)
        assert res2["success"] is False
        assert "Invalid regular expression" in res2["error"]


def test_qa_search_files_complex_regex_and_options(qa_workspace):
    """Verify regex filtering with case-sensitivity and match_path options."""
    executor: ActionExecutor = qa_workspace["executor"]
    allowed_dir = str(qa_workspace["allowed_dir"])

    # Regex targeting png or txt
    req = ActionRequest(
        category="file_system",
        action_type="search_files",
        params={"regex": r"\.(png|txt)$", "search_path": allowed_dir},
    )
    res = executor.execute(req)
    assert res["success"] is True
    found_names = [Path(p).name for p in res["files"]]
    assert "valid_doc.txt" in found_names
    assert "image.png" in found_names

    # match_path=True matching relative path with subfolder
    req_path = ActionRequest(
        category="file_system",
        action_type="search_files",
        params={
            "regex": r"subdir[\\/]sub_doc\.txt$",
            "match_path": True,
            "search_path": allowed_dir,
        },
    )
    res_path = executor.execute(req_path)
    assert res_path["success"] is True
    assert len(res_path["files"]) == 1
    assert Path(res_path["files"][0]).name == "sub_doc.txt"


def test_qa_file_index_cache_invalidation_on_file_addition(qa_workspace):
    """
    Ensure FileIndexCache invalidates correctly when a file is added:
    1. Through direct disk write (directory mtime changes)
    2. Through write_file action (cache.invalidate)
    """
    allowed_dir: Path = qa_workspace["allowed_dir"]
    ops = FileOpsAction(policy=qa_workspace["policy_dict"], cache_ttl=60.0)

    # Initial search (Cache MISS)
    res1 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res1["success"] is True
    assert res1["cached"] is False
    initial_count = len(res1["files"])

    # Second search (Cache HIT)
    res2 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res2["success"] is True
    assert res2["cached"] is True
    assert len(res2["files"]) == initial_count

    # 1. External file addition (disk write changes dir mtime)
    # Ensure mtime has advanced
    time.sleep(0.01)
    new_file = allowed_dir / "external_doc.txt"
    new_file.write_text("External file addition", encoding="utf-8")

    res3 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res3["success"] is True
    # Cache must miss because directory mtime changed
    assert res3["cached"] is False
    assert any("external_doc.txt" in p for p in res3["files"])

    # Now it is cached again
    res4 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res4["cached"] is True

    # 2. Action write_file addition (explicit cache.invalidate)
    action_file = allowed_dir / "action_doc.txt"
    res_write = ops.execute("write_file", {"path": str(action_file), "content": "Action file addition"})
    assert res_write["success"] is True

    res5 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res5["success"] is True
    assert res5["cached"] is False
    assert any("action_doc.txt" in p for p in res5["files"])


def test_qa_file_index_cache_invalidation_on_file_deletion(qa_workspace):
    """
    Ensure FileIndexCache invalidates correctly when a file is deleted:
    1. Through direct disk unlink (directory mtime changes)
    2. Through delete_file action (cache.invalidate)
    """
    allowed_dir: Path = qa_workspace["allowed_dir"]
    target_file = allowed_dir / "to_delete.txt"
    target_file.write_text("File to be deleted", encoding="utf-8")

    ops = FileOpsAction(policy=qa_workspace["policy_dict"], cache_ttl=60.0)

    # Prime cache
    res1 = ops.execute("search_files", {"query": "to_delete", "search_path": str(allowed_dir)})
    assert res1["success"] is True
    assert len(res1["files"]) == 1

    # Verify cached
    res2 = ops.execute("search_files", {"query": "to_delete", "search_path": str(allowed_dir)})
    assert res2["cached"] is True

    # 1. Delete via delete_file action
    res_del = ops.execute("delete_file", {"path": str(target_file)})
    assert res_del["success"] is True

    res3 = ops.execute("search_files", {"query": "to_delete", "search_path": str(allowed_dir)})
    assert res3["success"] is True
    assert res3["cached"] is False
    assert len(res3["files"]) == 0

    # 2. External unlink
    temp_del = allowed_dir / "temp_del.txt"
    temp_del.write_text("Temp delete file", encoding="utf-8")
    ops.execute("search_files", {"query": "temp_del", "search_path": str(allowed_dir)})

    time.sleep(0.01)
    temp_del.unlink()

    res4 = ops.execute("search_files", {"query": "temp_del", "search_path": str(allowed_dir)})
    assert res4["cached"] is False
    assert len(res4["files"]) == 0


def test_qa_file_index_cache_ttl_expiration(qa_workspace):
    """Verify that cached entries expire after ttl_seconds elapses."""
    allowed_dir: Path = qa_workspace["allowed_dir"]
    # Very short TTL
    cache = FileIndexCache(ttl_seconds=0.05)
    ops = FileOpsAction(policy=qa_workspace["policy_dict"])
    ops.index_cache = cache

    res1 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res1["cached"] is False
    assert cache.misses == 1

    res2 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res2["cached"] is True
    assert cache.hits == 1

    # Sleep beyond TTL
    time.sleep(0.06)

    res3 = ops.execute("search_files", {"query": "doc", "search_path": str(allowed_dir)})
    assert res3["cached"] is False
    assert cache.misses == 2


# ===========================================================================
# 2. Phase 14 QA: Malformed Input, Schema Validation & JWT Security
# ===========================================================================


def test_qa_repair_json_empty_and_whitespace_inputs():
    """Ensure empty, blank, or invalid-type inputs are rejected safely with clear errors."""
    for empty_input in ("", "   ", "\n\t  \r\n"):
        res = repair_and_parse_json(empty_input)
        assert res.parsed is None
        assert res.repaired is False
        assert "Empty structured output" in (res.error or "")

        report = validate_command_payload(empty_input)
        assert report.valid is False
        assert report.errors[0].code == "json_parse_error"

    for bad_type in (None, 12345, 3.1415, ["not", "a", "dict"]):
        res_bad = repair_and_parse_json(bad_type)  # type: ignore[arg-type]
        assert res_bad.parsed is None
        assert "Expected a JSON string or dict" in (res_bad.error or "")


def test_qa_repair_json_non_dict_json_structures():
    """Ensure JSON arrays, primitives, and non-dict JSON strings are rejected."""
    for non_dict in ("[1, 2, 3]", '"just a string"', "12345", "true", "null"):
        res = repair_and_parse_json(non_dict)
        assert res.parsed is None
        assert res.repaired is False
        assert "JSON object" in (res.error or "")


def test_qa_repair_json_nested_markdown_fences_edge_case():
    """
    Audit Edge Case: Nested Markdown code fences.
    Standard single fence is successfully repaired.
    Nested fences demonstrate the documented regex limitation and fail safely without crashing.
    """
    # 1. Standard single fence works
    single_fenced = '```json\n{"app_name": "notepad"}\n```'
    res_single = repair_and_parse_json(single_fenced)
    assert res_single.parsed == {"app_name": "notepad"}
    assert res_single.repaired is True
    assert "stripped_markdown_fences" in res_single.repairs_applied

    # 2. Nested fences (outer ```markdown wrapping inner ```json)
    nested_fenced = '```markdown\n```json\n{"action_type": "open_app", "params": {"app_name": "notepad"}}\n```\n```'
    res_nested = repair_and_parse_json(nested_fenced)
    # The non-greedy fence regex stops at the second fence, resulting in empty/unparseable candidate
    assert res_nested.parsed is None
    assert "Unable to parse or repair structured JSON output" in (res_nested.error or "")

    # validate_command_payload also rejects it safely with structured report
    report = validate_command_payload(nested_fenced)
    assert report.valid is False
    assert report.errors[0].code == "json_parse_error"


def test_qa_repair_json_oversized_payload_handling():
    """Ensure oversized/garbage payloads fail safely without memory spikes or unhandled exceptions."""
    huge_prose = (
        ("This is junk text before " * 50000)
        + '{"action_type": "open_app", "params": {"app_name": "vscode"}}'
    )
    report = validate_command_payload(huge_prose)
    # Extract wrapper can still locate the embedded JSON
    assert report.valid is True
    assert report.normalized_params == {"app_name": "vscode"}

    # Pure garbage of large size
    garbage = "x" * 1000000
    report_bad = validate_command_payload(garbage)
    assert report_bad.valid is False
    assert report_bad.errors[0].code == "json_parse_error"


def test_qa_validators_strict_type_enforcement():
    """Ensure StrictStr and StrictBool correctly reject type coercion."""
    # 1. StrictBool in tuya_control_device rejects string "true" or "1"
    res_str_bool = validate_tool_arguments(
        "tuya_control_device",
        {"device_id": "lamp1", "command_name": "switch_led", "value": "true"},
    )
    assert res_str_bool.valid is False
    assert any(e.code == "invalid_type" for e in res_str_bool.errors)

    res_int_bool = validate_tool_arguments(
        "tuya_control_device",
        {"device_id": "lamp1", "command_name": "switch_led", "value": 1},
    )
    assert res_int_bool.valid is False
    assert any(e.code == "invalid_type" for e in res_int_bool.errors)

    # 2. StrictStr in open_app rejects integer 123
    res_int_str = validate_tool_arguments("open_app", {"app_name": 12345})
    assert res_int_str.valid is False
    assert any(e.code == "invalid_type" for e in res_int_str.errors)

    # 3. Non-empty string validator rejects blank string
    res_empty_str = validate_tool_arguments("open_app", {"app_name": "   "})
    assert res_empty_str.valid is False
    assert any("non-empty" in e.message.lower() for e in res_empty_str.errors)


def test_qa_validators_forbidden_extra_fields():
    """Ensure ConfigDict(extra='forbid') rejects unexpected parameter fields and suggests corrections."""
    res = validate_tool_arguments("open_app", {"app_name": "calc", "unexpected_extra": 42})
    assert res.valid is False
    assert any(e.code == "unexpected_field" for e in res.errors)

    # Typo suggestion
    res_typo = validate_tool_arguments("open_app", {"app": "calc"})
    assert res_typo.valid is False
    assert any("app_name" in s for s in res_typo.suggestions)


def test_qa_validators_unknown_action_type_and_missing_action():
    """Ensure unknown actions provide closest fuzzy matches and missing actions return error."""
    # Missing action
    res_none = validate_tool_arguments("", {"app_name": "vscode"})
    assert res_none.valid is False
    assert res_none.errors[0].code == "missing_action_type"

    # Unknown action with fuzzy suggestion
    res_fuzzy = validate_tool_arguments("search_file", {"query": "*.py"})
    assert res_fuzzy.valid is False
    assert res_fuzzy.errors[0].code == "unknown_action"
    assert "search_files" in res_fuzzy.suggestions[0]


def test_qa_api_validation_and_history_unauthenticated_returns_401(tmp_path, monkeypatch):
    """
    CRITICAL SECURITY CHECK:
    When ADMIN_PASSWORD is set, requests to /api/v1/commands/validate and /api/v1/commands/history
    WITHOUT a valid JWT Authorization Bearer header MUST return HTTP 401 Unauthorized.
    """
    temp_store = DatabaseStore(db_path=tmp_path / "qa_auth.db")
    monkeypatch.setitem(app.state._state, "store", temp_store)
    monkeypatch.setenv("ADMIN_PASSWORD", "qa_secure_password_123")

    client = TestClient(app)

    # 1. POST /api/v1/commands/validate without Authorization
    res_val_noauth = client.post(
        "/api/v1/commands/validate",
        json={"action_type": "open_app", "params": {"app_name": "calc"}},
    )
    assert res_val_noauth.status_code == 401, f"Expected 401, got {res_val_noauth.status_code}"
    assert "Authentication credentials missing" in res_val_noauth.json().get("detail", "")

    # 2. GET /api/v1/commands/history without Authorization
    res_hist_noauth = client.get("/api/v1/commands/history")
    assert res_hist_noauth.status_code == 401, f"Expected 401, got {res_hist_noauth.status_code}"

    # 3. GET /api/v1/commands/schemas without Authorization
    res_schemas_noauth = client.get("/api/v1/commands/schemas")
    assert res_schemas_noauth.status_code == 401, f"Expected 401, got {res_schemas_noauth.status_code}"

    # 4. Invalid Bearer Token
    bad_headers = {"Authorization": "Bearer invalid_or_tampered_jwt_token"}
    res_bad_token = client.post(
        "/api/v1/commands/validate",
        json={"action_type": "open_app", "params": {"app_name": "calc"}},
        headers=bad_headers,
    )
    assert res_bad_token.status_code == 401
    assert "Could not validate credentials" in res_bad_token.json().get("detail", "")


def test_qa_api_authenticated_validation_and_execution_history(tmp_path, monkeypatch):
    """Ensure authenticated requests succeed and record_history writes into SQLite execution_history."""
    temp_store = DatabaseStore(db_path=tmp_path / "qa_exec_history.db")
    monkeypatch.setitem(app.state._state, "store", temp_store)
    monkeypatch.setenv("ADMIN_PASSWORD", "qa_admin_pw")

    client = TestClient(app)

    # Login to acquire valid JWT token
    login_res = client.post("/auth/token", json={"username": "admin", "password": "qa_admin_pw"})
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Validate with valid repaired payload + record_history=True
    res_val = client.post(
        "/api/v1/commands/validate",
        json={
            "action_type": "write_file",
            "params": "{'path': '~/Documents/report.txt', 'content': 'QA Report',}",
            "source_device_id": "qa_test_suite",
            "record_history": True,
        },
        headers=headers,
    )
    assert res_val.status_code == 200
    data_val = res_val.json()
    assert data_val["valid"] is True
    assert data_val["repaired"] is True
    assert data_val["normalized_params"]["content"] == "QA Report"

    # 2. Validate with invalid payload + record_history=True
    res_invalid = client.post(
        "/api/v1/commands/validate",
        json={
            "action_type": "open_app",
            "params": {},
            "source_device_id": "qa_test_suite",
            "record_history": True,
        },
        headers=headers,
    )
    assert res_invalid.status_code == 200
    assert res_invalid.json()["valid"] is False

    # 3. Query /api/v1/commands/history with JWT
    res_hist = client.get(
        "/api/v1/commands/history",
        params={"source_device_id": "qa_test_suite"},
        headers=headers,
    )
    assert res_hist.status_code == 200
    hist_json = res_hist.json()
    assert hist_json["success"] is True
    assert hist_json["count"] == 2
    assert hist_json["stats"]["total"] == 2
    assert hist_json["stats"]["success_count"] == 1
    assert hist_json["stats"]["failed_count"] == 1


def test_qa_api_schemas_endpoint_authenticated(tmp_path, monkeypatch):
    """Verify that authenticated requests to /schemas and /schemas/{action} return valid JSON Schemas."""
    temp_store = DatabaseStore(db_path=tmp_path / "qa_schemas.db")
    monkeypatch.setitem(app.state._state, "store", temp_store)
    monkeypatch.setenv("ADMIN_PASSWORD", "qa_schema_pw")

    client = TestClient(app)
    token = client.post("/auth/token", json={"username": "admin", "password": "qa_schema_pw"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res_list = client.get("/api/v1/commands/schemas", headers=headers)
    assert res_list.status_code == 200
    schemas = res_list.json()["schemas"]
    assert len(schemas) == len(ACTION_PARAM_SCHEMAS)
    assert "tuya_control_device" in schemas

    res_single = client.get("/api/v1/commands/schemas/tuya_control_device", headers=headers)
    assert res_single.status_code == 200
    assert res_single.json()["schema"]["properties"]["device_id"]["type"] == "string"

    res_unknown = client.get("/api/v1/commands/schemas/non_existent_action", headers=headers)
    assert res_unknown.status_code == 404
