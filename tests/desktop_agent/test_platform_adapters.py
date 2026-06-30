import platform
from desktop_agent.platform import get_platform_adapter
from desktop_agent.platform.base import PlatformAdapter
from desktop_agent.platform.windows_adapter import WindowsAdapter
from desktop_agent.platform.linux_adapter import LinuxAdapter
from desktop_agent.platform.macos_adapter import MacOSAdapter

def test_get_platform_adapter():
    adapter = get_platform_adapter()
    assert isinstance(adapter, PlatformAdapter)
    
    system = platform.system()
    if system == "Windows":
        assert isinstance(adapter, WindowsAdapter)
    elif system == "Linux":
        assert isinstance(adapter, LinuxAdapter)
    elif system == "Darwin":
        assert isinstance(adapter, MacOSAdapter)
