import sys
from .intent_matcher import IntentMatcher
from .permission_registry import PermissionRegistry
from .logger import NovaLogger
from .action_executor import ActionExecutor

def run_loop():
    print("==================================================")
    print(" Nova Desktop Agent (Phase 1 Foundation)")
    print(" Type 'exit' or 'quit' to close.")
    print("==================================================")

    # Initialize components
    try:
        permission_registry = PermissionRegistry()
        logger = NovaLogger()
        intent_matcher = IntentMatcher()
        action_executor = ActionExecutor(permission_registry, logger)
    except Exception as e:
        print(f"Initialization error: {e}")
        sys.exit(1)

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

        # Log the raw command
        command_id = logger.log_command(user_input)

        # Match intent
        action_request = intent_matcher.try_match(user_input)

        if action_request:
            # Execute action (which checks permission registry internally)
            result = action_executor.execute(action_request, command_id)
            if result.get("success"):
                if action_request.category == "app_control":
                    print(f"Success: launched {result.get('launched')}")
                elif action_request.category == "read_only_info":
                    if action_request.action_type == "get_battery":
                        bat = result["battery"]
                        plugged = "plugged in" if bat['power_plugged'] else "not plugged in"
                        print(f"Battery: {bat['percent']}% ({plugged})")
                    elif action_request.action_type == "list_running_apps":
                        apps = result["running_apps"]
                        print(f"Running Apps ({len(apps)}):")
                        for app in apps[:15]: # Show first 15 to avoid cluttering terminal
                            print(f"  - {app['name']} (PID: {app['pid']})")
                        if len(apps) > 15:
                            print(f"  ... and {len(apps) - 15} more.")
                    elif action_request.action_type == "get_system_info":
                        metrics = result["system_metrics"]
                        bat = result.get("battery", {})
                        print("System Information:")
                        print(f"  CPU Usage: {metrics['cpu_percent']}%")
                        print(f"  RAM Usage: {metrics['ram_percent']}% ({metrics['ram_used_gb']} GB / {metrics['ram_total_gb']} GB)")
                        if bat.get("available"):
                            plugged = "plugged in" if bat['power_plugged'] else "not plugged in"
                            print(f"  Battery: {bat['percent']}% ({plugged})")
                elif action_request.category == "file_system":
                    if action_request.action_type == "search_files":
                        files = result.get("files", [])
                        print(f"Found {len(files)} file(s):")
                        for f in files:
                            print(f"  - {f}")
                    elif action_request.action_type == "read_file":
                        print(f"--- File Contents: {result.get('path')} ---")
                        print(result.get("content"))
                        print("--------------------------------------------")
                    elif action_request.action_type in ("write_file", "delete_file"):
                        print(f"Success: {result.get('message')}")
                elif action_request.category == "shell_command":
                    print(f"Success: command finished with exit code {result.get('exit_code')}")
                    if result.get("stdout"):
                        print(f"Output:\n{result.get('stdout')}")
                    if result.get("stderr"):
                        print(f"Error Output:\n{result.get('stderr')}")
            else:
                print(f"Error: {result.get('error')}")
        else:
            # Stub/local-only fallback for unmatched commands (per prd.md F1.6)
            print("I don't understand that yet. (Nova Phase 1 standalone agent)")

if __name__ == "__main__":
    run_loop()
