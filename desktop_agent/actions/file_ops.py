import os
from pathlib import Path

class FileOpsAction:
    def __init__(self, policy: dict):
        self.policy = policy

    def execute(self, action_type: str, params: dict) -> dict:
        """
        Executes file operations (search, read, write, delete) under whitelisted rules.
        """
        if action_type == "search_files":
            query = params.get("query")
            if not query:
                return {"success": False, "error": "Missing 'query' parameter for file search"}
            
            search_path = params.get("search_path")
            paths_to_search = []
            if search_path:
                paths_to_search.append(search_path)
            else:
                # Default to policy allowed read paths
                read_policy = self.policy.get("categories", {}).get("file_system", {}).get("read", {})
                paths_to_search = read_policy.get("allowed_paths", [])
            
            results = []
            for path_str in paths_to_search:
                resolved_base = Path(os.path.expanduser(path_str)).resolve()
                if not resolved_base.exists() or not resolved_base.is_dir():
                    continue
                # Recursive walk, find files containing query (case-insensitive substring)
                for root, dirs, files in os.walk(resolved_base):
                    for file in files:
                        if query.lower() in file.lower():
                            full_path = os.path.join(root, file)
                            results.append(full_path)
                            if len(results) >= 50:
                                break
                    if len(results) >= 50:
                        break
            return {"success": True, "files": results}

        elif action_type == "read_file":
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

        elif action_type == "write_file":
            path_str = params.get("path")
            content = params.get("content", "")
            if not path_str:
                return {"success": False, "error": "Missing 'path' parameter for write operation"}
            
            try:
                resolved_path = Path(os.path.expanduser(path_str)).resolve()
                resolved_path.parent.mkdir(parents=True, exist_ok=True)
                with open(resolved_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return {"success": True, "path": path_str, "message": f"Successfully wrote content to {path_str}"}
            except Exception as e:
                return {"success": False, "error": f"Write error: {str(e)}"}

        elif action_type == "delete_file":
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
                return {"success": True, "path": path_str, "message": f"Successfully deleted file {path_str}"}
            except Exception as e:
                return {"success": False, "error": f"Delete error: {str(e)}"}

        else:
            return {"success": False, "error": f"Unsupported action type '{action_type}' for file_ops"}
