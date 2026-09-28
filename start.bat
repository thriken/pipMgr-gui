@echo off
chcp 65001 >nul
cd /d "%~dp0"
pythonw main.py
if errorlevel 1 pause
