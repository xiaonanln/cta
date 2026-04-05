#!/usr/bin/env python3
"""
CTA — Claude Telegram Agent.
Telegram bot powered by Claude Code CLI.
Uses Max subscription — no API tokens needed.
"""

import argparse
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import telebot
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

# ── TUI ───────────────────────────────────────────────────────────────────────

_log_entries: deque[tuple[str, str]] = deque(maxlen=200)
_tui_lock = threading.Lock()


def tui_log(text: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _tui_lock:
        _log_entries.append((ts, text))


class _TuiLogHandler(logging.Handler):
    """Redirect Python log records into the TUI log."""
    def emit(self, record):
        tui_log(f"[dim]{escape(self.format(record))}[/]")


def _status_panel() -> Panel:
    line1 = (
        f"[bold]model:[/] [cyan]{escape(MODEL)}[/]"
        f"   [bold]cwd:[/] [cyan]{escape(DEFAULT_CWD)}[/]"
    )
    if user_sessions:
        session_parts = [
            f"[bold]{uid}:[/] [dim]{sid[:8]}…[/]"
            for uid, sid in user_sessions.items()
        ]
        line2 = "[bold]sessions:[/] " + "   ".join(session_parts)
        info = line1 + "\n" + line2
        size = 5
    else:
        info = line1 + "   [bold]sessions:[/] [dim]none[/]"
        size = 3
    return Panel(info, title="[bold green]CTA[/]"), size


def _log_panel() -> Panel:
    table = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    table.add_column("time", style="dim", width=8, no_wrap=True)
    table.add_column("text")
    with _tui_lock:
        entries = list(_log_entries)
    for ts, text in entries:
        table.add_row(ts, text)
    return Panel(table, title="[bold]Log[/]")


def _build_layout() -> Layout:
    layout = Layout()
    panel, size = _status_panel()
    layout.split_column(Layout(name="status", size=size), Layout(name="log"))
    layout["status"].update(panel)
    layout["log"].update(_log_panel())
    return layout


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "allowed_users": [],
    "claude_timeout": 600,
    "sessions_file": "sessions.json",
}


def load_config(config_path=None) -> dict:
    """Load config from file, with env var overrides."""
    config = dict(DEFAULT_CONFIG)

    # Load from file
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            file_config = json.load(f)
        config.update(file_config)

    # Env vars override file config
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        config["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("ALLOWED_USERS"):
        config["allowed_users"] = [
            int(x) for x in os.environ["ALLOWED_USERS"].split(",") if x.strip()
        ]
    if os.environ.get("CLAUDE_TIMEOUT"):
        config["claude_timeout"] = int(os.environ["CLAUDE_TIMEOUT"])

    return config


# ── Globals (set by init()) ───────────────────────────────────────────────────

BOT_TOKEN = ""
ALLOWED_USERS: set[int] = set()
TIMEOUT = 120
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5")
DEFAULT_CWD = os.getcwd()
SESSIONS_FILE = "sessions.json"

bot = None  # initialized in create_bot()
user_sessions: dict[int, str] = {}  # per-user Claude session IDs
user_cwd: dict[int, str] = {}       # per-user working directory
user_model: dict[int, str] = {}     # per-user model override
user_queues: dict[int, queue.Queue] = {}  # per-user message queues (serializes requests)
user_queues_lock = threading.Lock()


def init(config: dict):
    """Apply config to module globals."""
    global BOT_TOKEN, ALLOWED_USERS, TIMEOUT, SESSIONS_FILE
    BOT_TOKEN = config["telegram_bot_token"]
    ALLOWED_USERS = set(config["allowed_users"])
    TIMEOUT = config["claude_timeout"]
    SESSIONS_FILE = config.get("sessions_file", "sessions.json")


def load_sessions():
    """Load persisted session IDs from disk into user_sessions."""
    if not os.path.exists(SESSIONS_FILE):
        return
    try:
        with open(SESSIONS_FILE) as f:
            data = json.load(f)
        for uid_str, session_id in data.items():
            user_sessions[int(uid_str)] = session_id
        tui_log(f"[dim]Loaded {len(data)} session(s) from {SESSIONS_FILE}[/]")
    except Exception as e:
        tui_log(f"[red]Warning: could not load sessions: {escape(str(e))}[/]")


def save_sessions():
    """Persist user_sessions to disk atomically."""
    tmp = SESSIONS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({str(uid): sid for uid, sid in user_sessions.items()}, f)
        os.replace(tmp, SESSIONS_FILE)
    except Exception as e:
        tui_log(f"[red]Warning: could not save sessions: {escape(str(e))}[/]")


# ── Claude CLI ────────────────────────────────────────────────────────────────

def call_claude(prompt: str, cwd: str = None, session_id: str = None, model: str = None) -> tuple[str, str]:
    """Call Claude Code CLI. Returns (text, session_id)."""
    cwd = cwd or DEFAULT_CWD
    cmd = ["claude", "--print", "--dangerously-skip-permissions", "--model", model or MODEL,
           "--output-format", "json", "-p", prompt]
    if session_id:
        cmd += ["--resume", session_id]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)
        if not result.stdout.strip():
            err = result.stderr.strip()
            return (f"[Error] {err}" if err else "(empty response)"), ""
        data = json.loads(result.stdout)
        text = (data.get("result") or "").strip() or "(empty response)"
        return text, data.get("session_id", "")
    except subprocess.TimeoutExpired:
        return "(Claude timed out)", ""
    except FileNotFoundError:
        return "(claude CLI not found — install @anthropic-ai/claude-code)", ""


# ── Bot Handlers ──────────────────────────────────────────────────────────────

