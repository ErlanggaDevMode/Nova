import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from desktop_agent.action_executor import ActionExecutor
from desktop_agent.actions.file_ops import DEFAULT_EXCLUDE_DIRS, FileIndexCache, FileOpsAction
from desktop_agent.logger import NovaLogger
from desktop_agent.models import ActionRequest
from desktop_agent.permission_registry import PermissionRegistry


@pytest.fixture
def mock_logger():
    return MagicMock(spec=NovaLogger)


@pytest.fixture
def sample_workspace(tmp_path):
    """Create a realistic directory tree with normal files and excluded dirs."""
    root = tmp_path / "workspace"
    root.mkdir()

    # Normal files
    (root / "report_2025.pdf").write_bytes(b"%PDF-1.4\n%fake pdf content")
    (root / "report_2026.pdf").write_bytes(b"%PDF-1.7\n%fake pdf content")
    (root / "Report_Final.txt").write_text("final report", encoding="utf-8")
    (root / "notes.md").write_text("# Notes\nHello Nova", encoding="utf-8")
    (root / "config.yaml").write_text("key: value\n", encoding="utf-8")

    src = root / "src"
    src.mkdir()
    (src / "app_v1.py").write_text("print('v1')\n", encoding="utf-8")
    (src / "app_v2.py").write_text("print('v2')\n", encoding="utf-8")
    (src / "helper.py").write_text("def help(): pass\n", encoding="utf-8")

    # Default excluded directories
    for excluded_dir in (".git", "node_modules", "__pycache__", ".venv"):
        d = root / excluded_dir
        d.mkdir()
        (d / "report_hidden.txt").write_text("should be excluded", encoding="utf-8")
        (d / "app_hidden.py").write_text("# hidden", encoding="utf-8")

    # Custom directory to exclude in custom tests
    build_dir = root / "build_artifacts"
    build_dir.mkdir()
    (build_dir / "report_build.txt").write_text("build output", encoding="utf-8")

    return root


