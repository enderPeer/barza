#!/usr/bin/env python3
"""barza liveness: probe the address book, republish host.json + status.json.

Runs on GitHub's machines (free) every 15 minutes via .github/workflows/
liveness.yml. It never invents a host and never touches the message record;
when nothing answers it clears the url on purpose, so the site falls
straight to the published archive instead of waiting on a dead address.
Dead addresses are kept in `candidates` so an all-down day cannot erase
the way back.

Stdlib only. Run locally with:  python .github/liveness.py
"""
import concurrent.futures
import json
import re
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 8


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean(u) -> str:
    return (u or "").strip().rstrip("/")


def is_http(u) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", u or ""))


def probe(url: str) -> dict:
    out = {"url": url, "live": False, "seq": None, "messages": None, "error": None}
    try:
        req = urllib.request.Request(
            url + "/api/health", headers={"user-agent": "barza-liveness/1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
        out["live"] = bool(d.get("ok"))
        out["seq"] = d.get("seq")
        out["messages"] = d.get("messages")
    except Exception as e:  # noqa: BLE001 - a dead host is a normal answer here
        out["error"] = str(e)[:200]
    return out


def main() -> None:
    with open("host.json", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    candidates = []
    for u in [*(cfg.get("urls") or []), cfg.get("url"), *(cfg.get("candidates") or [])]:
        u = clean(u)
        if is_http(u) and u not in candidates:
            candidates.append(u)

    results = []
    if candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(probe, candidates))
    for r in results:
        print(("LIVE " if r["live"] else "down ") + r["url"]
              + (f"  seq {r['seq']}" if r["live"] else f"  {r['error']}"))

    live = sorted((r for r in results if r["live"]),
                  key=lambda r: -(r["seq"] or 0))

    next_cfg = dict(cfg)
    next_cfg["url"] = live[0]["url"] if live else ""
    if len(live) > 1:
        next_cfg["urls"] = [r["url"] for r in live]
    else:
        next_cfg.pop("urls", None)
    next_cfg["candidates"] = candidates
    next_cfg["checked"] = now()
    next_cfg["checkedBy"] = "liveness workflow (.github/workflows/liveness.yml)"
    if live:
        next_cfg.pop("note", None)
    else:
        next_cfg["note"] = ("No published host answered at the last check, so this "
                            "file names none: the site reads the published archive "
                            "(data/messages.json) instead of waiting on a dead "
                            "address. Addresses are kept in `candidates`.")

    status = {
        "checked": next_cfg["checked"],
        "anyLive": bool(live),
        "host": live[0]["url"] if live else None,
        "hosts": results,
        "note": ("Written every 15 minutes by a scheduled GitHub Action. It probes "
                 "the hosts named in host.json and repoints that file; it never "
                 "touches the message record."),
    }

    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open("host.json", "w", encoding="utf-8") as f:
        json.dump(next_cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("host.json -> " + (live[0]["url"] if live
          else "(none: the site reads the published archive)"))


if __name__ == "__main__":
    main()
