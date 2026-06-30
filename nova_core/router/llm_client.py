import os
import logging
from typing import Tuple, Optional
from nova_core.models import ActionRequest

logger = logging.getLogger("nova.llm_client")

# Define the standard schema list
TOOLS = [
    {
        "name": "open_app",
        "description": "Launches an application by name or executable path.",
        "parameters": {
          "type": "object",
          "properties": {
            "app_name": {"type": "string", "description": "The name of the app to launch."}
          },
          "required": ["app_name"]
        }
    },
    {
        "name": "get_battery",
        "description": "Returns current battery information.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "list_running_apps",
        "description": "Returns a list of currently running processes.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "get_system_info",
        "description": "Returns system CPU and RAM utilization metrics.",
        "parameters": {"type": "object", "properties": {}}
    },
    {
        "name": "search_files",
        "description": "Recursively searches for files matching a query.",
        "parameters": {
          "type": "object",
          "properties": {
            "query": {"type": "string", "description": "The file name query string."},
            "search_path": {"type": "string", "description": "Optional search directory path."}
          },
          "required": ["query"]
        }
    },
    {
        "name": "read_file",
        "description": "Reads the text contents of a file.",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "Absolute path to the file."}
          },
          "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Writes text content to a file, creating parent directories.",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "Target file path."},
            "content": {"type": "string", "description": "Text content to write."}
          },
          "required": ["path", "content"]
        }
    },
    {
        "name": "delete_file",
        "description": "Deletes a file at the specified path.",
        "parameters": {
          "type": "object",
          "properties": {
            "path": {"type": "string", "description": "Path to the file."}
          },
          "required": ["path"]
        }
    },
    {
        "name": "run_script",
        "description": "Runs a shell script/command unconditionally.",
        "parameters": {
          "type": "object",
          "properties": {
            "command": {"type": "string", "description": "The command string to execute."}
          },
          "required": ["command"]
        }
    },
    {
        "name": "install_package",
        "description": "Installs a package using pip or npm.",
        "parameters": {
          "type": "object",
          "properties": {
            "package": {"type": "string", "description": "The package name."},
            "manager": {"type": "string", "description": "Manager to use, default 'pip'."}
          },
          "required": ["package"]
        }
    },
    {
        "name": "tuya_control_device",
        "description": "Sends control commands (e.g. turn on, turn off) to a Tuya smart home device.",
        "parameters": {
            "type": "object",
            "properties": {
                "device_id": {"type": "string", "description": "The target Tuya device ID."},
                "command_name": {"type": "string", "description": "Command name (e.g., 'switch_led')."},
                "value": {"type": "boolean", "description": "True to turn on, False to turn off."}
            },
            "required": ["device_id", "command_name", "value"]
        }
    }
]

def map_action_to_category(action_type: str) -> str:
    if action_type == "open_app":
        return "app_control"
    elif action_type in ("get_battery", "list_running_apps", "get_system_info"):
        return "read_only_info"
    elif action_type in ("search_files", "read_file", "write_file", "delete_file"):
        return "file_system"
    elif action_type in ("run_script", "install_package"):
        return "shell_command"
    elif action_type == "tuya_control_device":
        return "smart_home"
    return "unknown"

