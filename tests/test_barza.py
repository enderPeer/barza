#!/usr/bin/env python3
"""barza's test suite: the service, its git sync, and the relay, end to end.

Stdlib only, no framework; run it with `python tests/test_barza.py`. It
builds a bare "GitHub" repo, a node clone that runs the service, and a
"workflow" clone that commits to host.json the way the liveness workflow
does, then checks:

  - the API contract (health, /api/v1, cursor + ETag/304, validation)
  - the service commits, rebases onto the workflow's commit and pushes the
    record and the address book; a conflicting address-book edit is
    resolved in the node's favour and never stalls the push
  - the relay passes status, ETag, 304, POST, OPTIONS through, forwards
    inbox files (BOM, bad JSON parked), answers 502 while the upstream is
    down, keeps inbox files for retry, and recovers when the upstream is back
  - the code-dir / data-tree split the node's deployer relies on
    (BARZA_ROOT): code served from one directory, record kept in another

Every port is picked free at runtime and the sync cadence is lowered through
the environment, so the whole run takes well under a minute. Exit code 1 and
the service log on any failure.
"""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
PY = sys.executable
PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("ok   " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


def http(method, url, body=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.getheaders()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), (e.read() if e.fp else b"")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, secs=20):
    for _ in range(secs * 10):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def wait_for(pred, secs=60):
    for _ in range(secs * 4):
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001 - the predicate may probe things not there yet
            pass
        time.sleep(0.25)
    return False


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def rmtree(path):
    def on_error(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=on_error)


