@echo off
echo ==================================================
echo  Nova Assistant Environment Setup (Windows)
echo ==================================================

echo [1/3] Creating virtual environment (.venv)...
python -m venv .venv
if %errorlevel% neq 0 (
    echo Error: Failed to create virtual environment. Make sure Python is installed.
    exit /b %errorlevel%
)

echo [2/3] Activating virtual environment & installing dependencies...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e .
if %errorlevel% neq 0 (
    echo Error: Dependency installation failed.
    exit /b %errorlevel%
)

echo [3/3] Creating default .env configuration file...
if not exist .env (
    echo LLM_PROVIDER=anthropic> .env
    echo LLM_API_KEY=>> .env
    echo ADMIN_PASSWORD=nayakacode>> .env
    echo DB_TYPE=sqlite>> .env
    echo.>> .env
    echo # Optional Smart Home Config>> .env
    echo TUYA_CLIENT_ID=>> .env
    echo TUYA_CLIENT_SECRET=>> .env
    echo TUYA_REGION=us>> .env
    echo.>> .env
    echo # Optional Local LLM Config>> .env
    echo LOCAL_LLM_URL=http://localhost:11434/v1>> .env
    echo LOCAL_LLM_MODEL=llama3>> .env
    echo Configured default .env file.
) else (
    echo .env file already exists. Skipping creation.
)

echo ==================================================
echo  Setup Completed Successfully!
echo  To run the server: python -m nova_core.main
echo  To run the desktop client: python -m desktop_agent.main
echo ==================================================
