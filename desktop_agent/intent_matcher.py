import re
from .models import ActionRequest

class IntentMatcher:
    def __init__(self):
        # List of rules: (regex_pattern, handler_method)
        self.rules = [
            (r"^(?:open|launch)\s+(.+)$", self._handle_open_app),
            (r"^(?:get|show|query)?\s*battery(?:\s*status)?$", self._handle_get_battery),
            (r"^(?:list|show|get)\s*(?:running\s+)?apps$", self._handle_list_running_apps),
            (r"^(?:show|get|query)?\s*(?:system\s+info|system\s+metrics|cpu|ram|memory)$", self._handle_get_system_info),
            (r"^(?:search|find)\s+file(?:s)?\s+(.+?)(?:\s+in\s+(.+))?$", self._handle_search_files),
            (r"^(?:read|show|view)\s+file\s+(.+)$", self._handle_read_file),
            (r"^(?:write|create|save)(?:\s+file)?\s+(.+?)\s+(?:with\s+)?content\s+(.+)$", self._handle_write_file),
            (r"^(?:delete|remove)\s+file\s+(.+)$", self._handle_delete_file),
            (r"^(?:run|execute|exec)\s+(?:script|command)\s+(.+)$", self._handle_run_script),
            (r"^(?:npm|pip)?\s*install\s+(?:package\s+)?(.+)$", self._handle_install_package),
        ]

    def try_match(self, command: str) -> ActionRequest | None:
        cmd_stripped = command.strip()
        for pattern, handler in self.rules:
            match = re.match(pattern, cmd_stripped, re.IGNORECASE)
            if match:
                return handler(match)
        return None

    def _handle_open_app(self, match: re.Match) -> ActionRequest:
        app_name = match.group(1).strip()
        return ActionRequest(
            action_type="open_app",
            category="app_control",
            params={"app_name": app_name}
        )

    def _handle_get_battery(self, match: re.Match) -> ActionRequest:
        return ActionRequest(
            action_type="get_battery",
            category="read_only_info",
            params={}
        )

    def _handle_list_running_apps(self, match: re.Match) -> ActionRequest:
        return ActionRequest(
            action_type="list_running_apps",
            category="read_only_info",
            params={}
        )

    def _handle_get_system_info(self, match: re.Match) -> ActionRequest:
        return ActionRequest(
            action_type="get_system_info",
            category="read_only_info",
            params={}
        )

    def _handle_search_files(self, match: re.Match) -> ActionRequest:
        query = match.group(1).strip()
        search_path = match.group(2).strip() if match.group(2) else None
        params = {"query": query}
        if search_path:
            params["search_path"] = search_path
        return ActionRequest(
            action_type="search_files",
            category="file_system",
            params=params
        )

    def _handle_read_file(self, match: re.Match) -> ActionRequest:
        path = match.group(1).strip()
        return ActionRequest(
            action_type="read_file",
            category="file_system",
            params={"path": path}
        )

    def _handle_write_file(self, match: re.Match) -> ActionRequest:
        path = match.group(1).strip()
        content = match.group(2).strip()
        return ActionRequest(
            action_type="write_file",
            category="file_system",
            params={"path": path, "content": content}
        )

    def _handle_delete_file(self, match: re.Match) -> ActionRequest:
        path = match.group(1).strip()
        return ActionRequest(
            action_type="delete_file",
            category="file_system",
            params={"path": path}
        )

    def _handle_run_script(self, match: re.Match) -> ActionRequest:
        cmd = match.group(1).strip()
        return ActionRequest(
            action_type="run_script",
            category="shell_command",
            params={"command": cmd}
        )

    def _handle_install_package(self, match: re.Match) -> ActionRequest:
        full_command = match.string.strip().lower()
        manager = "pip"
        if full_command.startswith("npm"):
            manager = "npm"
        package = match.group(1).strip()
        return ActionRequest(
            action_type="install_package",
            category="shell_command",
            params={"package": package, "manager": manager}
        )
