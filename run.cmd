@echo off
rem 雙擊即可重新產生對話檢視器並打開索引頁
chcp 65001 >nul
cd /d "%~dp0"
py ai_session_viewer.py --out out --open
if errorlevel 1 (
  echo.
  echo 發生錯誤，請看上方訊息。
  pause
)
