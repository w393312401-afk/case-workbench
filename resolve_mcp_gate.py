"""Stdio MCP proxy in front of DaVinci's ResolveMCP.exe for one AI edit.

Every scripting call first waits for this edit's turn at the workbench (resolve_share.TurnServer), runs, and gives the
turn back when its answer arrives; documentation lookups pass straight through. Everything else is forwarded as is.
Standard library only: it runs with -I as a child of the AI client.

    python -I resolve_mcp_gate.py --port N --token T -- ResolveMCP.exe [args...]
"""
import json
import os
import socket
import subprocess
import sys
import threading

UNGATED = {"search_scripting_api", "get_scripting_api", "get_scripting_docs", "get_whats_new"}


class Turns:
    def __init__(self, port, token):
        self.port, self.token, self.sock, self.reader = port, token, None, None

    def _send(self, action):
        if self.sock is None:
            self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=None)
            self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.sock.sendall(f"{self.token} {action}\n".encode("utf-8"))
        line = self.reader.readline()
        if not line:
            raise OSError("workbench closed the connection")
        status, _, text = line.rstrip("\n").partition("\t")
        return status == "ok", text

    def acquire(self):
        try:
            return self._send("acquire")
        except OSError:
            self.sock = None
            return False, "工作台的达芬奇排队服务已断开，本次调用没有执行；剪辑任务可能已结束。"

    def release(self):
        try:
            self._send("release")
        except OSError:
            self.sock = None


def main(argv):
    try:
        split = argv.index("--")
        options, command = argv[:split], argv[split + 1:]
        port, token = int(options[options.index("--port") + 1]), options[options.index("--token") + 1]
    except (ValueError, IndexError):
        sys.stderr.write("usage: resolve_mcp_gate.py --port N --token T -- command [args]\n")
        return 2
    if not command:
        return 2
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    turns, out_lock, state = Turns(port, token), threading.Lock(), threading.Lock()
    gated, notes = set(), {}

    def write(raw):
        with out_lock:
            sys.stdout.buffer.write(raw if raw.endswith(b"\n") else raw + b"\n")
            sys.stdout.buffer.flush()

    def answers():
        for raw in iter(child.stdout.readline, b""):
            try:
                message = json.loads(raw)
            except ValueError:
                message = None
            key = json.dumps(message.get("id")) if isinstance(message, dict) and "method" not in message and "id" in message else None
            if key is not None and key in notes:
                note = notes.pop(key)
                content = (message.get("result") or {}).get("content")
                if isinstance(content, list):
                    content.insert(0, {"type": "text", "text": note})
                    raw = json.dumps(message, ensure_ascii=False).encode("utf-8")
            write(raw)
            if key is not None:
                with state:
                    if key in gated:
                        gated.discard(key)
                        if not gated:
                            turns.release()

    reply = threading.Thread(target=answers, daemon=True)
    reply.start()
    try:
        for raw in iter(sys.stdin.buffer.readline, b""):
            try:
                message = json.loads(raw)
            except ValueError:
                message = None
            if (isinstance(message, dict) and message.get("method") == "tools/call" and "id" in message
                    and (message.get("params") or {}).get("name") not in UNGATED):
                key = json.dumps(message["id"])
                with state:
                    if not gated:
                        ok, text = turns.acquire()
                        if not ok:
                            write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
                                "content": [{"type": "text", "text": text or "达芬奇暂时不可用，本次调用没有执行。"}],
                                "isError": True}}, ensure_ascii=False).encode("utf-8"))
                            continue
                        if text:
                            notes[key] = text
                    gated.add(key)
            try:
                child.stdin.write(raw if raw.endswith(b"\n") else raw + b"\n")
                child.stdin.flush()
            except OSError:
                break
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(5)
        except subprocess.TimeoutExpired:
            child.kill()
        reply.join(2)
        with state:
            if gated:
                turns.release()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
