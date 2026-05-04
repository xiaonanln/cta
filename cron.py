#!/usr/bin/env python3
"""
cron.py — CLI for managing CTA cron jobs via the local web API.

Preferred over hand-editing ~/.cta/crons/<uid>:<chat_id>.json because it
avoids JSON-escape bugs and lets the server compute next_run & validate
schema.

Context (uid, chat_id) is read from env vars CTA_UID / CTA_CHAT_ID, which
agent.py injects when spawning the Claude CLI subprocess. Both can be
overridden via flags for debugging or cross-chat ops.

Commands:
  cron.py add <job_id> --schedule "0 9 * * *" --prompt "..."
  cron.py list
  cron.py remove <job_id>
  cron.py update <job_id> [--schedule ...] [--prompt ...]
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from argparse import Namespace
from typing import Any


def _base_url() -> str:
    port = 17488
    cfg = os.path.expanduser("~/.cta/config.json")
    try:
        with open(cfg) as f:
            port = int(json.load(f).get("web_port", port))
    except Exception:
        pass
    return f"http://127.0.0.1:{port}"


def _ctx(args: Namespace) -> tuple[int, int]:
    uid = args.uid if args.uid is not None else os.environ.get("CTA_UID")
    chat_id = args.chat_id if args.chat_id is not None else os.environ.get("CTA_CHAT_ID")
    if uid is None or chat_id is None:
        sys.exit("error: uid/chat_id not set — pass --uid/--chat-id or run under agent.py (which sets CTA_UID/CTA_CHAT_ID)")
    return int(uid), int(chat_id)


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        _base_url() + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code}: {e.read().decode(errors='replace').strip()}")
    except urllib.error.URLError as e:
        sys.exit(f"error: could not reach CTA at {_base_url()} ({e.reason})")


def cmd_add(args: Namespace) -> None:
    uid, chat_id = _ctx(args)
    r = _request("POST", "/cronjobs", {
        "uid": uid, "chat_id": chat_id,
        "id": args.job_id, "schedule": args.schedule, "prompt": args.prompt,
    })
    print(f"added {args.job_id} — next_run {r.get('next_run', '?')}")


def cmd_list(args: Namespace) -> None:
    uid, chat_id = _ctx(args)
    r = _request("GET", "/cronjobs")
    jobs = [j for j in r.get("jobs", []) if j["uid"] == uid and j["chat_id"] == chat_id]
    if args.json:
        print(json.dumps(jobs, indent=2))
        return
    if not jobs:
        print("(no cron jobs)")
        return
    for j in jobs:
        print(f"{j['id']:<30} {j['schedule']:<20} next: {j.get('next_run', '?')}")
        print(f"    prompt: {j.get('prompt', '')[:120]}{'…' if len(j.get('prompt','')) > 120 else ''}")


def cmd_remove(args: Namespace) -> None:
    uid, chat_id = _ctx(args)
    _request("DELETE", f"/cronjobs/{uid}/{chat_id}/{args.job_id}")
    print(f"removed {args.job_id}")


def cmd_update(args: Namespace) -> None:
    uid, chat_id = _ctx(args)
    body: dict[str, str] = {}
    if args.schedule is not None:
        body["schedule"] = args.schedule
    if args.prompt is not None:
        body["prompt"] = args.prompt
    if not body:
        sys.exit("error: update needs at least --schedule or --prompt")
    r = _request("PUT", f"/cronjobs/{uid}/{chat_id}/{args.job_id}", body)
    print(f"updated {args.job_id} — next_run {r.get('next_run', '?')}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cron.py", description="Manage CTA cron jobs")
    p.add_argument("--uid", type=int, help="user id (default: $CTA_UID)")
    p.add_argument("--chat-id", type=int, help="chat id (default: $CTA_CHAT_ID)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a cron job")
    a.add_argument("job_id")
    a.add_argument("--schedule", required=True, help='cron syntax, e.g. "0 9 * * *"')
    a.add_argument("--prompt", required=True)
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list cron jobs for this chat")
    l.add_argument("--json", action="store_true", help="raw JSON output")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("remove", help="remove a cron job by id")
    r.add_argument("job_id")
    r.set_defaults(func=cmd_remove)

    u = sub.add_parser("update", help="update schedule and/or prompt by id")
    u.add_argument("job_id")
    u.add_argument("--schedule")
    u.add_argument("--prompt")
    u.set_defaults(func=cmd_update)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
