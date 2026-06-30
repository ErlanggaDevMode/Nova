import platform
from .base import PlatformAdapter
from .windows_adapter import WindowsAdapter
from .linux_adapter import LinuxAdapter
from .macos_adapter import MacOSAdapter

class UnsupportedPlatformError(Exception):
    pass

def get_platform_adapter() -> PlatformAdapter:
    system = platform.system()
    if system == "Windows":
        return WindowsAdapter()
    elif system == "Linux":
        return LinuxAdapter()
    elif system == "Darwin":
        return MacOSAdapter()
    else:
        raise UnsupportedPlatformError(f"Operating system '{system}' is not supported.")
