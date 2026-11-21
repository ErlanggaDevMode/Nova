import mimetypes
import os
import re
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".hg",
    ".svn",
    ".pytest_cache",
    ".mypy_cache",
})

# Extra extension mappings that Python's mimetypes may omit on minimal OS setups
_EXTRA_EXTENSION_MIME_TYPES: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".toml": "application/toml",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".jsx": "text/jsx",
    ".jsonl": "application/x-ndjson",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".wasm": "application/wasm",
}

# ZIP-based container extensions where extension MIME type is more specific than application/zip
_ZIP_CONTAINER_EXTENSIONS: frozenset[str] = frozenset({
    ".docx",
    ".xlsx",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".jar",
    ".apk",
    ".epub",
})


@dataclass
class _CacheEntry:
    files: list[str]
    created_at: float
    dir_mtimes: dict[str, int]


class FileIndexCache:
    """In-memory TTL file index cache with directory mtime-based invalidation."""

    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, frozenset[str]], _CacheEntry] = {}
        self.hits: int = 0
        self.misses: int = 0

    def _is_valid(self, entry: _CacheEntry) -> bool:
        if self.ttl_seconds <= 0:
            return False
        if (time.monotonic() - entry.created_at) > self.ttl_seconds:
            return False
        for dir_path, recorded_mtime_ns in entry.dir_mtimes.items():
            try:
                if os.stat(dir_path).st_mtime_ns != recorded_mtime_ns:
                    return False
            except OSError:
                return False
        return True

    def get(self, base_path: Path, exclude_dirs: frozenset[str]) -> list[str] | None:
        key = (str(base_path), exclude_dirs)
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        if not self._is_valid(entry):
            self._entries.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return entry.files

    def put(
        self,
        base_path: Path,
        exclude_dirs: frozenset[str],
        files: list[str],
        dir_mtimes: dict[str, int],
    ) -> None:
        if self.ttl_seconds <= 0:
            return
        key = (str(base_path), exclude_dirs)
        self._entries[key] = _CacheEntry(
            files=files,
            created_at=time.monotonic(),
            dir_mtimes=dir_mtimes,
        )

    def invalidate(self, path: Path | str | None = None) -> None:
        if path is None:
            self._entries.clear()
            return
        target = str(Path(path).resolve())
        keys_to_remove = []
        for key in self._entries:
            base_str = key[0]
            try:
                if (
                    os.path.commonpath([target, base_str]) == base_str
                    or os.path.commonpath([target, base_str]) == target
                ):
                    keys_to_remove.append(key)
            except ValueError:
                continue
        for key in keys_to_remove:
            self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def stats(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "entries": len(self._entries),
        }


def _detect_magic_bytes(header: bytes) -> str | None:
    """Detect common MIME types from leading magic bytes."""
    if not header:
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF":
        subtype = header[8:12]
        if subtype == b"WEBP":
            return "image/webp"
        if subtype == b"WAVE":
            return "audio/wav"
        if subtype == b"AVI ":
            return "video/x-msvideo"
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip"
    if header.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if header.startswith(b"BZh"):
        return "application/x-bzip2"
    if header.startswith(b"\xfd7zXZ\x00"):
        return "application/x-xz"
    if header.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if header.startswith(b"\x7fELF"):
        return "application/x-elf"
    if header.startswith(b"MZ"):
        return "application/x-msdownload"
    if header.startswith(b"SQLite format 3\x00"):
        return "application/vnd.sqlite3"
    if header.startswith(b"ID3") or header[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"OggS"):
        return "application/ogg"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4"
    stripped = header.lstrip()
    if stripped.startswith(b"<?xml"):
        return "application/xml"
    lower_stripped = stripped.lower()
    if lower_stripped.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return None


def detect_file_mime_type(file_path: Path) -> tuple[str, str | None, str]:
    """
    Detect MIME type for a file or directory using magic bytes, mimetypes, and content heuristics.
    Returns (mime_type, encoding, detection_method).
    """
    if file_path.is_dir():
        return ("inode/directory", None, "directory")

    ext = file_path.suffix.lower()
    guessed_mime, encoding = mimetypes.guess_type(str(file_path))
    if not guessed_mime and ext in _EXTRA_EXTENSION_MIME_TYPES:
        guessed_mime = _EXTRA_EXTENSION_MIME_TYPES[ext]

    header = b""
    sample = b""
    try:
        with open(file_path, "rb") as f:
            sample = f.read(512)
            header = sample[:32]
    except OSError:
        if guessed_mime:
            return (guessed_mime, encoding, "mimetypes")
        return ("application/octet-stream", None, "fallback")

    magic_mime = _detect_magic_bytes(header)
    if magic_mime:
        # For ZIP-based Office/archive formats, prefer specific extension MIME type when known
        if magic_mime == "application/zip" and ext in _ZIP_CONTAINER_EXTENSIONS and guessed_mime:
            return (guessed_mime, encoding, "mimetypes")
        return (magic_mime, encoding, "magic_bytes")

    if guessed_mime:
        return (guessed_mime, encoding, "mimetypes")

    # Content heuristic: UTF-8 decodable without NUL bytes -> text/plain
    if sample and b"\x00" not in sample:
        try:
            sample.decode("utf-8")
            return ("text/plain", None, "content_heuristic")
        except UnicodeDecodeError:
            pass

    return ("application/octet-stream", None, "fallback")


