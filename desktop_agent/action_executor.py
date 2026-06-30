import os
from .models import ActionRequest, PermissionDecision
from .permission_registry import PermissionRegistry
from .logger import NovaLogger
from .platform import get_platform_adapter
from .actions.app_control import AppControlAction
from .actions.system_info import SystemInfoAction
from .actions.file_ops import FileOpsAction

class ActionExecutor:
    def __init__(self, permission_registry: PermissionRegistry, logger: NovaLogger, platform_adapter=None, confirm_callback=None):
        self.permission_registry = permission_registry
        self.logger = logger
        self.confirm_callback = confirm_callback
        self.platform_adapter = platform_adapter or get_platform_adapter()
        # Initialize action handlers
        self.app_control = AppControlAction(self.platform_adapter, self.permission_registry.policy)
        self.system_info = SystemInfoAction(self.platform_adapter)
        self.file_ops = FileOpsAction(self.permission_registry.policy)

    def execute(self, action: ActionRequest, command_id: str | None = None, bypass_registry: bool = False) -> dict:
        """
        Main entry point for action execution.
        Strictly checks permissions before execution, unless bypassed.
        """
        action_id = os.urandom(8).hex()
        
        if bypass_registry:
            decision_dict = {
                "allowed": True,
                "requires_confirmation": False,
                "reason": "Permission check bypassed by command server."
            }
        else:
            # 1. Strictly enforce permission check before executing (Rule 4.2 in design.md)
            decision = self.permission_registry.check(action)
            
            decision_dict = {
                "allowed": decision.allowed,
                "requires_confirmation": decision.requires_confirmation,
                "reason": decision.reason
            }
            
            if not decision.allowed:
                result = {"success": False, "error": f"Permission denied: {decision.reason}"}
                self.logger.log_action(
                    action_id=action_id,
                    command_id=command_id,
                    action_type=action.action_type,
                    category=action.category,
                    params=action.params,
                    permission_decision=decision_dict,
                    executed=False,
                    result=result
                )
                return {**result, "action_id": action_id}

            # 2. Gate execution with confirmation if required
            if decision.requires_confirmation:
                confirmed = self._request_confirmation(action)
                if not confirmed:
                    result = {"success": False, "error": "Action rejected by user"}
                    self.logger.log_action(
                        action_id=action_id,
                        command_id=command_id,
                        action_type=action.action_type,
                        category=action.category,
                        params=action.params,
                        permission_decision=decision_dict,
                        executed=False,
                        result=result
                    )
                    return {**result, "action_id": action_id}

        # 3. Perform execution
        result = {"success": False, "error": f"No handler implemented for category '{action.category}'"}
        try:
            if action.category == "app_control":
                # Ensure handler runs with latest policy
                self.app_control.policy = self.permission_registry.policy
                result = self.app_control.execute(action.action_type, action.params)
            elif action.category == "read_only_info":
                result = self.system_info.execute(action.action_type, action.params)
            elif action.category == "file_system":
                self.file_ops.policy = self.permission_registry.policy
                result = self.file_ops.execute(action.action_type, action.params)
            elif action.category == "shell_command":
                result = self._execute_shell_command(action)
            else:
                result = {"success": False, "error": f"Category '{action.category}' is not implemented in Phase 1."}
        except Exception as e:
            result = {"success": False, "error": f"Execution error: {str(e)}"}

        # 4. Log final result
        executed = result.get("success", False)
        self.logger.log_action(
            action_id=action_id,
            command_id=command_id,
            action_type=action.action_type,
            category=action.category,
            params=action.params,
            permission_decision=decision_dict,
            executed=executed,
            result=result
        )

        return {**result, "action_id": action_id}

    def _execute_shell_command(self, action: ActionRequest) -> dict:
        import subprocess
        action_type = action.action_type
        
        if action_type == "run_script":
            cmd = action.params.get("command") or action.params.get("script_path")
            if not cmd:
                return {"success": False, "error": "Missing 'command' parameter for shell script"}
            
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=60
                )
                return {
                    "success": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr
                }
            except Exception as e:
                return {"success": False, "error": f"Shell command execution error: {str(e)}"}
                
        elif action_type == "install_package":
            package = action.params.get("package")
            if not package:
                return {"success": False, "error": "Missing 'package' parameter for package installation"}
            manager = action.params.get("manager", "pip")
            
            try:
                cmd = f"{manager} install {package}"
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=120
                )
                return {
                    "success": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr
                }
            except Exception as e:
                return {"success": False, "error": f"Package installation error: {str(e)}"}
                
        else:
            return {"success": False, "error": f"Unsupported action type '{action_type}' for shell_command"}

    def _request_confirmation(self, action: ActionRequest) -> bool:
        """Helper to get user confirmation."""
        if self.confirm_callback:
            return self.confirm_callback(action)
        
        # Fallback console prompt
        print(f"\n[CONFIRMATION REQUIRED] Nova wants to execute:")
        print(f"  Category: {action.category}")
        print(f"  Action:   {action.action_type}")
        print(f"  Params:   {action.params}")
        val = input("Do you confirm this action? (yes/no): ").strip().lower()
        return val in ("y", "yes")
