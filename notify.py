#!/usr/bin/env python3
"""
notify.py — CLI for sending messages between CTA chats (cross-agent comms).

Reuses the existing POST /chat/<uid>/<chat_id>/send endpoint. No new server
infra; this is a thin convenience wrapper so agents can address each other
without remembering numeric IDs.

Addressing modes (exact match only — no fuzzy):
  notify.py send --to "<label>" "<message>"      — find chat by exact label
  notify.py send --uid <U> --chat-id <C> "<msg>"  — explicit IDs
  notify.py list                                  — list all chats with labels

Example:
  notify.py send --to "CTA" "build broke on main, please look"
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


def _resolve_chat(args: Namespace) -> tuple[int, int]:
    """Return (uid, chat_id) from either --uid + --chat-id or --to <label>.

    Exact label match — fails loudly on 0 or >1 matches so addressing is
    deterministic.
    """
    if args.uid is not None and args.chat_id is not None:
        return args.uid, args.chat_id
    if args.to:
        sessions = _request("GET", "/chats").get("chats", [])
        matches = [s for s in sessions if s.get("label") == args.to]
        if not matches:
            sys.exit(f"error: no chat with label {args.to!r}. Try `notify.py list`.")
        if len(matches) > 1:
            ids = ", ".join(f"{s['uid']}:{s['chat_id']}" for s in matches)
            sys.exit(f"error: multiple chats with label {args.to!r} ({ids}). "
                     f"Use --uid + --chat-id to disambiguate.")
        return matches[0]["uid"], matches[0]["chat_id"]
    sys.exit("error: pass --to <label> or --uid + --chat-id")


def cmd_send(args: Namespace) -> None:
    uid, chat_id = _resolve_chat(args)
    _request("POST", f"/chat/{uid}/{chat_id}/send", {"text": args.message})
    print(f"sent to {uid}:{chat_id}")


def cmd_list(args: Namespace) -> None:
    sessions = _request("GET", "/chats").get("chats", [])
    if not sessions:
        print("(no chats)")
        return
    for s in sessions:
        label = s.get("label", "?")
        print(f"{label:<30} uid={s['uid']} chat_id={s['chat_id']}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="notify.py",
                                description="Send a message to another CTA chat.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="send a message to another chat")
    s.add_argument("message", help="message text to deliver")
    s.add_argument("--to", help="recipient chat label (exact match)")
    s.add_argument("--uid", type=int, help="recipient user id (use with --chat-id)")
    s.add_argument("--chat-id", type=int, help="recipient chat id (use with --uid)")
    s.set_defaults(func=cmd_send)

    l = sub.add_parser("list", help="list all known chats with labels")
    l.set_defaults(func=cmd_list)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
