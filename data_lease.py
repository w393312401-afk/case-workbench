"""A server lease prevents offline backups racing a live or starting workbench."""
from __future__ import annotations

import os
from pathlib import Path

from data_backup import BackupError, linked


class ServerLease:
    def __init__(self, runtime):
        self.path = Path(runtime).resolve() / "data-transfer" / "server.lock"
        self.stream = None

    def acquire(self):
        if self.path.parent.exists() and linked(self.path.parent):
            raise BackupError("备份工作目录不能是链接。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and linked(self.path):
            raise BackupError("服务锁文件不能是链接。")
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if not self.path.stat().st_size:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def close(self):
        if self.stream:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None
