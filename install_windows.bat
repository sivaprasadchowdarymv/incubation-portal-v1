@echo off
REM One-time install on Windows: double-click this file.
cd /d "%~dp0"
python -m venv .venv || (echo Python not found. Install Python 3.12 and tick "Add python.exe to PATH". & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt || (echo Package install failed. Check the internet connection and run again. & pause & exit /b 1)
if not exist .env copy .env.example .env
if not exist storage\incubation.db python scripts\init_db.py --admin-user admin
echo.
echo Installed. Now double-click start_portal.bat
pause
