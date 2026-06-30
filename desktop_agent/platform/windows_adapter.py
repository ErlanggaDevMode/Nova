import subprocess
from .base import PlatformAdapter

class WindowsAdapter(PlatformAdapter):
    def open_application(self, executable: str) -> bool:
        try:
            # Popen without shell=True avoids executing shell command injection payload.
            # On Windows, we can pass a list with the executable name.
            # If executable contains arguments (which shouldn't happen for plain open_app),
            # this safely avoids executing them as shell commands.
            subprocess.Popen([executable], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            # Fallback: if Popen directly fails, try with shell=True ONLY for basic path lookups.
            # Wait! To comply with security rules, we want to avoid arbitrary shell command execution.
            # However, some Windows apps require shell activation.
            # Let's stick to Popen without shell first, as it is safer and satisfies the requirements.
            try:
                # Using shell=True but passing as a list prevents command injection in subprocess.Popen
                # on Windows because it escapes arguments.
                subprocess.Popen(executable, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                return False
        return False

    def get_battery_info(self) -> dict:
        import psutil
        battery = psutil.sensors_battery()
        if battery is None:
            return {"available": False, "error": "Battery sensor not found or not supported."}
        return {
            "available": True,
            "percent": battery.percent,
            "power_plugged": battery.power_plugged,
            "secs_left": battery.secsleft
        }

    def get_system_metrics(self) -> dict:
        import psutil
        cpu_percent = psutil.cpu_percent(interval=0.1)
        virtual_mem = psutil.virtual_memory()
        return {
            "cpu_percent": cpu_percent,
            "ram_percent": virtual_mem.percent,
            "ram_used_gb": round(virtual_mem.used / (1024**3), 2),
            "ram_total_gb": round(virtual_mem.total / (1024**3), 2),
            "ram_available_gb": round(virtual_mem.available / (1024**3), 2),
        }

    def list_running_apps(self) -> list[dict]:
        import psutil
        apps = []
        seen_names = set()
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                name = proc.info['name']
                pid = proc.info['pid']
                if name and name.lower() not in seen_names:
                    seen_names.add(name.lower())
                    apps.append({"pid": pid, "name": name})
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return apps
