@echo off
chcp 65001 >nul
cd /d "%~dp0"
call .venv\Scripts\activate.bat
set PYTHONIOENCODING=utf-8

echo ==========================================
echo   RAG Private Knowledge Base
echo   Open http://127.0.0.1:8000 in browser
echo ==========================================
echo.

python app.py
pause