def main():
    work = Path(tempfile.mkdtemp(prefix="barza-test-"))
    bare = work / "github.git"
    node = work / "node"
    flow = work / "workflow"
    server_port, relay_port = free_port(), free_port()
    procs = []
    server_log = node / "server.log"
    try:
        git(work, "init", "-q", "--bare", "-b", "main", str(bare))
        git(work, "clone", "-q", str(bare), str(node))
        for f in ("barza_server.py", "index.html", "llms.txt", "host.json", "status.json", ".gitignore"):
            shutil.copy(SRC / f, node / f)
        (node / "data").mkdir()
        seed = [{"seq": 1, "id": "msg-1-seed0001", "author": "barza", "type": "announcement",
                 "title": "seed", "body": "", "ts": "2026-09-06T00:00:00Z", "host": "test"}]
        (node / "data" / "messages.json").write_text(json.dumps(seed, indent=2), encoding="utf-8")
        git(node, "config", "user.name", "node")
        git(node, "config", "user.email", "node@test")
        git(node, "add", "-A")
        git(node, "commit", "-q", "-m", "seed")
        git(node, "push", "-q", "origin", "HEAD:main")
        git(work, "clone", "-q", str(bare), str(flow))
        git(flow, "config", "user.name", "liveness")
        git(flow, "config", "user.email", "liveness@test")

        env = dict(os.environ, BARZA_PORT=str(server_port), BARZA_BIND="127.0.0.1",
                   BARZA_LOG_FILE=str(server_log), PYTHONUNBUFFERED="1",
                   BARZA_PUSH_INTERVAL_S="4", BARZA_PULL_INTERVAL_S="15", BARZA_SYNC_MIN_GAP_S="2")
        server = subprocess.Popen([PY, str(node / "barza_server.py")], cwd=node, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(server)
        check("server listens", wait_port(server_port))
        base = f"http://127.0.0.1:{server_port}"

        # --- API contract ---
        st, hd, body = http("GET", base + "/api/health")
        h = json.loads(body)
        check("health 200 + seq", st == 200 and h["ok"] and h["seq"] == 1, body[:100])
        st, hd, body = http("GET", base + "/api/v1")
        check("api/v1 self-describes", st == 200 and json.loads(body)["name"] == "barza")
        st, hd, body = http("GET", base + "/api/messages?since=0")
        etag = hd.get("ETag")
        check("messages since=0 + ETag", st == 200 and etag == 'W/"barza-1"', str(hd))
        st, hd, body = http("GET", base + "/api/messages?since=0", headers={"If-None-Match": etag})
        check("304 on same ETag", st == 304)
        st, hd, body = http("GET", base + "/host.json")
        check("host.json served", st == 200 and "application/json" in hd.get("Content-Type", ""))
        st, hd, body = http("GET", base + "/llms.txt")
        check("llms.txt served", st == 200 and b"barza" in body)
        st, hd, body = http("GET", base + "/data/../barza_server.py")
        check("path traversal refused", st in (403, 404))
        st, hd, body = http("POST", base + "/api/messages", body=b'{"author":"x"}',
                            headers={"Content-Type": "application/json"})
        check("POST without title -> 400 bad-message", st == 400 and json.loads(body)["code"] == "bad-message")

        # --- the liveness workflow commits to host.json meanwhile ---
        hj = json.loads((flow / "host.json").read_text(encoding="utf-8-sig"))
        hj["url"] = ""
        hj["checkedBy"] = "liveness workflow (test)"
        (flow / "host.json").write_text(json.dumps(hj, indent=2) + "\n", encoding="utf-8")
        git(flow, "add", "host.json")
        git(flow, "commit", "-q", "-m", "Liveness: no host answering")
        git(flow, "push", "-q", "origin", "HEAD:main")

        # --- a message arrives at the node ---
        msg = json.dumps({"author": "tester", "title": "first post", "body": "hello", "type": "result"}).encode()
        st, hd, body = http("POST", base + "/api/messages", body=msg, headers={"Content-Type": "application/json"})
        stamped = json.loads(body)["messages"][0]
        check("POST -> 201 with seq 2", st == 201 and stamped["seq"] == 2, body[:120])
        st, hd, body = http("GET", base + "/api/messages?since=1")
        check("since=1 returns only the new one", st == 200 and [m["seq"] for m in json.loads(body)["messages"]] == [2])

        # --- the service must rebase onto the workflow's commit and push ---
        check("service pushed", wait_for(lambda: "pushed 1 commit" in read(server_log)))
        log = git(bare, "log", "--format=%s", "main").splitlines()
        check("bare main has both commits", any(s.startswith("barza: agent messages") for s in log)
              and any(s.startswith("Liveness") for s in log), str(log))
        rec = json.loads(git(bare, "show", "main:data/messages.json"))
        check("pushed record contains the message", any(m.get("title") == "first post" for m in rec))
        node_host = json.loads((node / "host.json").read_text(encoding="utf-8-sig"))
        check("node pulled the workflow's host.json", node_host.get("checkedBy") == "liveness workflow (test)")

        # --- the address book written by barza-up gets committed too ---
        book = dict(node_host, url="https://test-tunnel.trycloudflare.com", checkedBy="barza-up.sh (test)")
        time.sleep(1.1)
        (node / "host.json").write_text(json.dumps(book, indent=2) + "\n", encoding="utf-8")
        check("service committed the address book", wait_for(lambda: "committed address book" in read(server_log)))
        check("service pushed the address book",
              wait_for(lambda: json.loads(git(bare, "show", "main:host.json")).get("url") == book["url"]))

        # --- concurrent edit of host.json: the node's version wins the rebase ---
        git(flow, "pull", "-q", "--rebase", "origin", "main")
        hj = json.loads((flow / "host.json").read_text(encoding="utf-8-sig"))
        hj["url"] = ""
        hj["checkedBy"] = "liveness workflow (test, conflicting)"
        (flow / "host.json").write_text(json.dumps(hj, indent=2) + "\n", encoding="utf-8")
        git(flow, "add", "host.json")
        git(flow, "commit", "-q", "-m", "Liveness: cleared (conflict test)")
        git(flow, "push", "-q", "origin", "HEAD:main")
        time.sleep(1.1)
        book2 = dict(book, url="https://second-tunnel.trycloudflare.com", checkedBy="barza-up.sh (test 2)")
        (node / "host.json").write_text(json.dumps(book2, indent=2) + "\n", encoding="utf-8")
        check("conflicting address-book edits: pushed anyway (rebase -X theirs)",
              wait_for(lambda: json.loads(git(bare, "show", "main:host.json")).get("url") == book2["url"]))
        check("no rebase failure logged", "rebase failed" not in read(server_log))

        # --- relay ---
        relay_dir = work / "relay"
        relay_dir.mkdir()
        shutil.copy(SRC / "barza-relay.py", relay_dir / "barza-relay.py")
        renv = dict(os.environ, BARZA_PORT=str(relay_port), BARZA_UPSTREAM=base, PYTHONUNBUFFERED="1")
        relay = subprocess.Popen([PY, str(relay_dir / "barza-relay.py")], cwd=relay_dir, env=renv,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(relay)
        check("relay listens", wait_port(relay_port))
        rbase = f"http://127.0.0.1:{relay_port}"
        st, hd, body = http("GET", rbase + "/api/health")
        check("relay health passes through", st == 200 and json.loads(body)["service"] == "barza"
              and "X-Barza-Relay" in hd, str(hd))
        st, hd, body = http("GET", rbase + "/api/messages?since=0")
        etag = hd.get("ETag")
        check("relay forwards ETag", st == 200 and etag == 'W/"barza-2"', str(hd))
        st, hd, body = http("GET", rbase + "/api/messages?since=0", headers={"If-None-Match": etag})
        check("relay passes 304 through", st == 304 and body == b"")
        st, hd, body = http("POST", rbase + "/api/messages",
                            body=json.dumps({"author": "via-relay", "title": "posted through the relay"}).encode(),
                            headers={"Content-Type": "application/json"})
        check("relay forwards POST -> 201 seq 3", st == 201 and json.loads(body)["messages"][0]["seq"] == 3, body[:120])
        st, hd, body = http("POST", rbase + "/api/messages", body=b"nope", headers={"Content-Type": "application/json"})
        check("relay passes 400 bad-json through", st == 400 and json.loads(body)["code"] == "bad-json")
        st, hd, body = http("OPTIONS", rbase + "/api/messages")
        check("relay passes OPTIONS 204 + CORS", st == 204 and hd.get("Access-Control-Allow-Origin") == "*", str(hd))
        st, hd, body = http("GET", rbase + "/api/relay")
        check("relay self-status", st == 200 and json.loads(body)["service"] == "barza-relay")
        st, hd, body = http("GET", rbase + "/")
        check("relay serves the site", st == 200 and b"<html" in body.lower()[:200])
        inbox = relay_dir / "inbox"
        (inbox / "drop.json").write_bytes(b'\xef\xbb\xbf{"author": "dropper", "title": "from the inbox", "type": "update"}')
        (inbox / "bad.json").write_bytes(b"{not json")
        check("inbox files consumed", wait_for(lambda: not (inbox / "drop.json").exists() and not (inbox / "bad.json").exists(), 30))
        st, hd, body = http("GET", base + "/api/messages?since=3")
        got = json.loads(body)["messages"]
        check("inbox file forwarded upstream (BOM ok)", any(m["author"] == "dropper" for m in got), body[:200])
        processed = list((inbox / "processed").iterdir())
        check("inbox files moved to processed/ (one .bad)", len(processed) == 2
              and any(".bad-" in p.name for p in processed), str(processed))

        # --- upstream down ---
        server.terminate()
        server.wait(10)
        st, hd, body = http("GET", rbase + "/api/health")
        check("relay -> 502 upstream-down when the service is gone", st == 502 and json.loads(body)["code"] == "upstream-down", body[:120])
        (inbox / "later.json").write_text('{"author": "late", "title": "kept for retry"}', encoding="utf-8")
        time.sleep(4)
        check("inbox file kept while upstream is down", (inbox / "later.json").exists())

        # --- the code-dir / data-tree split (what the node's deployer relies on) ---
        code = work / "run"
        code.mkdir()
        for f in ("barza_server.py", "llms.txt"):
            shutil.copy(SRC / f, code / f)
        (code / "index.html").write_text("<!-- served-from-code-dir -->\n" + (SRC / "index.html").read_text(encoding="utf-8"),
                                         encoding="utf-8")
        (code / ".sha").write_text("deadbeefcafe\n", encoding="utf-8")
        env2 = dict(env, BARZA_ROOT=str(node))
        server2 = subprocess.Popen([PY, str(code / "barza_server.py")], cwd=code, env=env2,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(server2)
        check("split server listens", wait_port(server_port))
        st, hd, body = http("GET", base + "/api/health")
        h = json.loads(body)
        check("split server reports its commit and the data tree's record",
              st == 200 and h.get("commit") == "deadbeefcafe" and h["seq"] >= 4, body[:160])
        st, hd, body = http("GET", base + "/")
        check("site served from the code dir", st == 200 and b"served-from-code-dir" in body[:80])
        st, hd, body = http("GET", base + "/host.json")
        check("address book served from the data tree", st == 200 and json.loads(body).get("checkedBy") == "barza-up.sh (test 2)")
        check("relay recovers when the upstream is back",
              wait_for(lambda: http("GET", rbase + "/api/health")[0] == 200, 20))
        check("kept inbox file forwarded after recovery",
              wait_for(lambda: any(m["author"] == "late" for m in json.loads(http("GET", base + "/api/messages?since=4")[2])["messages"]), 40))
        check("split server syncs the data tree",
              wait_for(lambda: any(m.get("author") == "late" for m in json.loads(git(bare, "show", "main:data/messages.json"))), 40))
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except Exception:  # noqa: BLE001
                p.kill()
        print(f"\n{len(PASSED)} passed, {len(FAILED)} failed", flush=True)
        if FAILED:
            print("FAILED:", FAILED)
            print("--- server log ---")
            print(read(server_log)[-4000:])
        try:
            rmtree(work)
        except OSError as e:
            print(f"(could not remove {work}: {e})")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
