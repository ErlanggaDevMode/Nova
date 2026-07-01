# Nova Assistant — User and Developer Guide

Welcome to Nova, a personal, cross-device assistant with a conversational brain and a permissioned action layer. This guide covers how to set up, configure, run, and understand Nova's core systems.

---

## 🚀 1. Quick Start & Installation

Nova is designed to run locally on your system, orchestrating actions across multiple connected devices (such as your desktop and Android clients) through the centralized **Nova Core** server.

### System Requirements
* Python 3.10 or higher
* Windows, macOS, or Linux (cross-platform adapters handle OS-specific actions)

### Installation Steps

1. **Clone or Navigate to the Repository:**
   ```bash
   cd c:\T\Syntho
   ```

2. **Run the Environment Setup Script:**
   On Windows, double-click or run `setup.bat`:
   ```cmd
   setup.bat
   ```
   This will:
   * Create a Python virtual environment (`.venv`).
   * Upgrade pip and install all core dependencies.
   * Generate a default `.env` configuration file.

3. **Configure Environment Variables (`.env`):**
   Open the generated `.env` file and populate the variables:
   ```env
   LLM_PROVIDER=anthropic
   LLM_API_KEY=your_anthropic_api_key_here
   ADMIN_PASSWORD=nayakacode
   DB_TYPE=sqlite
   ```

4. **Install Development/Test Dependencies:**
   To run unit tests, install optional dependencies:
   ```cmd
   .venv\Scripts\pip install -e ".[test]"
   ```

---

## 🖥️ 2. Running Nova

Nova consists of the centralized **Nova Core** server and various **Clients** (Desktop Agent CLI, Android Agent, Web Dashboard).

### A. Starting Nova Core Server
Run the FastAPI server from the repository root:
```bash
.venv\Scripts\python -m nova_core.main
```
By default, the server runs on `http://127.0.0.1:8000`. The API documentation is available at `http://127.0.0.1:8000/docs`.

### B. Starting the Desktop Agent Client
Run the interactive CLI client:
```bash
.venv\Scripts\python -m desktop_agent.main
```
This launches a CLI loop where you can directly submit instructions to the assistant.

### C. Accessing the Web Dashboard
Open your browser and navigate to `http://127.0.0.1:8000/`.
* Enter your configured `ADMIN_PASSWORD` (default: `nayakacode`) to authenticate.
* Once logged in, you can execute commands, inspect active context/devices, and manage automation rules.

---

## 🧠 3. Core Architecture & Concepts

```
[Android Client] ─┐
[Desktop Client] ─┼──→ [Nova Core Server] ──→ [Hybrid Router] ─┬→ [Local Regex Matcher]
[Web Client]      ─┘         │                                  └→ [Cloud LLM API]
                             ├──→ [Sync Engine (WebSocket)]
                             ├──→ [Permission Registry]
                             └──→ [Context / State Store]
```

### A. Hybrid AI Routing
Nova uses a **Hybrid Router** to process instructions:
1. **Local Path (Regex Matcher):** Commands like *"open vscode"*, *"what is my battery"*, or *"set alarm at 8:00"* are matched locally using fast regex rules. This resolves in under **500ms** and avoids cloud costs.
2. **Cloud Path (LLM Fallback):** If no local pattern matches, the instruction is routed to a Cloud LLM (e.g., Anthropic Claude) via tool/function calling.

### B. Permission Registry (`policy.yaml`)
Nova's core safety differentiator is its strict **Permission Registry** located in `nova_core/policy.yaml`. All action requests—regardless of whether they originate from local matches, cloud LLM execution, or background automations—must check this policy.

| Category | Default Confirmation Level | Description |
|---|---|---|
| `read_only_info` | `none` | Battery, location, calendar, running apps |
| `app_control` | `none` for whitelisted apps, `confirm` otherwise | VS Code, Browser, etc. |
| `communication` | `confirm` | WhatsApp, SMS, Email |
| `file_system` | `none` (reads) / `confirm` (writes & deletes) | Gated to specific allowed folder paths |
| `shell_command` | `always_explicit` | Raw scripts and terminal commands; **never bypassable** |

### C. Context Syncing & Device Hierarchy
Nova synchronizes active task state across all connected devices using WebSockets. When a device updates context, Nova merges it and resolves conflicts using a deterministic **Device Priority Hierarchy**:

$$\text{Desktop Agent (Priority 3)} > \text{Android Agent (Priority 2)} > \text{Web Dashboard (Priority 1)}$$

* **Conflict Resolution Window:** If two devices attempt to write to the same context key within a **5-second window**, the update from the device with the lower priority is rejected and logged to the `context_conflicts` table.
* **Deep Merging:** Dictionaries are recursively deep-merged so partial updates don't overwrite unrelated keys.
* **List Truncation:** To prevent bloating, list/array values are capped at the **last 5 elements**.
* **Sliding Window Budget:** When active context size exceeds **1500 characters** in formatting, lower-priority keys are pruned first.
* **Time-Based Expiry (TTL):** Active contexts auto-expire: High-priority states live for **1 hour (3600s)**, normal-priority states live for **30 minutes (1800s)**.

---

## 🧪 4. Running Verification & Tests

Nova includes a complete test suite under `tests/` utilizing pytest.

### Run All Tests
```bash
.venv\Scripts\python -m pytest
```

### Run Specific Test Modules
* **Context Optimizations & Expiry:** `.venv\Scripts\python -m pytest tests/nova_core/test_context_optimizations.py`
* **Context Sync & Conflict Rejections:** `.venv\Scripts\python -m pytest tests/nova_core/test_context_sync.py`
* **Automation Engine:** `.venv\Scripts\python -m pytest tests/nova_core/test_automation_engine.py`
* **Database & Migration Tests:** `.venv\Scripts\python -m pytest tests/nova_core/test_migrations.py`