def cmd_start(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    bot.reply_to(message, "👋 Hi! I'm powered by Claude Code CLI. Just send me a message.")


def cmd_clear(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    user_sessions.pop(message.from_user.id, None)
    save_sessions()
    bot.reply_to(message, "🧹 Conversation cleared.")


def cmd_cd(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    path = message.text.replace("/cd", "", 1).strip()
    if not path:
        cwd = user_cwd.get(message.from_user.id, DEFAULT_CWD)
        bot.reply_to(message, f"📂 Current: `{cwd}`", parse_mode="Markdown")
        return
    expanded = os.path.expanduser(path)
    if os.path.isdir(expanded):
        user_cwd[message.from_user.id] = expanded
        bot.reply_to(message, f"📂 → `{expanded}`", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"❌ Not a directory: `{path}`", parse_mode="Markdown")


def cmd_pwd(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    cwd = user_cwd.get(message.from_user.id, DEFAULT_CWD)
    bot.reply_to(message, f"📂 `{cwd}`", parse_mode="Markdown")


def cmd_model(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    uid = message.from_user.id
    name = message.text.replace("/model", "", 1).strip()
    if not name:
        current = user_model.get(uid, MODEL)
        bot.reply_to(message, f"🤖 Model: `{current}`", parse_mode="Markdown")
        return
    user_model[uid] = name
    user_sessions.pop(uid, None)  # new model = fresh session
    save_sessions()
    bot.reply_to(message, f"🤖 Model → `{name}` (session cleared)", parse_mode="Markdown")


def cmd_status(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    uid = message.from_user.id
    cwd = user_cwd.get(uid, DEFAULT_CWD)
    model = user_model.get(uid, MODEL)
    session_id = user_sessions.get(uid, "none")
    bot.reply_to(
        message,
        f"🤖 Model: `{model}`\n📂 Cwd: `{cwd}`\n🔑 Session: `{session_id}`",
        parse_mode="Markdown",
    )


def _process_message(uid: int, message):
    """Process a single message for a user (called serially from the user's worker thread)."""
    cwd = user_cwd.get(uid, DEFAULT_CWD)
    model = user_model.get(uid, MODEL)
    session_id = user_sessions.get(uid)

    done = threading.Event()

    def typing_loop():
        while not done.wait(timeout=4):
            try:
                bot.send_chat_action(message.chat.id, "typing")
            except Exception:
                pass

    threading.Thread(target=typing_loop, daemon=True).start()
    try:
        reply, new_session_id = call_claude(message.text, cwd=cwd, session_id=session_id, model=model)
    finally:
        done.set()

    if new_session_id:
        user_sessions[uid] = new_session_id
        save_sessions()
    preview = reply[:120].replace("\n", " ")
    tui_log(f"[blue]←[/] [bold]{escape(str(message.from_user.username or uid))}[/] {escape(preview)}{'…' if len(reply) > 120 else ''}")
    for i in range(0, len(reply), 4096):
        bot.reply_to(message, reply[i : i + 4096])


def _user_worker(uid: int, q: queue.Queue):
    """Worker thread: processes one message at a time for a user."""
    while True:
        message = q.get()
        try:
            _process_message(uid, message)
        except Exception as e:
            tui_log(f"[red][worker:{uid}] error: {escape(str(e))}[/]")
        finally:
            q.task_done()


def _get_user_queue(uid: int) -> queue.Queue:
    with user_queues_lock:
        if uid not in user_queues:
            q = queue.Queue()
            user_queues[uid] = q
            threading.Thread(target=_user_worker, args=(uid, q), daemon=True).start()
        return user_queues[uid]


def handle_message(message):
    uid = message.from_user.id
    tui_log(f"[green]→[/] [bold]{escape(str(message.from_user.username or uid))}[/] {escape(message.text)}")
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        return
    _get_user_queue(uid).put(message)


class _Suppress409(logging.Filter):
    def filter(self, record):
        return "409" not in record.getMessage()


def create_bot():
    """Create and configure the Telegram bot."""
    global bot
    telebot_log = logging.getLogger("TeleBot")
    telebot_log.addFilter(_Suppress409())
    telebot_log.addHandler(_TuiLogHandler())
    telebot_log.propagate = False
    bot = telebot.TeleBot(BOT_TOKEN)
    bot.message_handler(commands=["start"])(cmd_start)
    bot.message_handler(commands=["clear"])(cmd_clear)
    bot.message_handler(commands=["cd"])(cmd_cd)
    bot.message_handler(commands=["pwd"])(cmd_pwd)
    bot.message_handler(commands=["model"])(cmd_model)
    bot.message_handler(commands=["status"])(cmd_status)
    bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))(handle_message)
    return bot


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CTA — Claude Telegram Agent")
    parser.add_argument("-f", "--config", default="config.json", help="Config file path (default: config.json)")
    args = parser.parse_args()

    config = load_config(args.config)
    init(config)

    if not BOT_TOKEN:
        print("Error: telegram_bot_token not set")
        print("Set in config.json or TELEGRAM_BOT_TOKEN env var")
        print(f"  python agent.py -f config.json")
        exit(1)

    load_sessions()
    create_bot()
    tui_log(f"[dim]CTA starting… model=[cyan]{MODEL}[/] cwd=[cyan]{DEFAULT_CWD}[/] users={ALLOWED_USERS or 'all'}[/]")

    if ALLOWED_USERS:
        for uid in ALLOWED_USERS:
            try:
                bot.send_message(uid, "✅ CTA is ready.")
            except Exception as e:
                tui_log(f"[red]Could not notify {uid}: {escape(str(e))}[/]")
    else:
        tui_log("[dim]No ALLOWED_USERS set — skipping startup notification[/]")

    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    with Live(auto_refresh=False, screen=True) as live:
        while True:
            live.update(_build_layout())
            live.refresh()
            time.sleep(0.25)
