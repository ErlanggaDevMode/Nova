from typing import Protocol, runtime_checkable

@runtime_checkable
class PlatformAdapter(Protocol):
    def open_application(self, executable: str) -> bool:
        """
        Launches an application by name or executable path.
        Returns True if launch command succeeded, False otherwise.
        """
        ...

    def get_battery_info(self) -> dict:
        """
        Returns battery information (percentage, charging status, time remaining).
        """
        ...

    def get_system_metrics(self) -> dict:
        """
        Returns system CPU and RAM utilization metrics.
        """
        ...

    def list_running_apps(self) -> list[dict]:
        """
        Returns a list of currently running processes with their name and PID.
        """
        ...
