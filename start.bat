@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo ================================================
echo   实时价格监控面板
echo ================================================
echo.
python server.py
pause