class LLMClient:
    def __init__(self):
        from dotenv import load_dotenv
        load_dotenv() # Load local .env file

        self.provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
        self.api_key = os.environ.get("LLM_API_KEY", "").strip()
        self.model = os.environ.get("LLM_MODEL", "").strip()
        self.api_base = os.environ.get("LLM_API_BASE", "").strip()

        # Sensible defaults for models if not specified
        if not self.model:
            if self.provider == "anthropic":
                self.model = "claude-3-5-sonnet-20241022"
            elif self.provider == "openrouter":
                self.model = "anthropic/claude-3.5-sonnet"
            elif self.provider == "nvidia":
                self.model = "meta/llama-3.1-405b-instruct"

    def query(self, command: str, source_device_id: str, context_store = None) -> Tuple[Optional[ActionRequest], Optional[str]]:
        """
        Sends a command to the cloud LLM using tool calling.
        Returns a tuple: (ActionRequest or None, response_text or None).
        Falls back to stub mock behavior if no api_key is configured.
        """
        context_summary = ""
        if context_store:
            context_summary = context_store.get_system_prompt_context()

        if not self.api_key and self.provider != "ollama":
            logger.warning("LLM API key is missing. Using stub fallback reasoning.")
            return self._mock_fallback(command, source_device_id, context_store)

        try:
            if self.provider == "anthropic":
                return self._call_anthropic(command, source_device_id, context_summary)
            elif self.provider in ("openrouter", "nvidia"):
                return self._call_openai_compatible(command, source_device_id, context_summary)
            elif self.provider == "ollama":
                return self._call_ollama(command, source_device_id, context_summary)
            else:
                raise ValueError(f"Unsupported LLM provider '{self.provider}'")
        except Exception as e:
            logger.warning(f"Primary LLM provider '{self.provider}' failed: {e}. Falling back to local Ollama.")
            try:
                return self._call_ollama(command, source_device_id, context_summary)
            except Exception as ex:
                logger.error(f"Local Ollama fallback failed: {ex}. Using stub fallback.")
                return self._mock_fallback(command, source_device_id, context_store)

    def _call_anthropic(self, command: str, source_device_id: str, context_summary: str = "") -> Tuple[Optional[ActionRequest], Optional[str]]:
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key)
        
        # Convert tools to Anthropic format
        anthropic_tools = []
        for t in TOOLS:
            anthropic_tools.append({
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"]
            })

        system_prompt = "You are Nova, Erlangga's cross-device agent assistant. Translate request into function calls where appropriate."
        if context_summary:
            system_prompt += "\n" + context_summary

        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": command}],
            tools=anthropic_tools
        )

        action_req = None
        resp_text = None

        for content in response.content:
            if content.type == "text":
                resp_text = content.text
            elif content.type == "tool_use":
                action_req = ActionRequest(
                    action_type=content.name,
                    category=map_action_to_category(content.name),
                    params=content.input,
                    source_device_id=source_device_id,
                    origin="cloud_llm"
                )

        return action_req, resp_text

    def _call_openai_compatible(self, command: str, source_device_id: str, context_summary: str = "") -> Tuple[Optional[ActionRequest], Optional[str]]:
        from openai import OpenAI
        
        base_url = self.api_base if self.api_base else None
        client = OpenAI(api_key=self.api_key, base_url=base_url)

        openai_tools = []
        for t in TOOLS:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"]
                }
            })

        system_prompt = "You are Nova, Erlangga's cross-device agent assistant. Translate request into function calls where appropriate."
        if context_summary:
            system_prompt += "\n" + context_summary

        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": command}
            ],
            tools=openai_tools,
            tool_choice="auto"
        )

        choice = response.choices[0]
        resp_text = choice.message.content
        action_req = None

        if choice.message.tool_calls:
            tool_call = choice.message.tool_calls[0]
            import json
            params = json.loads(tool_call.function.arguments)
            action_req = ActionRequest(
                action_type=tool_call.function.name,
                category=map_action_to_category(tool_call.function.name),
                params=params,
                source_device_id=source_device_id,
                origin="cloud_llm"
            )

        return action_req, resp_text

    def _mock_fallback(self, command: str, source_device_id: str, context_store = None) -> Tuple[Optional[ActionRequest], Optional[str]]:
        cmd = command.strip().lower()

        # Check keyword matches to trigger stubs
        if "open" in cmd or "launch" in cmd:
            app_name = cmd.replace("open", "").replace("launch", "").strip()
            return ActionRequest(
                action_type="open_app",
                category="app_control",
                params={"app_name": app_name or "notepad"},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "battery" in cmd:
            return ActionRequest(
                action_type="get_battery",
                category="read_only_info",
                params={},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "running apps" in cmd or "list apps" in cmd:
            return ActionRequest(
                action_type="list_running_apps",
                category="read_only_info",
                params={},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "system info" in cmd or "metrics" in cmd or "cpu" in cmd or "ram" in cmd:
            return ActionRequest(
                action_type="get_system_info",
                category="read_only_info",
                params={},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "search" in cmd or "find" in cmd:
            return ActionRequest(
                action_type="search_files",
                category="file_system",
                params={"query": "test_query"},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "read file" in cmd:
            path = command.replace("read file", "").strip()
            return ActionRequest(
                action_type="read_file",
                category="file_system",
                params={"path": path or "~/Documents/cli_test.txt"},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "write file" in cmd:
            return ActionRequest(
                action_type="write_file",
                category="file_system",
                params={"path": "~/Documents/nova-workspace/cli_test.txt", "content": "mock content"},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "delete file" in cmd:
            return ActionRequest(
                action_type="delete_file",
                category="file_system",
                params={"path": "~/Documents/nova-workspace/cli_test.txt"},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "run command" in cmd or "run script" in cmd:
            return ActionRequest(
                action_type="run_script",
                category="shell_command",
                params={"command": "echo 'Hello mock'"},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "install" in cmd:
            package = cmd.replace("install", "").replace("package", "").strip()
            return ActionRequest(
                action_type="install_package",
                category="shell_command",
                params={"package": package or "requests"},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None
            
        elif "turn on" in cmd or "turn off" in cmd or "light" in cmd or "switch" in cmd:
            value = "turn on" in cmd or "on" in cmd
            return ActionRequest(
                action_type="tuya_control_device",
                category="smart_home",
                params={"device_id": "mock_tuya_light_1", "command_name": "switch_led", "value": value},
                source_device_id=source_device_id,
                origin="cloud_llm"
            ), None

        context_info = ""
        if context_store:
            context_info = context_store.get_system_prompt_context()
        msg = f"Nova cloud reasoning stub response. I received: '{command}'."
        if context_info:
            msg += f"\nActive Context: {context_info}"
        return None, msg

    def _call_ollama(self, command: str, source_device_id: str, context_summary: str = "") -> Tuple[Optional[ActionRequest], Optional[str]]:
        from openai import OpenAI
        import json
        api_base = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434/v1")
        model = os.environ.get("LOCAL_LLM_MODEL", "llama3")
        
        # Uses standard OpenAI interface targeted at Ollama client base
        client = OpenAI(api_key="ollama", base_url=api_base)
        
        openai_tools = []
        for t in TOOLS:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"]
                }
            })

        system_prompt = "You are Nova, Erlangga's cross-device agent assistant. Translate request into function calls where appropriate."
        if context_summary:
            system_prompt += "\n" + context_summary

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": command}
            ],
            tools=openai_tools,
            tool_choice="auto"
        )

        choice = response.choices[0]
        resp_text = choice.message.content
        action_req = None

        if choice.message.tool_calls:
            tool_call = choice.message.tool_calls[0]
            params = json.loads(tool_call.function.arguments)
            action_req = ActionRequest(
                action_type=tool_call.function.name,
                category=map_action_to_category(tool_call.function.name),
                params=params,
                source_device_id=source_device_id,
                origin="cloud_llm"
            )

        return action_req, resp_text
