from desktop_agent.intent_matcher import IntentMatcher

def test_intent_matcher_open_app():
    matcher = IntentMatcher()
    
    # Check "open <app>" match
    action = matcher.try_match("open notepad")
    assert action is not None
    assert action.action_type == "open_app"
    assert action.category == "app_control"
    assert action.params == {"app_name": "notepad"}

    # Check "launch <app>" match
    action2 = matcher.try_match("launch calc")
    assert action2 is not None
    assert action2.action_type == "open_app"
    assert action2.category == "app_control"
    assert action2.params == {"app_name": "calc"}

    # Case insensitivity and stripping
    action3 = matcher.try_match("  OPEN notepad  ")
    assert action3 is not None
    assert action3.params == {"app_name": "notepad"}

def test_intent_matcher_no_match():
    matcher = IntentMatcher()
    
    # Check non-matching command
    action = matcher.try_match("hello world")
    assert action is None

def test_intent_matcher_system_info():
    matcher = IntentMatcher()
    
    action = matcher.try_match("show battery")
    assert action is not None
    assert action.action_type == "get_battery"
    assert action.category == "read_only_info"
    
    action = matcher.try_match("list running apps")
    assert action is not None
    assert action.action_type == "list_running_apps"
    assert action.category == "read_only_info"

    action = matcher.try_match("system metrics")
    assert action is not None
    assert action.action_type == "get_system_info"
    assert action.category == "read_only_info"

def test_intent_matcher_file_ops():
    matcher = IntentMatcher()
    
    action = matcher.try_match("find file main.py")
    assert action is not None
    assert action.action_type == "search_files"
    assert action.category == "file_system"
    assert action.params == {"query": "main.py"}

    action = matcher.try_match("search file test in ~/Documents")
    assert action is not None
    assert action.action_type == "search_files"
    assert action.category == "file_system"
    assert action.params == {"query": "test", "search_path": "~/Documents"}

    action = matcher.try_match("read file ~/Downloads/test.txt")
    assert action is not None
    assert action.action_type == "read_file"
    assert action.params == {"path": "~/Downloads/test.txt"}

    action = matcher.try_match("write file ~/Documents/test.txt with content Hello World")
    assert action is not None
    assert action.action_type == "write_file"
    assert action.params == {"path": "~/Documents/test.txt", "content": "Hello World"}

    action = matcher.try_match("delete file test.txt")
    assert action is not None
    assert action.action_type == "delete_file"
    assert action.params == {"path": "test.txt"}

def test_intent_matcher_shell_commands():
    matcher = IntentMatcher()
    
    action = matcher.try_match("run command echo 'hello'")
    assert action is not None
    assert action.action_type == "run_script"
    assert action.category == "shell_command"
    assert action.params == {"command": "echo 'hello'"}

    action = matcher.try_match("pip install requests")
    assert action is not None
    assert action.action_type == "install_package"
    assert action.category == "shell_command"
    assert action.params == {"package": "requests", "manager": "pip"}

