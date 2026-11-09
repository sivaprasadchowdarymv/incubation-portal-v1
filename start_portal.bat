@echo off
REM Starts the portal (website + emails + reminders) on this computer: http://127.0.0.1:8501
cd /d "%~dp0"
call .venv\Scripts\activate.bat
set RUN_WORKER_IN_APP=1
streamlit run app.py --server.address 127.0.0.1 --server.port 8501
pause
