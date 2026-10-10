#!/bin/zsh
# 案例工作台一键启动（预览模式）：关闭本工作台的旧服务，启动新实例，再打开页面。
# 关闭这个终端窗口或按 Ctrl+C 即停止服务。

APP="$HOME/Desktop/case-workbench"
PORT=8767
URL="http://127.0.0.1:$PORT"

cd "$APP" || { echo "找不到项目文件夹：$APP"; read -k1; exit 1; }

# 只结束本工作台的旧服务；端口被其他程序占用时提示，不去动它
PID=$(lsof -nP -tiTCP:$PORT -sTCP:LISTEN 2>/dev/null)
if [[ -n "$PID" ]]; then
  CWD=$(lsof -a -p "$PID" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
  if [[ "$CWD" == "$APP" ]] && ps -o command= -p "$PID" | grep -q "server.py"; then
    echo "关闭旧服务（PID $PID）…"
    kill "$PID"
    for i in {1..20}; do lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.25; done
  else
    echo "端口 $PORT 被其他程序占用：$(ps -o command= -p "$PID")"
    echo "按任意键退出。"; read -k1; exit 1
  fi
fi

# 服务就绪后自动打开浏览器
( for i in {1..40}; do
    curl -s -o /dev/null "$URL" && { open "$URL"; exit 0; }
    sleep 0.5
  done ) &

echo "启动案例工作台：$URL"
PYTHONPATH="$APP/.preview/skill/scripts" exec "$APP/.venv/bin/python" "$APP/server.py" \
  --skill-root "$APP/.preview/skill" --runtime-root "$APP/.preview/runtime" --port $PORT --no-open
