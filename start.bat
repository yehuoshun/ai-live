@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [ai-live] 启动本地服务 http://127.0.0.1:8788/puppet.html
node server.mjs
pause
