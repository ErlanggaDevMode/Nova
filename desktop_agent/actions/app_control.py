from ..platform.base import PlatformAdapter

class AppControlAction:
    def __init__(self, platform_adapter: PlatformAdapter, policy: dict):
        self.platform_adapter = platform_adapter
        self.policy = policy

    def execute(self, action_type: str, params: dict) -> dict:
        """
        Executes the app control action.
        Returns a dict indicating execution success and details.
        """
        if action_type != "open_app":
            return {"success": False, "error": f"Unsupported action type '{action_type}' for app_control"}

        app_name = params.get("app_name")
        executable = params.get("executable")

        if not app_name and not executable:
            return {"success": False, "error": "Missing 'app_name' or 'executable' parameters"}

        # If executable is not explicitly provided, try to resolve app_name to executable via policy whitelist
        target_exec = executable
        if not target_exec and app_name:
            target_exec = app_name
            # Look up in whitelist
            whitelist = self.policy.get("categories", {}).get("app_control", {}).get("whitelist", [])
            for app in whitelist:
                if app.get("name") == app_name.lower():
                    target_exec = app.get("executable")
                    break

        success = self.platform_adapter.open_application(target_exec)
        if success:
            return {"success": True, "launched": target_exec}
        else:
            return {"success": False, "error": f"Failed to launch app executable: '{target_exec}'"}
