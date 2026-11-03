#!/bin/zsh
# Finder 双击入口；解析链接后定位项目，兼容桌面快捷方式。
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
APP_ROOT="${0:A:h:h}"

PYTHON_BIN="$(command -v python3.12 || true)"
if [[ -z "$PYTHON_BIN" && -x "$APP_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$APP_ROOT/.venv/bin/python"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  print -u2 "找不到 Python，请先安装 Python 3.12 后再双击启动。"
  RESULT=1
elif "$PYTHON_BIN" -X utf8 -u "$APP_ROOT/deployment/launch_macos.py" "$@"; then
  exit 0
else
  RESULT=$?
fi

if [[ -t 0 ]]; then
  read -r "?按回车关闭此窗口…"
fi
exit "$RESULT"