@pytest.fixture
def fs_policy_file(tmp_path, sample_workspace):
    policy_content = {
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
                    "allowed_paths": [str(sample_workspace)],
                },
                "write": {
                    "confirmation": "confirm",
                    "allowed_paths": [str(sample_workspace)],
                },
                "delete": {
                    "confirmation": "confirm",
                    "allowed_paths": [str(sample_workspace)],
                },
            }
        }
    }
    policy_path = tmp_path / "fs_policy.yaml"
    with open(policy_path, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
    return policy_path


# ---------------------------------------------------------------------------
# F13.1: Regular expression (regex) filter tests
# ---------------------------------------------------------------------------

def test_search_files_regex_boolean_flag(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute(
        "search_files",
        {
            "query": r"^report_\d{4}\.pdf$",
            "regex": True,
            "search_path": str(sample_workspace),
        },
    )
    assert res["success"] is True
    names = sorted(Path(p).name for p in res["files"])
    assert names == ["report_2025.pdf", "report_2026.pdf"]


def test_search_files_regex_string_pattern(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute(
        "search_files",
        {
            "regex": r"^app_v\d+\.py$",
            "search_path": str(sample_workspace),
        },
    )
    assert res["success"] is True
    names = sorted(Path(p).name for p in res["files"])
    assert names == ["app_v1.py", "app_v2.py"]


def test_search_files_regex_combined_with_query_and_case_sensitivity(sample_workspace):
    ops = FileOpsAction(policy={})

    # Case-insensitive by default: matches Report_Final.txt as well
    res_ci = ops.execute(
        "search_files",
        {
            "query": "report",
            "regex": r"\.txt$",
            "search_path": str(sample_workspace),
            "exclude_dirs": ["build_artifacts"],
        },
    )
    assert res_ci["success"] is True
    assert [Path(p).name for p in res_ci["files"]] == ["Report_Final.txt"]

    # Case-sensitive: lowercase "report" does NOT match "Report_Final.txt"
    res_cs = ops.execute(
        "search_files",
        {
            "query": "report",
            "regex": r"\.txt$",
            "case_sensitive": True,
            "search_path": str(sample_workspace),
            "exclude_dirs": ["build_artifacts"],
        },
    )
    assert res_cs["success"] is True
    assert res_cs["files"] == []


def test_search_files_regex_match_path(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute(
        "search_files",
        {
            "regex": r"^src[\\/]app_v2\.py$",
            "match_path": True,
            "search_path": str(sample_workspace),
        },
    )
    assert res["success"] is True
    assert [Path(p).name for p in res["files"]] == ["app_v2.py"]


def test_search_files_invalid_regex_returns_error(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute(
        "search_files",
        {
            "regex": r"[unclosed_group",
            "search_path": str(sample_workspace),
        },
    )
    assert res["success"] is False
    assert "Invalid regular expression" in res["error"]


def test_search_files_missing_query_and_regex_returns_error(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute("search_files", {"search_path": str(sample_workspace)})
    assert res["success"] is False
    assert "Missing 'query'" in res["error"]


# ---------------------------------------------------------------------------
# F13.2: exclude_dirs filter tests
# ---------------------------------------------------------------------------

def test_search_files_excludes_default_dirs(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute(
        "search_files",
        {"query": "hidden", "search_path": str(sample_workspace)},
    )
    assert res["success"] is True
    assert res["files"] == []


def test_search_files_excludes_custom_dirs_in_addition_to_defaults(sample_workspace):
    ops = FileOpsAction(policy={})

    # Without custom exclude, build_artifacts/report_build.txt is found
    res_before = ops.execute(
        "search_files",
        {"query": "report_build", "search_path": str(sample_workspace)},
    )
    assert res_before["success"] is True
    assert len(res_before["files"]) == 1

    # With custom exclude_dirs=["build_artifacts"], both build_artifacts and default dirs are excluded
    res_after = ops.execute(
        "search_files",
        {
            "query": "report",
            "exclude_dirs": ["build_artifacts"],
            "search_path": str(sample_workspace),
        },
    )
    assert res_after["success"] is True
    found_paths = res_after["files"]
    assert all("build_artifacts" not in p for p in found_paths)
    for default_dir in (".git", "node_modules", "__pycache__", ".venv"):
        assert all(default_dir not in Path(p).parts for p in found_paths)


def test_search_files_explicit_empty_exclude_dirs_includes_all(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute(
        "search_files",
        {
            "query": "report_hidden",
            "exclude_dirs": [],
            "search_path": str(sample_workspace),
        },
    )
    assert res["success"] is True
    # Should find report_hidden.txt in .git, node_modules, __pycache__, .venv
    assert len(res["files"]) == 4


def test_search_files_use_default_excludes_false(sample_workspace):
    ops = FileOpsAction(policy={})
    res = ops.execute(
        "search_files",
        {
            "query": "report_hidden",
            "exclude_dirs": [".git", "node_modules"],
            "use_default_excludes": False,
            "search_path": str(sample_workspace),
        },
    )
    assert res["success"] is True
    # Only __pycache__ and .venv should remain
    parent_names = sorted(Path(p).parent.name for p in res["files"])
    assert parent_names == [".venv", "__pycache__"]


# ---------------------------------------------------------------------------
# F13.3: Index caching (TTL + mtime invalidation) tests
# ---------------------------------------------------------------------------

def test_index_cache_hit_and_miss_behavior(sample_workspace):
    ops = FileOpsAction(policy={}, cache_ttl=60.0)

    res1 = ops.execute(
        "search_files",
        {"query": "app_v", "search_path": str(sample_workspace)},
    )
    assert res1["success"] is True
    assert res1["cached"] is False
    assert ops.index_cache.stats["misses"] == 1
    assert ops.index_cache.stats["hits"] == 0

    # Second search with a different query on the same directory should hit cache
    with patch("desktop_agent.actions.file_ops.os.walk") as mock_walk:
        res2 = ops.execute(
            "search_files",
            {"query": "notes", "search_path": str(sample_workspace)},
        )
        assert res2["success"] is True
        assert res2["cached"] is True
        assert [Path(p).name for p in res2["files"]] == ["notes.md"]
        mock_walk.assert_not_called()
        assert ops.index_cache.stats["hits"] == 1


def test_index_cache_invalidates_on_mtime_change(sample_workspace):
    ops = FileOpsAction(policy={}, cache_ttl=60.0)

    res1 = ops.execute(
        "search_files",
        {"query": "brand_new", "search_path": str(sample_workspace)},
    )
    assert res1["success"] is True
    assert res1["files"] == []
    assert res1["cached"] is False

    # Create a new file and ensure directory mtime visibly advances
    new_file = sample_workspace / "brand_new.txt"
    new_file.write_text("hello", encoding="utf-8")
    current_ns = os.stat(sample_workspace).st_mtime_ns
    os.utime(sample_workspace, ns=(current_ns + 1_000_000, current_ns + 1_000_000))

    res2 = ops.execute(
        "search_files",
        {"query": "brand_new", "search_path": str(sample_workspace)},
    )
    assert res2["success"] is True
    assert res2["cached"] is False
    assert [Path(p).name for p in res2["files"]] == ["brand_new.txt"]


def test_index_cache_invalidates_on_ttl_expiry(sample_workspace):
    ops = FileOpsAction(policy={}, cache_ttl=0.05)

    res1 = ops.execute(
        "search_files",
        {"query": "notes", "search_path": str(sample_workspace)},
    )
    assert res1["cached"] is False

    time.sleep(0.08)

    res2 = ops.execute(
        "search_files",
        {"query": "notes", "search_path": str(sample_workspace)},
    )
    assert res2["cached"] is False
    assert ops.index_cache.stats["misses"] == 2


def test_index_cache_invalidated_by_write_and_delete_file(sample_workspace):
    ops = FileOpsAction(policy={}, cache_ttl=60.0)

    # Populate cache
    ops.execute("search_files", {"query": "ephemeral", "search_path": str(sample_workspace)})
    assert ops.index_cache.stats["entries"] == 1

    # write_file invalidates cache
    target = sample_workspace / "ephemeral.txt"
    res_write = ops.execute("write_file", {"path": str(target), "content": "temp"})
    assert res_write["success"] is True
    assert ops.index_cache.stats["entries"] == 0

    res_find = ops.execute("search_files", {"query": "ephemeral", "search_path": str(sample_workspace)})
    assert res_find["cached"] is False
    assert [Path(p).name for p in res_find["files"]] == ["ephemeral.txt"]

    # delete_file invalidates cache
    res_del = ops.execute("delete_file", {"path": str(target)})
    assert res_del["success"] is True
    assert ops.index_cache.stats["entries"] == 0


def test_index_cache_bypass_with_use_cache_false(sample_workspace):
    ops = FileOpsAction(policy={}, cache_ttl=60.0)

    res1 = ops.execute(
        "search_files",
        {"query": "notes", "search_path": str(sample_workspace), "use_cache": False},
    )
    assert res1["success"] is True
    assert res1["cached"] is False
    assert ops.index_cache.stats["entries"] == 0


# ---------------------------------------------------------------------------
# F13.4: MIME type detection (detect_mime_type / get_file_mime) tests
# ---------------------------------------------------------------------------

def test_detect_mime_type_magic_bytes(tmp_path):
    ops = FileOpsAction(policy={})

    # Files with NO extension — must be detected strictly via magic bytes
    samples = {
        "img_png": (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "image/png"),
        "img_jpg": (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
        "doc_pdf": (b"%PDF-1.7\n1 0 obj", "application/pdf"),
        "arc_zip": (b"PK\x03\x04\x14\x00\x00\x00", "application/zip"),
        "arc_gz": (b"\x1f\x8b\x08\x00\x00\x00\x00\x00", "application/gzip"),
        "db_sqlite": (b"SQLite format 3\x00extra", "application/vnd.sqlite3"),
    }

    for filename, (content, expected_mime) in samples.items():
        p = tmp_path / filename
        p.write_bytes(content)
        res = ops.execute("detect_mime_type", {"path": str(p)})
        assert res["success"] is True
        assert res["mime_type"] == expected_mime
        assert res["detection_method"] == "magic_bytes"


def test_detect_mime_type_mimetypes_and_heuristics(tmp_path):
    ops = FileOpsAction(policy={})

    json_file = tmp_path / "data.json"
    json_file.write_text('{"ok": true}', encoding="utf-8")
    res_json = ops.execute("get_file_mime", {"path": str(json_file)})
    assert res_json["success"] is True
    assert res_json["mime_type"] == "application/json"
    assert res_json["detection_method"] == "mimetypes"

    md_file = tmp_path / "README.md"
    md_file.write_text("# Title\n", encoding="utf-8")
    res_md = ops.execute("detect_mime_type", {"path": str(md_file)})
    assert res_md["success"] is True
    assert res_md["mime_type"] == "text/markdown"

    # Extensionless UTF-8 text -> content heuristic
    plain_file = tmp_path / "LICENSE"
    plain_file.write_text("MIT License", encoding="utf-8")
    res_plain = ops.execute("detect_mime_type", {"path": str(plain_file)})
    assert res_plain["success"] is True
    assert res_plain["mime_type"] == "text/plain"
    assert res_plain["detection_method"] == "content_heuristic"

    # Extensionless binary -> fallback application/octet-stream
    bin_file = tmp_path / "blob"
    bin_file.write_bytes(b"\x00\x01\x02\x03\xff\xfe")
    res_bin = ops.execute("detect_mime_type", {"path": str(bin_file)})
    assert res_bin["success"] is True
    assert res_bin["mime_type"] == "application/octet-stream"
    assert res_bin["detection_method"] == "fallback"

    # Directory -> inode/directory
    res_dir = ops.execute("detect_mime_type", {"path": str(tmp_path)})
    assert res_dir["success"] is True
    assert res_dir["mime_type"] == "inode/directory"
    assert res_dir["detection_method"] == "directory"


def test_detect_mime_type_missing_file(tmp_path):
    ops = FileOpsAction(policy={})
    res = ops.execute("detect_mime_type", {"path": str(tmp_path / "does_not_exist.bin")})
    assert res["success"] is False
    assert "does not exist" in res["error"]


# ---------------------------------------------------------------------------
# F13.5: File metadata (get_file_metadata) + Executor & Policy integration
# ---------------------------------------------------------------------------

def test_get_file_metadata_for_file_and_dir(sample_workspace):
    ops = FileOpsAction(policy={})

    pdf_path = sample_workspace / "report_2025.pdf"
    res = ops.execute("get_file_metadata", {"path": str(pdf_path)})
    assert res["success"] is True
    meta = res["metadata"]
    assert meta["name"] == "report_2025.pdf"
    assert meta["size"] == pdf_path.stat().st_size
    assert meta["extension"] == ".pdf"
    assert meta["mime_type"] == "application/pdf"
    assert meta["is_file"] is True
    assert meta["is_dir"] is False
    assert meta["is_symlink"] is False
    assert isinstance(meta["permissions"], str) and meta["permissions"].startswith("-")
    assert isinstance(meta["permissions_octal"], str) and meta["permissions_octal"].startswith("0o")
    for ts_key in ("created_at", "modified_at", "accessed_at"):
        assert isinstance(meta[ts_key], str) and "T" in meta[ts_key]

    # Directory metadata
    res_dir = ops.execute("get_file_metadata", {"path": str(sample_workspace / "src")})
    assert res_dir["success"] is True
    dir_meta = res_dir["metadata"]
    assert dir_meta["is_dir"] is True
    assert dir_meta["is_file"] is False
    assert dir_meta["extension"] == ""
    assert dir_meta["mime_type"] == "inode/directory"
    assert dir_meta["permissions"].startswith("d")


def test_executor_and_permission_registry_for_mime_and_metadata(
    fs_policy_file, sample_workspace, mock_logger
):
    registry = PermissionRegistry(fs_policy_file)
    confirm_cb = MagicMock(return_value=True)
    executor = ActionExecutor(registry, mock_logger, confirm_callback=confirm_cb)

    pdf_path = sample_workspace / "report_2025.pdf"

    # 1. detect_mime_type via ActionExecutor (read-only safe: no confirmation!)
    res_mime = executor.execute(
        ActionRequest(
            action_type="detect_mime_type",
            category="file_system",
            params={"path": str(pdf_path)},
        )
    )
    assert res_mime["success"] is True
    assert res_mime["mime_type"] == "application/pdf"
    confirm_cb.assert_not_called()

    # 2. get_file_mime alias via ActionExecutor
    res_mime_alias = executor.execute(
        ActionRequest(
            action_type="get_file_mime",
            category="file_system",
            params={"path": str(pdf_path)},
        )
    )
    assert res_mime_alias["success"] is True
    assert res_mime_alias["mime_type"] == "application/pdf"
    confirm_cb.assert_not_called()

    # 3. get_file_metadata via ActionExecutor (read-only safe: no confirmation!)
    res_meta = executor.execute(
        ActionRequest(
            action_type="get_file_metadata",
            category="file_system",
            params={"path": str(pdf_path)},
        )
    )
    assert res_meta["success"] is True
    assert res_meta["metadata"]["mime_type"] == "application/pdf"
    assert res_meta["metadata"]["is_file"] is True
    confirm_cb.assert_not_called()

    # 4. Path outside allowed_paths must be denied
    res_denied = executor.execute(
        ActionRequest(
            action_type="get_file_metadata",
            category="file_system",
            params={"path": "/etc/passwd"},
        )
    )
    assert res_denied["success"] is False
    assert "Permission denied" in res_denied["error"]


def test_default_policy_yaml_registers_new_read_only_actions():
    """Verify desktop_agent/policy.yaml registers detect_mime_type, get_file_mime, and get_file_metadata."""
    registry = PermissionRegistry()
    read_policy = registry.policy["categories"]["file_system"]["read"]
    assert read_policy["confirmation"] == "none"
    for action_name in ("search_files", "read_file", "detect_mime_type", "get_file_mime", "get_file_metadata"):
        assert action_name in read_policy["actions"]

    decision = registry.check(
        ActionRequest(
            action_type="get_file_metadata",
            category="file_system",
            params={"path": "~/Documents/example.txt"},
        )
    )
    assert decision.allowed is True
    assert decision.requires_confirmation is False
