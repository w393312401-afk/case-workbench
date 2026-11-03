@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在更新程序...
git pull --ff-only
if not exist ".venv\Scripts\python.exe" (
  echo 首次运行，正在安装环境，需要几分钟...
  py -3.12 -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
echo 本机工作台已启动，浏览器会打开导入页。请保持这个窗口，关闭窗口就会停止。
start "" /b powershell -NoProfile -Command "Start-Sleep 4; Start-Process 'http://127.0.0.1:8767/#production/import'"
".venv\Scripts\python.exe" server.py --no-open
goto end
:fail
echo 安装失败：请确认这台电脑已安装 Python 3.12，再重新双击。
:end
pause
