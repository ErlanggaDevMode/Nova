import sys
import os
from .permission_registry import PermissionRegistry
from .logger import NovaLogger
from .action_executor import ActionExecutor
from .client.core_client import CoreClient
from .models import ActionRequest

DEVICE_ID = "desktop_agent_client"

def print_result_formatted(action_type, category, result):
    if not result.get("success"):
        print(f"Error: {result.get('error')}")
        return

    if category == "app_control":
        print(f"Success: launched {result.get('launched')}")
    elif category == "read_only_info":
        if action_type == "get_battery":
            bat = result["battery"]
            plugged = "plugged in" if bat['power_plugged'] else "not plugged in"
            print(f"Battery: {bat['percent']}% ({plugged})")
        elif action_type == "list_running_apps":
            apps = result["running_apps"]
            print(f"Running Apps ({len(apps)}):")
            for app in apps[:15]:
                print(f"  - {app['name']} (PID: {app['pid']})")
            if len(apps) > 15:
                print(f"  ... and {len(apps) - 15} more.")
        elif action_type == "get_system_info":
            metrics = result["system_metrics"]
            bat = result.get("battery", {})
            print("System Information:")
            print(f"  CPU Usage: {metrics['cpu_percent']}%")
            print(f"  RAM Usage: {metrics['ram_percent']}% ({metrics['ram_used_gb']} GB / {metrics['ram_total_gb']} GB)")
            if bat.get("available"):
                plugged = "plugged in" if bat['power_plugged'] else "not plugged in"
                print(f"  Battery: {bat['percent']}% ({plugged})")
    elif category == "file_system":
        if action_type == "search_files":
            files = result.get("files", [])
            print(f"Found {len(files)} file(s):")
            for f in files:
                print(f"  - {f}")
        elif action_type == "read_file":
            print(f"--- File Contents: {result.get('path')} ---")
            print(result.get("content"))
            print("--------------------------------------------")
        elif action_type in ("write_file", "delete_file"):
            print(f"Success: {result.get('message')}")
    elif category == "shell_command":
        print(f"Success: command finished with exit code {result.get('exit_code')}")
        if result.get("stdout"):
            print(f"Output:\n{result.get('stdout')}")
        if result.get("stderr"):
            print(f"Error Output:\n{result.get('stderr')}")

def run_loop():
    print("==================================================")
    print(" Nova Desktop Agent Client (Phase 2)")
    print(" Type 'exit' or 'quit' to close.")
    print("==================================================")

    # Initialize client components
    try:
        permission_registry = PermissionRegistry()
        logger = NovaLogger()
        action_executor = ActionExecutor(permission_registry, logger)
        client = CoreClient(DEVICE_ID)
    except Exception as e:
        print(f"Initialization error: {e}")
        sys.exit(1)

    # Capabilities matching the platform adapter actions
    capabilities = {
        "open_app": True,
        "get_battery": True,
        "list_running_apps": True,
        "get_system_info": True,
        "search_files": True,
        "read_file": True,
        "write_file": True,
        "delete_file": True,
        "run_script": True,
        "install_package": True
    }
    
    # Try registering with Core
    registered = client.register_capabilities("Desktop CLI Client", capabilities)
    if not registered:
        print("[WARNING] Could not connect to Nova Core to register capabilities. Is the server running?")
    else:
        print("[INFO] Registered capabilities with Nova Core successfully.")

    # Define the WebSocket action callback
    def on_action_received(data: dict) -> dict:
        action = ActionRequest(
            action_type=data["action_type"],
            category=data["category"],
            params=data["params"],
            source_device_id=DEVICE_ID,
            origin="cloud_llm"
        )
        # Execute locally, bypassing local registry check since the server already authorized it
        result = action_executor.execute(action, bypass_registry=True)
        return result

    # Start WebSocket connection listener in background thread
    client.start_websocket_listener(on_action_received)

    while True:
        try:
            user_input = input("Nova > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        # Submit command to Core
        res = client.send_command(user_input)
        
        if not res.get("success", True):
            print(f"Error: {res.get('error')}")
            continue

        # Check if the command required confirmation
        if res.get("requires_confirmation"):
            print(f"\n[CONFIRMATION REQUIRED] Nova wants to execute:")
            print(f"  Category: {res['action']['category']}")
            print(f"  Action:   {res['action']['action_type']}")
            print(f"  Params:   {res['action']['params']}")
            confirm_input = input("Do you confirm this action? (yes/no): ").strip().lower()
            
            if confirm_input in ("y", "yes"):
                confirm_res = client.confirm_action(res["action_id"])
                if confirm_res.get("success"):
                    print_result_formatted(
                        confirm_res.get("action_type"), 
                        confirm_res.get("category"), 
                        confirm_res.get("result", {})
                    )
                else:
                    print(f"Error: {confirm_res.get('error')}")
            else:
                print("Action cancelled.")
                
        elif res.get("response_text") is not None:
            # Conversational text response
            print(res["response_text"])
            
        elif "result" in res:
            # Immediate execution result
            print_result_formatted(
                res.get("action_type"),
                res.get("category"),
                res.get("result", {})
            )

if __name__ == "__main__":
    run_loop()
