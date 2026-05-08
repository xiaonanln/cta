#!/usr/bin/env python3
"""
send_image.py — CLI for sending an image to the current CTA chat.

Reads CTA_UID / CTA_CHAT_ID from the environment (injected by agent.py into
every Claude subprocess) and POSTs the image to the local web API.

Usage:
  send_image.py <file_path> [caption]

Examples:
  send_image.py /tmp/chart.png
  send_image.py /tmp/chart.png "Weight trend — last 30 days"
"""

import json
import os
import sys
import urllib.error
import urllib.request


def _base_url() -> str:
    port = 17488
    cfg = os.path.expanduser("~/.cta/config.json")
    try:
        with open(cfg) as f:
            port = int(json.load(f).get("web_port", port))
    except Exception:
        pass
    return f"http://127.0.0.1:{port}"


def main(argv: list[str] | None = None) -> None:
    args = (argv if argv is not None else sys.argv[1:])
    if not args:
        sys.exit("usage: send_image.py <file_path> [caption]")

    file_path = args[0]
    caption = args[1] if len(args) > 1 else None

    uid = os.environ.get("CTA_UID")
    chat_id = os.environ.get("CTA_CHAT_ID")
    if not uid or not chat_id:
        sys.exit("error: CTA_UID / CTA_CHAT_ID not set — run inside a CTA Claude session")

    body: dict = {"file_path": os.path.abspath(file_path)}
    if caption:
        body["caption"] = caption

    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{_base_url()}/chat/{uid}/{chat_id}/send_photo",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read() or b"{}")
            if result.get("ok"):
                print(f"sent {file_path}")
            else:
                sys.exit(f"error: {result}")
    except urllib.error.HTTPError as e:
        sys.exit(f"error: HTTP {e.code}: {e.read().decode(errors='replace').strip()}")
    except urllib.error.URLError as e:
        sys.exit(f"error: could not reach CTA at {_base_url()} ({e.reason})")


if __name__ == "__main__":
    main()
