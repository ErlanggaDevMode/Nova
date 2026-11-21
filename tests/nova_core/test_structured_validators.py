"""Unit and API tests for Phase 14: LLM Structured Response Validators (F14.1-F14.5)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nova_core.db.store import DatabaseStore
from nova_core.main import app
from nova_core.router.llm_client import LLMClient
from nova_core.router.validators import (
    ACTION_PARAM_SCHEMAS,
    ToolValidationError,
    get_action_json_schema,
    list_action_schemas,
    main as validator_cli_main,
    parse_and_validate_tool_call,
    repair_and_parse_json,
    run_interactive_validator,
    validate_command_payload,
    validate_tool_arguments,
)


# ---------------------------------------------------------------------------
# F14.1: JSON Schema & Pydantic Tool Call Argument Validators
# ---------------------------------------------------------------------------


def test_all_action_schemas_registered_and_exportable():
    schemas = list_action_schemas()
    expected_actions = {
        "open_app",
        "get_battery",
        "list_running_apps",
        "get_system_info",
        "search_files",
        "read_file",
        "write_file",
        "delete_file",
        "run_script",
        "install_package",
        "tuya_control_device",
    }
    assert expected_actions.issubset(set(schemas.keys()))
    for action_name in expected_actions:
        schema = get_action_json_schema(action_name)
        assert schema["type"] == "object"
        assert "properties" in schema

    with pytest.raises(KeyError):
        get_action_json_schema("nonexistent_action")


def test_valid_tool_arguments_for_nova_actions():
    res_open = validate_tool_arguments("open_app", {"app_name": "  vscode  "})
    assert res_open.valid is True
    assert res_open.category == "app_control"
    assert res_open.normalized_params == {"app_name": "vscode"}

    res_bat = validate_tool_arguments("get_battery", {})
    assert res_bat.valid is True
    assert res_bat.normalized_params == {}

    res_write = validate_tool_arguments(
        "write_file",
        {"path": "~/Documents/nova-workspace/out.txt", "content": ""},
    )
    assert res_write.valid is True
    assert res_write.normalized_params == {
        "path": "~/Documents/nova-workspace/out.txt",
        "content": "",
    }

    res_tuya = validate_tool_arguments(
        "tuya_control_device",
        {"device_id": "lamp_1", "command_name": "switch_led", "value": True},
    )
    assert res_tuya.valid is True
    assert res_tuya.normalized_params["value"] is True

    res_pkg = validate_tool_arguments("install_package", {"package": "httpx"})
    assert res_pkg.valid is True
    assert res_pkg.normalized_params == {"package": "httpx", "manager": "pip"}


def test_invalid_tool_arguments_return_structured_errors_and_suggestions():
    # Missing required field
    res_missing = validate_tool_arguments("write_file", {"path": "/tmp/demo.txt"})
    assert res_missing.valid is False
    assert any(e.code == "missing_required_field" and e.field == "params.content" for e in res_missing.errors)
    assert len(res_missing.suggestions) == 1

    # Wrong field type (strict bool & strict str)
    res_bad_bool = validate_tool_arguments(
        "tuya_control_device",
        {"device_id": "lamp_1", "command_name": "switch_led", "value": "yes"},
    )
    assert res_bad_bool.valid is False
    assert res_bad_bool.errors[0].code == "invalid_type"
    assert "boolean" in res_bad_bool.suggestions[0].lower()

    # Empty whitespace string
    res_blank = validate_tool_arguments("open_app", {"app_name": "   "})
    assert res_blank.valid is False
    assert res_blank.errors[0].field == "params.app_name"

    # Unexpected typo field suggests correct key
    res_extra = validate_tool_arguments("open_app", {"app": "notepad"})
    assert res_extra.valid is False
    codes = {e.code for e in res_extra.errors}
    assert "unexpected_field" in codes
    assert any("app_name" in s for s in res_extra.suggestions)

    # Unknown action suggests closest match
    res_unknown = validate_tool_arguments("open_ap", {"app_name": "notepad"})
    assert res_unknown.valid is False
    assert res_unknown.errors[0].code == "unknown_action"
    assert "open_app" in res_unknown.suggestions[0]


# ---------------------------------------------------------------------------
# F14.2: Structured Output JSON Parsing & Error Repair Logic
# ---------------------------------------------------------------------------


def test_repair_json_markdown_fences_and_prose():
    raw = 'Sure! Here is the tool call:\n```json\n{"app_name": "notepad"}\n```\nHope that helps.'
    res = repair_and_parse_json(raw)
    assert res.parsed == {"app_name": "notepad"}
    assert res.repaired is True
    assert "stripped_markdown_fences" in res.repairs_applied


def test_repair_json_trailing_commas_preserves_string_commas():
    raw = '{"path": "/tmp/a, } b.txt", "content": "hello, world",}'
    res = repair_and_parse_json(raw)
    assert res.parsed == {"path": "/tmp/a, } b.txt", "content": "hello, world"}
    assert res.repaired is True
    assert "removed_trailing_commas" in res.repairs_applied


def test_repair_json_single_quotes_and_python_literals():
    raw_py = "{'device_id': 'light_1', 'command_name': 'switch_led', 'value': True,}"
    res_py = repair_and_parse_json(raw_py)
    assert res_py.parsed == {
        "device_id": "light_1",
        "command_name": "switch_led",
        "value": True,
    }
    assert res_py.repaired is True

    raw_mixed = "{'device_id': 'light_1', 'command_name': 'switch_led', 'value': false}"
    res_mixed = repair_and_parse_json(raw_mixed)
    assert res_mixed.parsed == {
        "device_id": "light_1",
        "command_name": "switch_led",
        "value": False,
    }
    assert res_mixed.repaired is True


def test_repair_json_partial_wrappers_unclosed_braces_and_unquoted_keys():
    # Partial wrapper with prose + truncated brace
    raw_partial = 'Tool output: {"app_name": "vscode"'
    res_partial = repair_and_parse_json(raw_partial)
    assert res_partial.parsed == {"app_name": "vscode"}
    assert res_partial.repaired is True
    assert "balanced_closing_braces" in res_partial.repairs_applied

    # Unquoted keys
    raw_unquoted = '{query: "budget.pdf", use_regex: false}'
    res_unquoted = repair_and_parse_json(raw_unquoted)
    assert res_unquoted.parsed == {"query": "budget.pdf", "use_regex": False}
    assert res_unquoted.repaired is True
    assert "quoted_unquoted_keys" in res_unquoted.repairs_applied

    # Double-encoded JSON string
    raw_double = '"{\\"app_name\\": \\"calc\\"}"'
    res_double = repair_and_parse_json(raw_double)
    assert res_double.parsed == {"app_name": "calc"}
    assert res_double.repaired is True


def test_repair_json_rejects_irreparable_or_non_object():
    res_list = repair_and_parse_json("[1, 2, 3]")
    assert res_list.parsed is None
    assert "JSON object" in (res_list.error or "")

    res_garbage = repair_and_parse_json("totally not json at all {{{")
    assert res_garbage.parsed is None
    assert res_garbage.error is not None

    with pytest.raises(ToolValidationError):
        parse_and_validate_tool_call("open_app", "not valid json")


@patch("openai.resources.chat.completions.Completions.create")
def test_llm_client_repairs_malformed_tool_arguments(mock_create, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "llama3")

    mock_choice = MagicMock()
    mock_choice.message.content = "Turning light on"
    mock_tool_call = MagicMock()
    mock_tool_call.function.name = "tuya_control_device"
    # Malformed fenced + single quotes + trailing comma output from local model
    mock_tool_call.function.arguments = (
        "```json\n{'device_id': 'light_9', 'command_name': 'switch_led', 'value': true,}\n```"
    )
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_create.return_value.choices = [mock_choice]

    client = LLMClient()
    action, text = client.query("turn on bedroom light", "device_1")
    assert action is not None
    assert action.action_type == "tuya_control_device"
    assert action.params == {
        "device_id": "light_9",
        "command_name": "switch_led",
        "value": True,
    }
    assert text == "Turning light on"


# ---------------------------------------------------------------------------
# F14.3: Interactive Command Query Validator & CLI Helper
# ---------------------------------------------------------------------------


def test_validate_command_payload_modes():
    # Dict form
    r1 = validate_command_payload({"action_type": "open_app", "params": {"app_name": "vscode"}})
    assert r1.valid is True
    assert r1.normalized_params == {"app_name": "vscode"}

    # Malformed raw JSON string payload
    r2 = validate_command_payload(
        '```json\n{"tool_name": "search_files", "arguments": {\'query\': \'*.py\',}}\n```'
    )
    assert r2.valid is True
    assert r2.repaired is True
    assert r2.action_type == "search_files"
    assert r2.normalized_params == {"query": "*.py"}

    # Missing required field yields structured error + suggestion
    r3 = validate_command_payload({"action_type": "run_script", "params": {}})
    assert r3.valid is False
    assert r3.errors[0].field == "params.command"
    assert "command" in r3.suggestions[0]


def test_interactive_validator_repl_and_cli(capsys):
    inputs = iter(
        [
            "schemas",
            '{"action_type": "open_app", "params": {"app_name": "calc"}}',
            '{"action_type": "open_app", "params": {"wrong": "calc"}}',
            "exit",
        ]
    )
    outputs: list[str] = []
    exit_code = run_interactive_validator(
        input_fn=lambda _prompt: next(inputs),
        output_fn=outputs.append,
    )
    assert exit_code == 0
    joined = "\n".join(outputs)
    assert "Supported actions:" in joined
    assert "[VALID] action=open_app" in joined
    assert "[INVALID] action=open_app" in joined
    assert "Suggestion:" in joined

    # One-shot CLI checks
    assert validator_cli_main(["--action", "open_app", "--params", '{"app_name": "notepad"}']) == 0
    assert validator_cli_main(["--action", "open_app", "--params", "{}"]) == 1
    assert validator_cli_main(["--list-schemas"]) == 0
    captured = capsys.readouterr()
    assert "tuya_control_device" in captured.out


# ---------------------------------------------------------------------------
# F14.4: execution_history Migration, Rollback & Store Methods
# ---------------------------------------------------------------------------


def test_execution_history_migration_store_and_rollback(tmp_path):
    db_file = tmp_path / "exec_hist.db"
    store = DatabaseStore(db_path=db_file)

    # Migration 0006 must be applied automatically
    with store.get_connection() as conn:
        mig_row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = '0006_execution_history'"
        ).fetchone()
        assert mig_row is not None

    # Insert execution history entries
    id1 = store.log_execution_history(
        command_id="cmd-1",
        action_id="act-1",
        action_type="open_app",
        category="app_control",
        source_device_id="dev-laptop",
        status="success",
        params={"app_name": "vscode"},
        result={"pid": 1234},
        validated=True,
        repaired=False,
        duration_ms=14.25,
    )
    id2 = store.log_execution_history(
        command_id="cmd-2",
        action_id="act-2",
        action_type="tuya_control_device",
        category="smart_home",
        source_device_id="dev-phone",
        status="validation_error",
        params={},
        error_message="Missing value",
        validated=False,
        repaired=True,
        duration_ms=5.75,
    )
    assert id1 is not None and id2 is not None

    # Query all + filtered
    all_rows = store.get_execution_history(limit=10)
    assert len(all_rows) == 2
    assert all_rows[0]["action_type"] == "tuya_control_device"
    assert all_rows[0]["repaired"] is True
    assert all_rows[1]["params"] == {"app_name": "vscode"}
    assert all_rows[1]["result"] == {"pid": 1234}

    filtered = store.get_execution_history(action_type="open_app", status="success")
    assert len(filtered) == 1
    assert filtered[0]["source_device_id"] == "dev-laptop"

    stats = store.get_execution_history_stats()
    assert stats["total"] == 2
    assert stats["success_count"] == 1
    assert stats["failed_count"] == 1
    assert stats["repaired_count"] == 1
    assert stats["avg_duration_ms"] == 10.0

    # Export & import round-trip includes execution_history
    backup_file = tmp_path / "backup_hist.json"
    exp = store.export_data_to_json(backup_file)
    assert exp["success"] is True
    assert "execution_history" in exp["tables"]
    imp = store.import_data_from_json(backup_file)
    assert imp["success"] is True
    assert len(store.get_execution_history()) == 2

    # Rollback migration 0006 drops table cleanly
    rolled_back = store.rollback_migration("0006_execution_history")
    assert rolled_back is True
    with pytest.raises(Exception):
        with store.get_connection() as conn:
            conn.execute("SELECT * FROM execution_history")

    # Re-applying init_db restores table
    store.init_db()
    assert store.get_execution_history() == []


# ---------------------------------------------------------------------------
# F14.5: REST Endpoints (/api/v1/commands/validate, /schemas, /history)
# ---------------------------------------------------------------------------


def test_rest_command_validation_schemas_and_history_endpoints(tmp_path, monkeypatch):
    temp_store = DatabaseStore(db_path=tmp_path / "api_validators.db")
    monkeypatch.setitem(app.state._state, "store", temp_store)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    client = TestClient(app)

    # 1. GET /api/v1/commands/schemas
    res_schemas = client.get("/api/v1/commands/schemas")
    assert res_schemas.status_code == 200
    body_schemas = res_schemas.json()
    assert body_schemas["success"] is True
    assert body_schemas["count"] == len(ACTION_PARAM_SCHEMAS)
    assert "open_app" in body_schemas["schemas"]

    # 2. GET /api/v1/commands/schemas/{action_type}
    res_one = client.get("/api/v1/commands/schemas/open_app")
    assert res_one.status_code == 200
    assert res_one.json()["schema"]["properties"]["app_name"]["type"] == "string"

    res_missing_schema = client.get("/api/v1/commands/schemas/does_not_exist")
    assert res_missing_schema.status_code == 404

    # 3. POST /api/v1/commands/validate (valid + repaired raw_payload + record_history)
    res_valid = client.post(
        "/api/v1/commands/validate",
        json={
            "action_type": "tuya_control_device",
            "params": "```json\n{'device_id': 'lamp_1', 'command_name': 'switch_led', 'value': true,}\n```",
            "source_device_id": "cli-dev",
            "record_history": True,
        },
    )
    assert res_valid.status_code == 200
    data_valid = res_valid.json()
    assert data_valid["valid"] is True
    assert data_valid["repaired"] is True
    assert data_valid["normalized_params"] == {
        "device_id": "lamp_1",
        "command_name": "switch_led",
        "value": True,
    }

    # 4. POST /api/v1/commands/validate (invalid + record_history)
    res_invalid = client.post(
        "/api/v1/commands/validate",
        json={
            "action_type": "open_app",
            "params": {"app": "vscode"},
            "source_device_id": "cli-dev",
            "record_history": True,
        },
    )
    assert res_invalid.status_code == 200
    data_invalid = res_invalid.json()
    assert data_invalid["valid"] is False
    assert len(data_invalid["errors"]) >= 1
    assert any("app_name" in s for s in data_invalid["suggestions"])

    # 5. GET /api/v1/commands/history
    res_hist = client.get("/api/v1/commands/history", params={"source_device_id": "cli-dev"})
    assert res_hist.status_code == 200
    hist_data = res_hist.json()
    assert hist_data["success"] is True
    assert hist_data["count"] == 2
    assert hist_data["stats"]["total"] == 2
    assert hist_data["stats"]["success_count"] == 1
    assert hist_data["stats"]["failed_count"] == 1
    assert hist_data["stats"]["repaired_count"] == 1

    # Filtered history
    res_hist_filtered = client.get("/api/v1/commands/history", params={"status": "validation_error"})
    assert res_hist_filtered.status_code == 200
    assert res_hist_filtered.json()["count"] == 1


def test_rest_validation_endpoints_respect_jwt_auth(tmp_path, monkeypatch):
    temp_store = DatabaseStore(db_path=tmp_path / "api_auth.db")
    monkeypatch.setitem(app.state._state, "store", temp_store)
    monkeypatch.setenv("ADMIN_PASSWORD", "secret-phase14")
    client = TestClient(app)

    # Unauthenticated request rejected
    res_unauth = client.get("/api/v1/commands/history")
    assert res_unauth.status_code == 401

    # Authenticate and access
    tok_res = client.post("/auth/token", json={"username": "admin", "password": "secret-phase14"})
    assert tok_res.status_code == 200
    headers = {"Authorization": f"Bearer {tok_res.json()['access_token']}"}

    res_auth = client.post(
        "/api/v1/commands/validate",
        json={"action_type": "get_battery", "params": {}},
        headers=headers,
    )
    assert res_auth.status_code == 200
    assert res_auth.json()["valid"] is True
