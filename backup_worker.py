"""Scheduled backup entry: use the live server or take an offline snapshot."""
from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

from data_transfer import DataTransfer, TransferError
from data_lease import ServerLease


def run(config_path):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not config["settings"]["enabled"]:
        return
    port = config.get("port", 8767)
    base = f"http://127.0.0.1:{int(port)}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    live = None
    try:
        with opener.open(base + "/api/service/status", timeout=3) as response:
            live = json.load(response)
    except urllib.error.HTTPError as exc:
        raise TransferError("工作台服务暂时无法处理自动备份，请稍后重试。", 503) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if not isinstance(reason, ConnectionRefusedError) and getattr(reason, "errno", None) not in {errno.ECONNREFUSED, 10061}:
            raise TransferError("无法确认工作台服务已关闭，本次未尝试离线复制。", 503) from exc
    except OSError as exc:
        if not isinstance(exc, ConnectionRefusedError) and exc.errno not in {errno.ECONNREFUSED, 10061}:
            raise TransferError("工作台服务连接失败，本次未尝试离线复制。", 503) from exc
    if live is not None:
        expected = Path(config["skill_root"]).resolve() / "cases"
        received = Path(live.get("paths", {}).get("records", "")).resolve()
        runtime = Path(live.get("paths", {}).get("library", "")).resolve().parent
        if live.get("app") != "video-case-workbench" or received != expected or runtime != Path(config["runtime_root"]).resolve():
            raise TransferError("自动备份端口被其他工作台占用，请检查运行位置。")
        request = urllib.request.Request(base + "/api/data/backup/due", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json", "X-Workbench-Token": live["token"]})
        # If a live backup fails or times out, never fall back to an unlocked offline copy.
        with opener.open(request, timeout=12 * 3600) as response:
            json.load(response)
        return
    lease = ServerLease(config["runtime_root"])
    if not lease.acquire():
        raise TransferError("工作台服务正在启动或暂时无法连接，本次未尝试离线复制；下次可运行时会补备份。", 409)
    try:
        service = DataTransfer(config["skill_root"], config["runtime_root"], config["app_root"])
        service.due_backup()
    finally:
        lease.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        run(args.config)
        # A retry may succeed because the live app already completed this slot.
        # Clear the old worker failure even when no new archive was necessary.
        try:
            (Path(args.config).resolve().parent / "worker-error.log").unlink(missing_ok=True)
        except OSError:
            pass  # A completed backup must not fail because its old log is locked.
    except Exception as exc:
        # Windows uses pythonw; record a useful failure even without a console.
        path = Path(args.config).resolve().parent / "worker-error.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(exc) + "\n", encoding="utf-8")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
