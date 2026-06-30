from ..platform.base import PlatformAdapter

class SystemInfoAction:
    def __init__(self, platform_adapter: PlatformAdapter):
        self.platform_adapter = platform_adapter

    def execute(self, action_type: str, params: dict) -> dict:
        """
        Executes system information actions (battery, running apps, system metrics).
        """
        if action_type == "get_battery":
            info = self.platform_adapter.get_battery_info()
            if info.get("available"):
                return {"success": True, "battery": info}
            else:
                return {"success": False, "error": info.get("error", "Battery info unavailable")}
        elif action_type == "list_running_apps":
            apps = self.platform_adapter.list_running_apps()
            return {"success": True, "running_apps": apps}
        elif action_type == "get_system_info":
            metrics = self.platform_adapter.get_system_metrics()
            battery = self.platform_adapter.get_battery_info()
            return {
                "success": True,
                "system_metrics": metrics,
                "battery": battery
            }
        else:
            return {"success": False, "error": f"Unsupported action type '{action_type}' for system_info"}
