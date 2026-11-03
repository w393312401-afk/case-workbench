#!/bin/bash
# Mac: 双击运行。自动更新程序、装好环境、启动本机工作台并打开导入页。
cd "$(dirname "$0")" || exit 1
echo "正在更新程序..."
git pull --ff-only

PY="$(command -v python3.12 || command -v python3)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 12))'; then
  echo "没有找到 Python 3.12 或更高版本，请先从 python.org 安装，再重新双击。"
  read -r -p "按回车关闭"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "首次运行，正在安装环境，需要几分钟..."
  "$PY" -m venv .venv || { echo "创建环境失败。"; read -r -p "按回车关闭"; exit 1; }
fi
.venv/bin/python -m pip install -q -r requirements.txt || { echo "安装依赖失败。"; read -r -p "按回车关闭"; exit 1; }

# AI 剪辑要用达芬奇自带的 ResolveMCP；没手动指定时在应用里找一次。
if [ -z "$RESOLVE_MCP" ]; then
  RESOLVE_MCP="$(find "/Applications/DaVinci Resolve" -maxdepth 6 -type f -iname 'ResolveMCP*' 2>/dev/null | head -n 1)"
  [ -n "$RESOLVE_MCP" ] && export RESOLVE_MCP
fi
[ -z "$RESOLVE_MCP" ] && echo "提示：没有找到 ResolveMCP，AI 剪辑暂不可用；找到后用 RESOLVE_MCP 环境变量指向它。"

echo "本机工作台已启动，浏览器会打开导入页。请保持这个窗口，关闭窗口就会停止。"
( sleep 4; open "http://127.0.0.1:8767/#production/import" ) &
.venv/bin/python server.py --no-open
