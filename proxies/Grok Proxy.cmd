@echo off
title Grok Proxy :8319
cd /d "%~dp0"
echo ===================================================
echo   GROK PROXY v2.1 — http://127.0.0.1:8319/v1
echo ===================================================
echo.
node "%~dp0grok-proxy.js"
pause
