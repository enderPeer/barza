#!/usr/bin/env python3
"""barza relay — keeps this host's local barza addresses alive now that the
service itself runs on another machine (knecht24, see linux/).

Two jobs, both stdlib:

  1. A reverse proxy on 127.0.0.1:8901 that forwards every request to the
     upstream barza service (default http://192.168.178.200:8901), so agents
     on this host that still speak to 127.0.0.1:8901 keep working unchanged.
     Status, ETag, 304s and error codes pass through untouched; an upstream
     that does not answer becomes 502 {"code": "upstream-down"}.
  2. The inbox forwarder: JSON files dropped into inbox/ are POSTed upstream
     and moved to inbox/processed/, exactly as the local service used to do.
     A file the upstream refuses is parked as .bad-<ts>; a file it could not
     be reached for stays put and is retried.

Configuration (environment, optional):
  BARZA_UPSTREAM   default http://192.168.178.200:8901
  BARZA_BIND       default 127.0.0.1
  BARZA_PORT       default 8901
"""
import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INBOX_DIR = ROOT / "inbox"
PROCESSED_DIR = INBOX_DIR / "processed"
LOG_FILE = ROOT / "barza_relay.log"
UPSTREAM = os.environ.get("BARZA_UPSTREAM", "http://192.168.178.200:8901").rstrip("/")
BIND = os.environ.get("BARZA_BIND", "127.0.0.1")
PORT = int(os.environ.get("BARZA_PORT", "8901"))
TIMEOUT_S = 10
INBOX_TIMEOUT_S = 30
INBOX_POLL_S = 3
INBOX_BACKOFF_S = 15
VERSION = "1.0"
# Everything a forwarded request can raise on the wire. http.client's own
# exceptions are not OSErrors; an uncaught one would kill the inbox thread.
TRANSPORT_ERRORS = (OSError, ValueError, http.client.HTTPException)
FORWARD_REQ_HEADERS = ("content-type", "if-none-match", "accept")
FORWARD_RESP_HEADERS = ("content-type", "etag", "access-control-allow-origin",
                        "access-control-allow-methods", "access-control-allow-headers")

start_time = time.time()
stats = {"relayed": 0, "upstream_errors": 0, "inbox_forwarded": 0, "inbox_parked": 0}


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


def upstream_request(method: str, path: str, headers: dict | None = None, body: bytes | None = None,
                     timeout: float = TIMEOUT_S):
    """Forward one request. Returns (status, headers, body).

    Upstream answers that are not 2xx (304, 4xx, 5xx) are still answers and
    are returned as such; only a transport failure raises (TRANSPORT_ERRORS).
    """
    req = urllib.request.Request(UPSTREAM + path, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, list(r.getheaders()), r.read()
    except urllib.error.HTTPError as e:
        data = b""
        try:
            data = e.read()
        except (OSError, ValueError):
            pass
        return e.code, list(e.headers.items()), data


def park(path: Path, bad: bool = False) -> None:
    try:
        if bad:
            path.replace(PROCESSED_DIR / f"{path.name}.bad-{int(time.time())}")
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            path.replace(PROCESSED_DIR / f"{stamp}-{path.name}")
    except OSError as e:
        log(f"inbox: could not move {path.name}: {e}")


def inbox_worker() -> None:
    while True:
        delay = INBOX_POLL_S
        try:
            for path in sorted(INBOX_DIR.glob("*.json")):
                try:
                    raw = path.read_bytes()
                    json.loads(raw.decode("utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
                    log(f"inbox: {path.name} is not JSON, parking it: {e}")
                    park(path, bad=True)
                    stats["inbox_parked"] += 1
                    continue
                try:
                    status, _, data = upstream_request(
                        "POST", "/api/messages", {"Content-Type": "application/json"}, raw,
                        timeout=INBOX_TIMEOUT_S)
                except TRANSPORT_ERRORS as e:
                    log(f"inbox: upstream unreachable, keeping {path.name} for retry: {e}")
                    delay = INBOX_BACKOFF_S
                    break
                except Exception as e:  # noqa: BLE001 - never let the forwarder thread die
                    log(f"inbox: unexpected error for {path.name}, parking it: {e!r}")
                    park(path, bad=True)
                    stats["inbox_parked"] += 1
                    continue
                if status == 201:
                    log(f"inbox: forwarded {path.name}: {data[:100].decode('utf-8', 'replace')}")
                    park(path)
                    stats["inbox_forwarded"] += 1
                else:
                    log(f"inbox: upstream refused {path.name} ({status}), parking it: "
                        f"{data[:120].decode('utf-8', 'replace')}")
                    park(path, bad=True)
                    stats["inbox_parked"] += 1
        except Exception as e:  # noqa: BLE001 - never let the forwarder thread die
            log(f"inbox scan error: {e!r}")
        time.sleep(delay)


class Handler(BaseHTTPRequestHandler):
    server_version = f"barza-relay/{VERSION}"

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

    def _relay(self, method: str) -> None:
        path = self.path
        if path.partition("?")[0] == "/api/relay":
            self._send_json(200, {
                "ok": True, "service": "barza-relay", "version": VERSION,
                "host": host_name(), "upstream": UPSTREAM,
                "uptime_s": int(time.time() - start_time), **stats,
            })
            return
        body = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            body = self.rfile.read(length)
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in FORWARD_REQ_HEADERS:
                headers[k] = v
        try:
            status, resp_headers, data = upstream_request(method, path, headers, body)
        except TRANSPORT_ERRORS as e:
            stats["upstream_errors"] += 1
            log(f"upstream {UPSTREAM} unreachable: {e}")
            self._send_json(502, {
                "code": "upstream-down",
                "error": f"the barza service at {UPSTREAM} is not answering",
                "upstream": UPSTREAM,
            })
            return
        stats["relayed"] += 1
        self.send_response(status)
        for k, v in resp_headers:
            if k.lower() in FORWARD_RESP_HEADERS:
                self.send_header(k, v)
        self.send_header("X-Barza-Relay", f"{host_name()} -> {UPSTREAM}")
        has_body = status not in (204, 304) and method != "HEAD"
        if has_body:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if has_body and data:
            self.wfile.write(data)

    def do_GET(self):
        self._relay("GET")

    def do_HEAD(self):
        self._relay("HEAD")

    def do_POST(self):
        self._relay("POST")

    def do_OPTIONS(self):
        self._relay("OPTIONS")


def main():
    INBOX_DIR.mkdir(exist_ok=True)
    PROCESSED_DIR.mkdir(exist_ok=True)
    threading.Thread(target=inbox_worker, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log(f"barza relay v{VERSION} listening on http://{BIND}:{PORT} -> {UPSTREAM} "
        f"(host={host_name()}); inbox {INBOX_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
