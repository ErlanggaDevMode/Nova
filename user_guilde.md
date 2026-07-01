# Nova Assistant — User and Developer Guide

Please refer to the correctly spelled file [user_guide.md](file:///c:/T/Syntho/user_guide.md) for the full and up-to-date documentation.

For convenience, the setup and running steps are duplicated below:

## 🚀 1. Quick Start & Installation

1. **Clone or Navigate to the Repository:**
   ```bash
   cd c:\T\Syntho
   ```

2. **Run the Environment Setup Script:**
   On Windows, double-click or run `setup.bat`:
   ```cmd
   setup.bat
   ```

3. **Configure Environment Variables (`.env`):**
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

## 🖥️ 2. Running Nova

### A. Starting Nova Core Server
```bash
.venv\Scripts\python -m nova_core.main
```

### B. Starting the Desktop Agent Client
```bash
.venv\Scripts\python -m desktop_agent.main
```

### C. Accessing the Web Dashboard
Navigate to `http://127.0.0.1:8000/` and enter the `ADMIN_PASSWORD` (default: `nayakacode`).

---
See the detailed guide in [user_guide.md](file:///c:/T/Syntho/user_guide.md) for information on Routing, the Permission Registry, and Context Synchronization logic.
