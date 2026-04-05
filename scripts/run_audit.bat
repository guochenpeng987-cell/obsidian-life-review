@echo off
chcp 65001 >nul
cd /d %~dp0\..
if not exist config.json (
  echo [ERROR] config.json not found.
  echo Please copy config.example.json to config.json and edit it first.
  pause
  exit /b 1
)

python scripts\build_audit_pack.py --config config.json
if errorlevel 1 (
  echo.
  echo [FAILED] Audit build failed.
  pause
  exit /b 1
)

echo.
echo [OK] Audit pack built successfully.
echo Next step:
echo   1. git add .
echo   2. git commit -m "night audit"
echo   3. git push
pause
