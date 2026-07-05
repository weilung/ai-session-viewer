#!/usr/bin/env bash
# 重新產生對話檢視器並打開索引頁（Debian / macOS）
# 首次使用請先：chmod +x run.sh
cd "$(dirname "$0")" || exit 1
python3 ai_session_viewer.py --out out --open
