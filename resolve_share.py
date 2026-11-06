"""Same-project AI edits share DaVinci between their scripting calls.

Before: an edit held DaVinci for its whole AI session, although DaVinci sat idle about 70% of that time while the
model thought. Now the first edit of a project takes the application lease (the usual first-come line) and keeps it for
the project; other edits of that project join it and take short turns, one Resolve call at a time. Everything else —
other projects, timeline builds, user actions — still sees DaVinci busy exactly as before, so no other project is ever
loaded in between and nothing new can slip in.

New edits join only while nobody else waits for DaVinci, so a waiting build or another project's edit is never
overtaken by a stream of same-project edits.

Between turns of different edits the current timeline, playhead and page are put back: the outgoing editor's view is
read when someone else takes a turn, and restored on its next call.
"""
from __future__ import annotations

from contextlib import contextmanager
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

from codex_bridge import BridgeCancelled
from elevenlabs_client import VoiceError

# Seconds an editor keeps its turn after a call, so its next call (usually a moment later) does not hand DaVinci over.
LINGER = 1.5
GATE = Path(__file__).resolve().parent / "resolve_mcp_gate.py"
VIEW_LOST = ("提示：达芬奇刚被同项目的另一条剪辑用过，工作台没能完全切回你上次的时间线或播放头（{reason}）。"
             "继续操作前请先读取当前时间线和播放头，必要时切回你的目标时间线。")


class Share:
    def __init__(self, key, lease):
        self.key, self.lease = key, lease
        self.users = 0          # sessions inside this share
        self.queue = []         # (ticket, owner) waiting for a turn
        self.holder = None
        self.reserved = None    # (owner, deadline) while an editor lingers after a call
        self.view_owner = None  # the editor whose view is on screen
        self.views = {}         # editor -> view saved when another took DaVinci


class ProjectShares:
    def __init__(self, bridge, foreign_waiting=None, linger=LINGER):
        self.bridge, self.linger = bridge, linger
        self.foreign_waiting = foreign_waiting or (lambda: False)
        self.cond = threading.Condition()
        self.shares = {}

    def active(self, key):
        with self.cond:
            share = self.shares.get(key)
            return share.users if share else 0

    @contextmanager
    def session(self, key, owner, *, cancel, timeout, on_wait=None, ticket=None, ahead_of=()):
        """Join the project's share, or take DaVinci for the project in the usual line and open one."""
        share = None
        with self.cond:
            current = self.shares.get(key)
            if current is not None and not self.foreign_waiting():
                current.users += 1
                share = current
        if share is None:
            lease = self.bridge.access(cancel_event=cancel, timeout=timeout, on_wait=on_wait, ticket=ticket, ahead_of=ahead_of)
            lease.__enter__()
            with self.cond:
                # Holding the lease, no other share can exist: the previous one ended by releasing it.
                share = self.shares[key] = Share(key, lease)
                share.users = 1
        try:
            yield share
        finally:
            ended = False
            with self.cond:
                share.users -= 1
                share.views.pop(owner, None)
                if share.view_owner == owner:
                    share.view_owner = None
                if share.users <= 0:
                    self.shares.pop(key, None)
                    ended = True
                self.cond.notify_all()
            if ended:
                share.lease.__exit__(None, None, None)

    def _next(self, share):
        if share.reserved and time.monotonic() >= share.reserved[1]:
            share.reserved = None
        if share.reserved:
            return next((ticket for ticket, owner in share.queue if owner == share.reserved[0]), None)
        return share.queue[0][0] if share.queue else None

    @contextmanager
    def turn(self, share, owner, *, cancel, timeout, view=False, linger=0):
        """One exclusive turn inside the share. `view` editors get their timeline and playhead back; yields a note
        for the editor when that could not be done, else ""."""
        ticket, deadline = object(), time.monotonic() + timeout
        with self.cond:
            share.queue.append((ticket, owner))
            try:
                while True:
                    if cancel.is_set():
                        raise BridgeCancelled("已按你的要求停止等待达芬奇。")
                    if share.holder is None and self._next(share) is ticket:
                        break
                    if time.monotonic() >= deadline:
                        raise VoiceError(504, "等待达芬奇超时，本次任务已结束。")
                    self.cond.wait(.05)
            finally:
                share.queue = [entry for entry in share.queue if entry[0] is not ticket]
            share.holder, share.reserved = owner, None
        try:
            yield self._switch_view(share, owner, view)
        finally:
            with self.cond:
                share.holder = None
                if linger and share.queue:
                    share.reserved = (owner, time.monotonic() + linger)
                self.cond.notify_all()

    def _switch_view(self, share, owner, view):
        note = ""
        previous = share.view_owner
        if previous is not None and previous != owner:
            try:
                state = self.bridge.run("status", {}, timeout=30, held=True)
                share.views[previous] = {"project": state.get("project") or "", "project_id": state.get("project_id") or "",
                                         "unique_id": state.get("current_timeline_uid") or "", "name": state.get("current_timeline") or "",
                                         "timecode": state.get("current_timecode") or "", "page": state.get("page") or ""}
            except Exception as exc:
                share.views[previous] = {"error": str(exc)[:200] or type(exc).__name__}
        if not view:
            share.view_owner = None if previous != owner else previous
            return note
        saved = share.views.pop(owner, None) if previous != owner else None
        if saved is not None:
            if saved.get("error"):
                note = VIEW_LOST.format(reason="读取失败：" + saved["error"])
            elif saved.get("unique_id") and saved.get("project"):
                try:
                    self.bridge.run("restore", {key: value for key, value in saved.items() if value}, timeout=60, held=True)
                except Exception as exc:
                    note = VIEW_LOST.format(reason=str(exc)[:200] or type(exc).__name__)
        share.view_owner = owner
        return note


