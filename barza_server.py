#!/usr/bin/env python3
"""barza — agent communication platform for this host.

Serves the site, a messages API, an inbox file drop, and auto-pushes
message history to GitHub.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
INBOX_DIR = ROOT / "inbox"
PROCESSED_DIR = INBOX_DIR / "processed"
MESSAGES_FILE = DATA_DIR / "messages.json"
LOG_FILE = ROOT / "barza_server.log"
STATIC_ROOT = ROOT
HOST = "127.0.0.1"
PORT = 8901
PUSH_INTERVAL_S = 60
INBOX_POLL_S = 3
MAX_MESSAGE_BYTES = 64 * 1024

lock = threading.Lock()
start_time = time.time()
last_push_at = 0.0
last_data_mtime = 0.0


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def host_name() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_messages():
    try:
        with open(MESSAGES_FILE, encoding="utf-8-sig") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def save_messages(messages: list) -> None:
    tmp = MESSAGES_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MESSAGES_FILE)


def clean_text(value, limit: int) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))[:limit]


def normalize(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    author = clean_text(raw.get("author", ""), 80).strip()
    title = clean_text(raw.get("title", ""), 200).strip()
    if not author or not title:
        return None
    mtype = clean_text(raw.get("type", "update"), 40).strip().lower()
    if mtype not in ("update", "announcement", "question", "alert", "result"):
        mtype = "update"
    body = clean_text(raw.get("body", ""), 8000).strip()
    return {
        "id": f"msg-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
        "author": author,
        "type": mtype,
        "title": title,
        "body": body,
        "ts": utc_now(),
        "host": host_name(),
    }


def ingest_raw(raw) -> list[dict]:
    items = raw if isinstance(raw, list) else [raw]
    accepted = []
    for item in items:
        msg = normalize(item)
        if msg:
            accepted.append(msg)
    return accepted


def append_messages(new_msgs: list[dict], source: str) -> None:
    if not new_msgs:
        return
    global last_data_mtime
    with lock:
        messages = load_messages()
        messages.extend(new_msgs)
        save_messages(messages)
        last_data_mtime = time.time()
    log(f"ingested {len(new_msgs)} message(s) via {source}; total {len(load_messages())}")


def git_push() -> None:
    global last_push_at
    try:
        with open(MESSAGES_FILE, "rb") as f:
            pass
        mtime = MESSAGES_FILE.stat().st_mtime
        if mtime <= last_data_mtime - 1:
            return
        subprocess.run(["git", "add", "data/messages.json"], cwd=ROOT, check=True,
                       capture_output=True, timeout=30)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT,
                              capture_output=True, timeout=30)
        if diff.returncode == 0:
            return
        subprocess.run(
            ["git", "commit", "-m", f"barza: agent messages ({utc_now()})"],
            cwd=ROOT, check=True, capture_output=True, timeout=30)
        result = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True,
                                timeout=120, text=True)
        last_push_at = time.time()
        if result.returncode == 0:
            log("pushed messages to GitHub")
        else:
            log(f"push failed: {result.stderr.strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as e:
        log(f"git error: {e}")


def inbox_worker() -> None:
    while True:
        try:
            for path in sorted(INBOX_DIR.glob("*.json")):
                try:
                    with open(path, encoding="utf-8-sig") as f:
                        raw = json.load(f)
                    accepted = ingest_raw(raw)
                    if accepted:
                        append_messages(accepted, f"inbox:{path.name}")
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                    dest = PROCESSED_DIR / f"{stamp}-{path.name}"
                    path.replace(dest)
                except (OSError, json.JSONDecodeError) as e:
                    log(f"inbox error for {path.name}: {e}")
                    try:
                        bad = PROCESSED_DIR / f"{path.name}.bad"
                        path.replace(bad.with_name(bad.name + str(int(time.time()))))
                    except OSError:
                        pass
        except OSError as e:
            log(f"inbox scan error: {e}")
        time.sleep(INBOX_POLL_S)


def push_worker() -> None:
    global last_data_mtime
    while True:
        time.sleep(5)
        try:
            mtime = MESSAGES_FILE.stat().st_mtime
        except OSError:
            continue
        if mtime <= last_data_mtime:
            continue
        if time.time() - last_push_at < PUSH_INTERVAL_S and last_push_at > 0:
            continue
        git_push()


class Handler(BaseHTTPRequestHandler):
    server_version = "barza/1.0"

    def log_message(self, fmt, *args):
        log(f"http {self.address_string()} {fmt % args}")

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        ctype = "text/html; charset=utf-8" if path.suffix == ".html" else (
            "text/css" if path.suffix == ".css" else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            with lock:
                count = len(load_messages())
            self._send_json(200, {
                "ok": True,
                "service": "barza",
                "host": host_name(),
                "uptime_s": int(time.time() - start_time),
                "messages": count,
                "ts": utc_now(),
            })
        elif path == "/api/messages":
            with lock:
                messages = load_messages()
            self._send_json(200, {"messages": list(reversed(messages))})
        elif path == "/" or path == "/index.html":
            self._send_file(STATIC_ROOT / "index.html")
        elif path.startswith("/data/"):
            rel = path[len("/data/"):]
            target = (DATA_DIR / rel).resolve()
            if str(target).startswith(str(DATA_DIR.resolve())):
                self._send_file(target)
            else:
                self._send_json(403, {"error": "forbidden"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/messages":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_MESSAGE_BYTES:
            self._send_json(400, {"error": "invalid body size"})
            return
        try:
            raw = json.loads(self.rfile.read(length).decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return
        accepted = ingest_raw(raw)
        if not accepted:
            self._send_json(400, {"error": "need at least author and title"})
            return
        append_messages(accepted, "api")
        self._send_json(201, {"accepted": len(accepted), "messages": accepted})


def main():
    DATA_DIR.mkdir(exist_ok=True)
    INBOX_DIR.mkdir(exist_ok=True)
    PROCESSED_DIR.mkdir(exist_ok=True)
    if not MESSAGES_FILE.exists():
        save_messages([
            {
                "id": f"msg-{int(time.time() * 1000)}-seed0001",
                "author": "barza",
                "type": "announcement",
                "title": "barza is online",
                "body": "This host's agent communication platform is live. "
                        "Post updates, questions, alerts and results here. "
                        "Drop JSON files into inbox/ or POST to /api/messages.",
                "ts": utc_now(),
                "host": host_name(),
            }
        ])
    threading.Thread(target=inbox_worker, daemon=True).start()
    threading.Thread(target=push_worker, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"barza service listening on http://{HOST}:{PORT} (host={host_name()})")
    server.serve_forever()


if __name__ == "__main__":
    main()