class FileOpsAction:
    def __init__(self, policy: dict, cache_ttl: float = 60.0):
        self.policy = policy
        self.index_cache = FileIndexCache(ttl_seconds=cache_ttl)

    def _resolve_exclude_dirs(self, params: dict) -> frozenset[str]:
        read_policy = self.policy.get("categories", {}).get("file_system", {}).get("read", {})
        policy_excludes = read_policy.get("exclude_dirs")
        base_defaults = (
            frozenset(policy_excludes) if isinstance(policy_excludes, (list, set, tuple)) else DEFAULT_EXCLUDE_DIRS
        )

        custom_excludes = params.get("exclude_dirs")
        use_defaults = params.get("use_default_excludes", True)

        if custom_excludes is None:
            return base_defaults if use_defaults else frozenset()

        if isinstance(custom_excludes, str):
            custom_set = {custom_excludes}
        else:
            custom_set = set(custom_excludes)

        # Explicitly passing exclude_dirs=[] opts out of default excludes
        if len(custom_set) == 0 or not use_defaults:
            return frozenset(custom_set)

        return frozenset(base_defaults | custom_set)

    def _index_directory(
        self,
        resolved_base: Path,
        exclude_dirs: frozenset[str],
        use_cache: bool = True,
    ) -> tuple[list[str], bool]:
        if use_cache:
            cached_files = self.index_cache.get(resolved_base, exclude_dirs)
            if cached_files is not None:
                return cached_files, True

        discovered_files: list[str] = []
        dir_mtimes: dict[str, int] = {}

        for root, dirs, files in os.walk(resolved_base):
            # Prune excluded directories in-place so os.walk skips their subtrees
            if exclude_dirs:
                dirs[:] = [d for d in dirs if d not in exclude_dirs]
            try:
                dir_mtimes[root] = os.stat(root).st_mtime_ns
            except OSError:
                pass
            for file in files:
                discovered_files.append(os.path.join(root, file))

        if use_cache:
            self.index_cache.put(resolved_base, exclude_dirs, discovered_files, dir_mtimes)

        return discovered_files, False

    def execute(self, action_type: str, params: dict) -> dict:
        """
        Executes file operations (search, read, write, delete, MIME detection, metadata)
        under whitelisted rules.
        """
        if action_type == "search_files":
            return self._search_files(params)
        elif action_type == "read_file":
            return self._read_file(params)
        elif action_type == "write_file":
            return self._write_file(params)
        elif action_type == "delete_file":
            return self._delete_file(params)
        elif action_type in ("detect_mime_type", "get_file_mime"):
            return self._detect_mime_type(params)
        elif action_type == "get_file_metadata":
            return self._get_file_metadata(params)
        else:
            return {"success": False, "error": f"Unsupported action type '{action_type}' for file_ops"}

    def _search_files(self, params: dict) -> dict:
        query = params.get("query")
        regex_param = params.get("regex")
        case_sensitive = bool(params.get("case_sensitive", False))
        match_path = bool(params.get("match_path", False))
        max_results = int(params.get("max_results", 50))
        use_cache = bool(params.get("use_cache", True))

        regex_pattern: re.Pattern | None = None
        substring_query: str | None = None
        re_flags = 0 if case_sensitive else re.IGNORECASE

        if isinstance(regex_param, bool):
            if regex_param:
                if not query:
                    return {"success": False, "error": "Missing 'query' parameter for regex file search"}
                try:
                    regex_pattern = re.compile(query, re_flags)
                except re.error as e:
                    return {"success": False, "error": f"Invalid regular expression '{query}': {e}"}
            else:
                if not query:
                    return {"success": False, "error": "Missing 'query' parameter for file search"}
                substring_query = query
        elif isinstance(regex_param, str) and regex_param:
            try:
                regex_pattern = re.compile(regex_param, re_flags)
            except re.error as e:
                return {"success": False, "error": f"Invalid regular expression '{regex_param}': {e}"}
            if query:
                substring_query = query
        else:
            if not query:
                return {"success": False, "error": "Missing 'query' parameter for file search"}
            substring_query = query

        search_path = params.get("search_path")
        paths_to_search: list[str] = []
        if search_path:
            paths_to_search.append(search_path)
        else:
            read_policy = self.policy.get("categories", {}).get("file_system", {}).get("read", {})
            paths_to_search = read_policy.get("allowed_paths", [])

        exclude_dirs = self._resolve_exclude_dirs(params)
        results: list[str] = []
        any_walked = False
        all_cached = True

        for path_str in paths_to_search:
            resolved_base = Path(os.path.expanduser(path_str)).resolve()
            if not resolved_base.exists() or not resolved_base.is_dir():
                continue

            any_walked = True
            indexed_files, from_cache = self._index_directory(
                resolved_base, exclude_dirs, use_cache=use_cache
            )
            if not from_cache:
                all_cached = False

            for full_path in indexed_files:
                target_str = (
                    os.path.relpath(full_path, resolved_base)
                    if match_path
                    else os.path.basename(full_path)
                )

                if substring_query is not None:
                    if case_sensitive:
                        if substring_query not in target_str:
                            continue
                    else:
                        if substring_query.lower() not in target_str.lower():
                            continue

                if regex_pattern is not None:
                    if not regex_pattern.search(target_str):
                        continue

                results.append(full_path)
                if len(results) >= max_results:
                    break

            if len(results) >= max_results:
                break

        return {
            "success": True,
            "files": results,
            "cached": any_walked and all_cached,
        }

    def _read_file(self, params: dict) -> dict:
        path_str = params.get("path")
        if not path_str:
            return {"success": False, "error": "Missing 'path' parameter for read operation"}
        resolved_path = Path(os.path.expanduser(path_str)).resolve()
        if not resolved_path.exists():
            return {"success": False, "error": f"File does not exist: {path_str}"}
        if not resolved_path.is_file():
            return {"success": False, "error": f"Path is not a file: {path_str}"}

        try:
            with open(resolved_path, "r", encoding="utf-8", errors="replace") as f:
                # Limit to 100KB to prevent terminal flooding
                content = f.read(100000)
                if len(content) >= 100000:
                    content += "\n... [TRUNCATED due to size limit]"
                return {"success": True, "path": path_str, "content": content}
        except Exception as e:
            return {"success": False, "error": f"Read error: {str(e)}"}

    def _write_file(self, params: dict) -> dict:
        path_str = params.get("path")
        content = params.get("content", "")
        if not path_str:
            return {"success": False, "error": "Missing 'path' parameter for write operation"}

        try:
            resolved_path = Path(os.path.expanduser(path_str)).resolve()
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            with open(resolved_path, "w", encoding="utf-8") as f:
                f.write(content)
            self.index_cache.invalidate(resolved_path.parent)
            return {"success": True, "path": path_str, "message": f"Successfully wrote content to {path_str}"}
        except Exception as e:
            return {"success": False, "error": f"Write error: {str(e)}"}

    def _delete_file(self, params: dict) -> dict:
        path_str = params.get("path")
        if not path_str:
            return {"success": False, "error": "Missing 'path' parameter for delete operation"}

        try:
            resolved_path = Path(os.path.expanduser(path_str)).resolve()
            if not resolved_path.exists():
                return {"success": False, "error": f"File does not exist: {path_str}"}
            if resolved_path.is_dir():
                return {"success": False, "error": "Directory deletion is blocked for safety."}

            resolved_path.unlink()
            self.index_cache.invalidate(resolved_path.parent)
            return {"success": True, "path": path_str, "message": f"Successfully deleted file {path_str}"}
        except Exception as e:
            return {"success": False, "error": f"Delete error: {str(e)}"}

    def _detect_mime_type(self, params: dict) -> dict:
        path_str = params.get("path")
        if not path_str:
            return {"success": False, "error": "Missing 'path' parameter for MIME type detection"}

        resolved_path = Path(os.path.expanduser(path_str)).resolve()
        if not resolved_path.exists():
            return {"success": False, "error": f"Path does not exist: {path_str}"}

        mime_type, encoding, method = detect_file_mime_type(resolved_path)
        return {
            "success": True,
            "path": path_str,
            "mime_type": mime_type,
            "encoding": encoding,
            "detection_method": method,
        }

    def _get_file_metadata(self, params: dict) -> dict:
        path_str = params.get("path")
        if not path_str:
            return {"success": False, "error": "Missing 'path' parameter for metadata operation"}

        expanded_path = Path(os.path.expanduser(path_str))
        resolved_path = expanded_path.resolve()
        if not resolved_path.exists():
            return {"success": False, "error": f"Path does not exist: {path_str}"}

        try:
            st = resolved_path.stat()
            mime_type, _, _ = detect_file_mime_type(resolved_path)
            is_dir = resolved_path.is_dir()
            is_file = resolved_path.is_file()
            metadata = {
                "name": resolved_path.name,
                "path": str(resolved_path),
                "size": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
                "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "accessed_at": datetime.fromtimestamp(st.st_atime, tz=timezone.utc).isoformat(),
                "permissions": stat.filemode(st.st_mode),
                "permissions_octal": oct(stat.S_IMODE(st.st_mode)),
                "extension": resolved_path.suffix.lower() if is_file else "",
                "mime_type": mime_type,
                "is_dir": is_dir,
                "is_file": is_file,
                "is_symlink": expanded_path.is_symlink(),
            }
            return {
                "success": True,
                "path": path_str,
                "metadata": metadata,
            }
        except Exception as e:
            return {"success": False, "error": f"Metadata error: {str(e)}"}