class TurnServer:
    """Localhost line protocol for one edit's MCP gate: `<token> acquire` -> `ok[\\t<note>]` / `err\\t<message>`,
    then `<token> release` -> `ok`. A dropped connection gives its turn back."""

    def __init__(self, shares, share, owner, cancel, timeout):
        self.shares, self.share, self.owner, self.cancel, self.timeout = shares, share, owner, cancel, timeout
        self.token = secrets.token_hex(16)
        self.stats = {"calls": 0, "waited": 0.0, "longest_wait": 0.0, "view_notes": 0, "refused": 0}
        self._stats_lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.closed = threading.Event()
        self.connections = []
        threading.Thread(target=self._accept, name=f"resolve-turns-{owner}", daemon=True).start()

    def mcp_config(self, original):
        return {"command": sys.executable,
                "args": ["-X", "utf8", "-I", str(GATE), "--port", str(self.port), "--token", self.token, "--",
                         original["command"], *original.get("args", [])]}

    def _accept(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.sock.accept()
            except OSError:
                return
            self.connections.append(connection)
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _count(self, **values):
        with self._stats_lock:
            for key, value in values.items():
                if key == "longest_wait":
                    self.stats[key] = round(max(self.stats[key], value), 2)
                else:
                    self.stats[key] = round(self.stats[key] + value, 2)

    def _serve(self, connection):
        held = None
        try:
            reader = connection.makefile("r", encoding="utf-8", newline="\n")
            for line in reader:
                token, _, action = line.strip().partition(" ")
                if not secrets.compare_digest(token, self.token):
                    return
                if action == "acquire" and held is None:
                    started = time.monotonic()
                    try:
                        held = self.shares.turn(self.share, self.owner, cancel=self.cancel, timeout=self.timeout,
                                                view=True, linger=self.shares.linger)
                        note = held.__enter__()
                    except Exception as exc:
                        held = None
                        self._count(refused=1)
                        message = "剪辑已停止，不再操作达芬奇。" if isinstance(exc, BridgeCancelled) else (str(exc) or "等待达芬奇失败。")
                        connection.sendall(("err\t" + message.replace("\n", " ") + "\n").encode("utf-8"))
                        continue
                    waited = time.monotonic() - started
                    self._count(calls=1, waited=waited, longest_wait=waited, view_notes=1 if note else 0)
                    connection.sendall(("ok\t" + note.replace("\n", " ") + "\n" if note else "ok\n").encode("utf-8"))
                elif action == "release":
                    if held is not None:
                        current, held = held, None
                        current.__exit__(None, None, None)
                    connection.sendall(b"ok\n")
                else:
                    connection.sendall("err\t排队请求无效。\n".encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            if held is not None:
                held.__exit__(None, None, None)
            try:
                connection.close()
            except OSError:
                pass

    def close(self):
        self.closed.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
