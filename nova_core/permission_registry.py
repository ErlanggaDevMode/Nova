import os
from pathlib import Path
import yaml
from nova_core.models import ActionRequest, PermissionDecision

class PermissionRegistry:
    def __init__(self, policy_path: Path | str | None = None):
        if policy_path is None:
            # Default to policy.yaml next to this file
            policy_path = Path(__file__).parent / "policy.yaml"
        self.policy_path = Path(policy_path)
        self.policy = self._load_policy()

    def _load_policy(self) -> dict:
        if not self.policy_path.exists():
            raise FileNotFoundError(f"Policy file not found at {self.policy_path}")
        with open(self.policy_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def reload(self) -> None:
        """Reload the policy from disk."""
        self.policy = self._load_policy()

    def check(self, action: ActionRequest) -> PermissionDecision:
        category = action.category

        # 1. Enforce code-level security floor for shell commands
        if category == "shell_command":
            return PermissionDecision(
                allowed=True,
                requires_confirmation=True,
                reason="Shell/system commands always require explicit confirmation, unconditionally."
            )

        # 2. Retrieve the category policies
        categories = self.policy.get("categories", {})
        if category not in categories:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"Category '{category}' is not registered in permission policy."
            )

        cat_policy = categories[category]

        # 3. Evaluate based on category
        if category == "app_control":
            return self._check_app_control(action, cat_policy)
        elif category == "read_only_info":
            return self._check_read_only_info(action, cat_policy)
        elif category == "communication":
            return self._check_communication(action, cat_policy)
        elif category == "file_system":
            return self._check_file_system(action, cat_policy)
        elif category == "smart_home":
            return self._check_smart_home(action, cat_policy)

        # Default fallback: deny if category logic isn't explicitly implemented
        return PermissionDecision(
            allowed=False,
            requires_confirmation=False,
            reason=f"Permission check for category '{category}' is not implemented."
        )

    def _check_smart_home(self, action: ActionRequest, policy: dict) -> PermissionDecision:
        allowed_actions = policy.get("actions", [])
        if action.action_type not in allowed_actions:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"Action '{action.action_type}' is not an allowed smart home action."
            )
        confirm_level = policy.get("confirmation", "none")
        return PermissionDecision(
            allowed=True,
            requires_confirmation=(confirm_level != "none"),
            reason="Smart home action allowed."
        )

    def _check_app_control(self, action: ActionRequest, policy: dict) -> PermissionDecision:
        app_name = action.params.get("app_name")
        executable = action.params.get("executable")

        if not app_name and not executable:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason="App control action requires 'app_name' or 'executable' param."
            )

        # Search whitelist
        whitelist = policy.get("whitelist", [])
        matched_app = None
        for app in whitelist:
            name_match = app_name and app.get("name") == app_name.lower()
            exec_match = executable and app.get("executable") == executable.lower()
            if name_match or exec_match:
                matched_app = app
                break

        if matched_app:
            # Found in whitelist: confirmation level from the category config (default: none)
            confirm_level = policy.get("confirmation", "none")
            requires_conf = (confirm_level != "none")
            return PermissionDecision(
                allowed=True,
                requires_confirmation=requires_conf,
                reason=f"App '{app_name or executable}' is whitelisted."
            )
        else:
            # Not in whitelist: falls back to default_confirmation (default: confirm)
            default_confirm = policy.get("default_confirmation", "confirm")
            if default_confirm == "deny":
                return PermissionDecision(
                    allowed=False,
                    requires_confirmation=False,
                    reason=f"App '{app_name or executable}' is not whitelisted and default confirmation is 'deny'."
                )
            
            requires_conf = (default_confirm != "none")
            return PermissionDecision(
                allowed=True,
                requires_confirmation=requires_conf,
                reason=f"App '{app_name or executable}' is not whitelisted. Requires confirmation."
            )

    def _check_read_only_info(self, action: ActionRequest, policy: dict) -> PermissionDecision:
        allowed_actions = policy.get("actions", [])
        if action.action_type not in allowed_actions:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"Action '{action.action_type}' is not an allowed read-only action."
            )
        confirm_level = policy.get("confirmation", "none")
        return PermissionDecision(
            allowed=True,
            requires_confirmation=(confirm_level != "none"),
            reason="Read-only action allowed."
        )

    def _check_communication(self, action: ActionRequest, policy: dict) -> PermissionDecision:
        allowed_actions = policy.get("actions", [])
        if action.action_type not in allowed_actions:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"Action '{action.action_type}' is not an allowed communication action."
            )
        confirm_level = policy.get("confirmation", "confirm")
        return PermissionDecision(
            allowed=True,
            requires_confirmation=(confirm_level != "none"),
            reason="Communication action allowed with confirmation."
        )

    def _check_file_system(self, action: ActionRequest, policy: dict) -> PermissionDecision:
        action_type = action.action_type
        
        if action_type not in ("search_files", "read_file", "write_file", "delete_file"):
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"Action '{action_type}' is not a valid file_system action."
            )
            
        # Determine policy section (read vs write vs delete)
        if action_type in ("search_files", "read_file"):
            sub_policy = policy.get("read", {})
            op_name = "read"
        elif action_type == "write_file":
            sub_policy = policy.get("write", {})
            op_name = "write"
        elif action_type == "delete_file":
            sub_policy = policy.get("delete", {})
            op_name = "delete"
            
        allowed_paths = sub_policy.get("allowed_paths", [])
        confirm_level = sub_policy.get("confirmation", "confirm")
        
        path_param = action.params.get("path") or action.params.get("search_path")
        if not path_param:
            if action_type == "search_files":
                return PermissionDecision(
                    allowed=True,
                    requires_confirmation=(confirm_level != "none"),
                    reason="Search allowed across all read-permitted paths."
                )
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"File system action '{action_type}' requires a 'path' or 'search_path' parameter."
            )
            
        try:
            norm_target = os.path.normpath(os.path.expanduser(path_param))
            target_path = Path(norm_target).resolve()
        except Exception as e:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"Invalid target path '{path_param}': {str(e)}"
            )
        
        is_allowed = False
        for allowed in allowed_paths:
            try:
                allowed_path = Path(os.path.normpath(os.path.expanduser(allowed))).resolve()
                if os.path.commonpath([str(target_path), str(allowed_path)]) == str(allowed_path):
                    is_allowed = True
                    break
            except ValueError:
                continue
            except Exception:
                continue
                
        if not is_allowed:
            return PermissionDecision(
                allowed=False,
                requires_confirmation=False,
                reason=f"Path '{path_param}' is outside allowed '{op_name}' paths: {allowed_paths}"
            )
            
        return PermissionDecision(
            allowed=True,
            requires_confirmation=(confirm_level != "none"),
            reason=f"Path '{path_param}' is inside allowed '{op_name}' paths."
        )
